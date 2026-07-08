## Role

You are a senior Python engineer (Python ≥3.11, mypy --strict, protocol-driven design) working in this repo's `hedgekit/forecast/` package.

## Goal

Vote probabilities are parsed from structured provider responses (integer ppm JSON) instead of being derived from the market baseline ± fixed offsets — behind a new provider-agnostic `ForecastProvider` seam, with an ADR recording the two-family ensemble design — while CI remains fully offline and byte-deterministic via updated fixtures/cassettes.

## Context

- **Parent epic:** #183
- **Predecessor issue(s):** none — this is the skeleton issue.
- **SPEC section:** `plans/SPEC_v3.md` §8.2 (vote stage — amended by this issue's ADR), §8.6 (pinning, fingerprints, T14), §16 `forecast:` ensemble config block.
- **Files involved:**
  - `hedgekit/forecast/providers/__init__.py` — new subpackage (new)
  - `hedgekit/forecast/providers/base.py` — `ForecastProvider` protocol; frozen `ProviderForecast` (probability_ppm: int, rationale_summary: str, citations: tuple, cost_micros: int, provider/model_version/training_cutoff provenance); `ProviderError` taxonomy root (new)
  - `hedgekit/forecast/providers/fixture.py` — deterministic fixture provider reproducing today's CI behavior through the new seam (new)
  - `hedgekit/forecast/pipeline.py` — `collect_model_votes` parses `probability_ppm` from the (validated) response JSON; `_VOTE_MODELS`/`_VOTE_OFFSETS_PPM` become config-driven ensemble membership; `_build_model_vote` takes the parsed ppm (modify)
  - `hedgekit/forecast/sanitize.py` — extend `validate_vote_response` to schema-validate the structured vote JSON (integer ppm in [0, 1_000_000], bounded rationale) (modify)
  - `hedgekit/config/` — typed `forecast.providers`/`forecast.ensemble` config (unknown keys fatal, per M0 loader rules) (modify)
  - `docs/architecture/ADR/` — new ADR: "External research forecasters as ensemble members" (new)
  - `tests/forecast/` — seam tests + updated fixtures (modify/new)
- **Prior decisions:** cassette JSON rejects floats (`cassettes.py::_reject_float`) and the AST float-lint bans floats in the probability path — the vote schema MUST carry probability as **integer ppm**; `triage.py::_parse_prior_ppm` is the in-repo precedent for parsing ppm from a response. Invalid output is discarded and ledgered, never repaired (§8.5).
- **State of the world:** `collect_model_votes` computes `probability_ppm = clamp(base_ppm + offset)` — the response text only feeds the fingerprint. The three ensemble members are hardcoded fictional models. The whole pipeline is deterministic and green in CI.

## Output Format

Deliverable is a single PR containing:

- [ ] The `providers/` subpackage with protocol, frozen dataclasses, and error taxonomy root — all `mypy --strict` clean
- [ ] `collect_model_votes` deriving each `ModelVote.probability_ppm` from its validated response; a response failing schema validation is discarded + ledgered exactly like today's injection discards
- [ ] Ensemble membership (provider, model_version, training_cutoff) read from typed config, not module constants
- [ ] ADR documenting: two member families (research forecaster = research+vote fused; no-tools LLM vote), why §8.2's "no tools" clause is amended rather than violated, provider-agnosticism (FutureSearch first, never only), and the integer-ppm boundary rule
- [ ] Updated fixtures/cassettes so the full suite passes offline, byte-deterministically
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: structured vote response (cassette entry) → ModelVote**
```json
{"probability_ppm": 470000, "rationale_summary": "Base rate 40-50%; polling shift unpriced.", "abstain": false}
```
```python
def test_vote_probability_comes_from_response_not_baseline():
    record = run_pipeline(market, baseline_at_30_cents, transport=replay, ...)
    assert record.model_votes[0].probability_ppm == 470000  # not 300000 ± offset
```

**Example: malformed vote is discarded, never guessed**
```python
def test_non_integer_probability_is_discarded_and_ledgered(pipeline_env):
    run_with_vote_response(pipeline_env, '{"probability_ppm": 0.47}')
    assert pipeline_env.ledger.has_event("FORECAST_OUTPUT_DISCARDED")
```

## Constraints

**Scope fence:** No network code, no live API clients, no new dependencies — live adapters are #189/#191/#192. Do not touch selector, evaluation, or riskkernel packages. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — the full pipeline still runs offline in CI and produces schema-valid, live-eligible records from fixtures. Injection corpus stays green.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`), including the injection corpus.
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes; float-lint clean.
- [ ] ADR committed under `docs/architecture/ADR/`; public API changes reflected in docstrings.
- [ ] PR body includes `Refs #183` and `Closes #184`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `forecast-engine`
