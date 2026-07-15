# ADR-0005: External research forecasters as ensemble members

- **Status:** Accepted
- **Date:** 2026-07-15
- **Issue:** #184 (epic #183: forecast provider seam and live adapters)

## Context

SPEC §8.2's pipeline diagram names the vote stage plainly: `independent
structured model votes (no tools) → median aggregation`. Pre-#184, that
stage was one hard-wired function (`pipeline._vote_prompt` +
`pipeline._VOTE_MODELS`) that built a deterministic prompt from the
pipeline-supplied baseline and verified quotes, sent it through a single
in-module `LlmTransport` call, and parsed the JSON reply. There was no seam
between "collect one ensemble member's vote" and "how that vote is
obtained" — a real OpenAI/Anthropic client, a batching provider, or a
FutureSearch-style hosted research forecaster all had to squeeze through
the same in-module prompt-building code or not exist at all.

The forecast roadmap (epic #183) calls for a second, structurally different
kind of ensemble member: a **research forecaster** (FutureSearch-style)
that does its own bounded web research internally and returns a single
structured probability, rather than voting only on the quotes the pipeline
already verified and handed it. That is a member whose research and vote
are *fused* into one provider call — and it is, on its face, a member that
"uses tools," which reads as a direct conflict with SPEC §8.2's "(no
tools)" parenthetical.

Two problems had to be solved together, not sequentially: (1) the vote
stage needed a real seam so any provider — fixture, no-tools LLM, or
research forecaster — could plug in without the pipeline knowing which kind
it is, and (2) the "no tools" wording needed to be reconciled with a member
family that clearly does use tools, without loosening the SPEC §8.3
sandbox-boundary guarantee that wording exists to protect.

## Decision

### 1. Two member families behind one seam

`windbreak/forecast/providers/base.py` introduces the `ForecastProvider`
protocol — one method, `forecast(market, baseline, vote_index, quotes) ->
ProviderForecast` — as the single seam every ensemble member's vote crosses
through, regardless of family:

- **(a) No-tools LLM vote.** The classic SPEC §8.2 member: an independent
  structured model vote that sees only the pipeline-supplied baseline and
  verified quotes, threaded through `build_vote_prompt` as labelled
  untrusted-data blocks (SPEC §8.5), with zero tool access of its own.
  `windbreak/forecast/providers/fixture.py`'s `FixtureVoteProvider` is the
  first, network-free implementation of this family — it proves the seam
  end-to-end offline before any live client exists.
- **(b) Research forecaster.** A provider whose research and vote are
  fused: it performs its own bounded research (e.g. FutureSearch's hosted
  pipeline) and returns a structured probability, never seeing the
  pipeline's verified-quote text at all. It satisfies the *identical*
  `ForecastProvider` protocol and returns the *identical* `ProviderForecast`
  shape as family (a); the pipeline (`collect_model_votes` in
  `windbreak/forecast/pipeline.py`) cannot tell the two families apart, and
  does not need to.

Both families cross the seam as a frozen `ProviderForecast`
(`probability_ppm`, `rationale_summary`, `citations`, `cost_micros`,
`provider`, `model_version`, `training_cutoff`, `response_fingerprint`), and
a rejected response crosses back as `ProviderResponseRejectedError`,
carrying only a `RESPONSE_FAILURE_*` code and a sha256 `response_fingerprint`
— never the raw untrusted text (`providers/base.py::fingerprint_response`).

### 2. SPEC §8.2's "no tools" clause is amended, not violated

SPEC §8.2 reads "independent structured model votes (no tools)." Taken
literally, a research-forecaster member fails that reading — it does use
tools internally. The clause was never actually protecting "no tool call
anywhere in the vote stage" as an end in itself; SPEC §8.3 spells out what
it is protecting: the *sandbox boundary* forbidding ledger queries, config
reads, balance/position reads, order-book reads past the baseline snapshot,
risk APIs, order APIs, filesystem access outside the research cache, shell,
and any network destination outside the allowlist — "enforced by
process/namespace isolation," not by prompt wording.

A research forecaster reached only through `ForecastProvider` preserves
every one of those guarantees:

- It is provider-agnostic and reached *only* through the seam
  (`providers/base.py::ForecastProvider`) — the engine never imports a
  vendor SDK, so the provider's internal tool use never crosses into
  `windbreak`'s process at all.
- Whatever it does internally, its *output* crosses the trust boundary as a
  schema-validated integer-ppm `ProviderForecast` — the same
  `_require_probability_ppm` guard fixture votes satisfy
  (`ProviderForecast.__post_init__`).
- Per SPEC §8.5, that output is still injection-screened, schema-validated,
  and **discarded, not repaired**, on any failure
  (`sanitize.py::validate_vote_response` / `parse_vote_response`,
  `providers/fixture.py::FixtureVoteProvider.forecast` raising
  `ProviderResponseRejectedError` on rejection).

So the safety invariant SPEC §8.2's clause was standing in for — *untrusted
model output can only ever touch probability/rationale fields, and zero
tool calls escape the sandbox allowlist* — is fully preserved. What this
ADR amends is only the literal wording: **"the vote model's tools, if any,
live behind the provider seam, and its output crosses the trust boundary as
validated integer ppm"** replaces the flat "(no tools)" reading. A no-tools
LLM vote is simply the degenerate case of this rule where the provider
happens to use no tools at all.

### 3. Provider-agnosticism: FutureSearch first, never only

Nothing in `windbreak/forecast/providers/` or `windbreak/forecast/pipeline.py`
names FutureSearch, OpenAI, or Anthropic. The vendor-neutral seam is two
pieces:

- `ForecastProvider` (the protocol) + `ProviderForecast` (the frozen
  dataclass result) — the contract any vendor's adapter must satisfy.
- `EnsembleMemberLike` — a structural protocol exposing `provider`,
  `model_version`, `training_cutoff` as read-only strings, satisfied by both
  the forecast-package-local `EnsembleMember` and
  `windbreak.config.schema.EnsembleMemberConfig`, so the engine drives an
  ensemble without ever importing `windbreak.config` (SPEC §8.3).

Ensemble membership is config-driven: `ForecastConfig.vote_ensemble`
(`windbreak/config/schema.py`) is a tuple of `EnsembleMemberConfig`, and
`pipeline.collect_model_votes`'s `ensemble` parameter accepts any
`tuple[EnsembleMemberLike, ...]` — a caller-supplied override or, when
`None`, the pinned `DEFAULT_VOTE_ENSEMBLE` three-member no-tools triple
(byte-identical to the pre-#184 `_VOTE_MODELS`). FutureSearch is the first
integration this seam is built for, but live adapters — including a
FutureSearch adapter and live no-tools LLM clients — are deliberately out
of scope for #184 and tracked as separate child issues under epic #183
(#189, #191, #192). #184 ships the seam and its first, network-free prover
(`FixtureVoteProvider`); #240 tracks the follow-up to retire
`FixtureVoteProvider` as the pipeline's *default* once a live provider from
#189/#191/#192 is available to replace it in non-test configurations.

### 4. The integer-ppm boundary rule

Every probability crossing the provider seam is an integer
parts-per-million value in `[0, 1_000_000]`, never a float, enforced at
four independent points so a float cannot slip through by any single path:

1. `ProviderForecast.__post_init__` calls `_require_probability_ppm`
   (`providers/base.py`), which rejects a non-`int` or a `bool` (an `int`
   subclass that must never masquerade as a probability) before the range
   check.
2. `sanitize.py::validate_vote_response` / `_probability_failure` schema-
   checks the raw JSON `probability_ppm` leaf and returns
   `RESPONSE_FAILURE_NON_INTEGER_PROBABILITY` for a JSON float (checked
   *before* the `int` branch, since `bool` is an `int` subclass) —
   `parse_vote_response` then raises rather than truncating.
3. `cassettes.py::_reject_float` is installed as the `parse_float` hook on
   every cassette load, so a recorded fixture response containing a float
   leaf (e.g. an errant `temperature: 0.7`) fails cassette loading outright.
4. `scripts/lint_no_floats.py`'s AST float-lint bans float literals on the
   probability/money path at the source level, guarding every module named
   above.

A response that fails any of these is **discarded and ledgered, never
repaired** — `collect_model_votes` catches `ProviderResponseRejectedError`,
records a `FORECAST_OUTPUT_DISCARDED_EVENT` with a fingerprint-only payload
when a ledger is wired, and drops the vote (SPEC §8.5). This is what makes
it safe for a research-forecaster's output to join the same ensemble as a
no-tools LLM vote: regardless of what the provider did internally, only a
validated integer ppm value, a bounded rationale string, and a fingerprint
ever reach `ModelVote` (`pipeline.py::_build_model_vote`).

## Alternatives considered

1. **Give research forecasters their own parallel, non-ensemble pipeline
   path instead of a shared seam.** Rejected: this would duplicate the
   discard/ledger machinery (`ProviderResponseRejectedError` →
   `FORECAST_OUTPUT_DISCARDED_EVENT`) and the integer-ppm boundary checks
   for a second time, and would require `aggregate_median`
   (`windbreak/forecast/ensemble.py`) to reconcile two differently-shaped
   result types. One seam, one boundary, one aggregation path is strictly
   simpler and matches SPEC §8.6's single "median aggregation" step.
2. **Read SPEC §8.2 literally and exclude research-forecaster members
   entirely (no tools, full stop).** Rejected: this discards exactly the
   member family the roadmap (epic #183) exists to add, and it would leave
   FutureSearch-style research fused into the ensemble impossible to
   represent without either lying about what the provider does or
   duplicating its research inside the pipeline's own sandboxed
   `ResearchTools` (SPEC §8.3) — which is a narrower, differently-scoped
   boundary than what a hosted research forecaster runs internally.
3. **Hardcode FutureSearch as a special-cased branch in the pipeline or
   ensemble modules.** Rejected: violates provider-agnosticism outright,
   forces the forecast engine to import a specific vendor SDK (widening the
   SPEC §8.3 sandbox surface for no reason), and blocks #189/#191/#192 from
   adding further providers without another pipeline change. The
   `ForecastProvider` protocol makes membership a config concern
   (`ForecastConfig.vote_ensemble`), not a code concern.
4. **Let a provider return a float probability and convert it to ppm at the
   seam.** Rejected: this reopens exactly the float-leak class of bug
   `scripts/lint_no_floats.py` exists to prevent, and moves the failure
   point from construction (`ProviderForecast.__post_init__` /
   `parse_vote_response`, which reject immediately) to a downstream cast
   that could silently round instead of failing closed.

## Consequences

- **Positive:** One `ForecastProvider` seam serves both member families
  with no pipeline-visible distinction between them — `collect_model_votes`
  drives `len(ensemble)` provider calls regardless of what each member's
  provider does internally, and every result is subject to the identical
  injection screen, schema validation, and integer-ppm guard.
- **Positive, behavior-preserving:** `DEFAULT_VOTE_ENSEMBLE` and
  `ForecastConfig`'s `_default_vote_ensemble()` are pinned to the pre-#184
  `_VOTE_MODELS` triple, and `build_vote_prompt` is byte-identical to the
  pre-#184 `pipeline._vote_prompt`, so extracting the seam changed no
  existing vote provenance, prompt text, or byte-determinism for the
  default configuration.
- **Out of scope:** Live provider adapters — a real OpenAI/Anthropic
  no-tools client and a FutureSearch research-forecaster client — are not
  part of #184. They are tracked as epic #183's child issues #189, #191,
  and #192, each of which must satisfy `ForecastProvider` without the
  forecast engine changing.
- **Deferred:** Retiring `FixtureVoteProvider` as the pipeline's default
  ensemble driver once a live provider exists is tracked as issue #240 — a
  deliberate follow-up, not resolved here, since #184's job is proving the
  seam offline and deterministically, not shipping a live default.
- **Cross-references:** SPEC §8.2 (pipeline stage this ADR amends the
  wording of), §8.3 (the sandbox boundary the amendment preserves), §8.5
  (the injection screen and discard-not-repair rule both member families
  are subject to), §8.6 (model pinning, canaries, and the provenance every
  `ModelVote` carries).
