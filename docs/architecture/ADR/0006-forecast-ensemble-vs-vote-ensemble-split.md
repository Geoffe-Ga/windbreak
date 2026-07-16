# ADR-0006: `ForecastConfig.ensemble` vs. `ForecastConfig.vote_ensemble` — a documented split, not a rename

- **Status:** Accepted
- **Date:** 2026-07-16
- **Issue:** #240 (epic #183: forecast provider seam and live adapters; depends
  on #184)

## Context

#184 (ADR-0005) added `ForecastConfig.vote_ensemble`: a tuple of
`EnsembleMemberConfig`, each carrying its own `provider` / `model_version` /
`training_cutoff`, that the forecast engine's vote stage
(`pipeline.collect_model_votes`) drives directly. It did not touch the
pre-existing `ForecastConfig.ensemble` field — a tuple of `ModelRef` that SPEC
§16 pins as the `forecast.ensemble:` YAML key, populated by the SPEC §16
legacy triage/promotion path.

Since #184, `ForecastConfig` therefore carries two ensemble-shaped fields at
once:

- `ensemble: tuple[ModelRef, ...]` — the SPEC §16 legacy triage/promotion
  model set. `ModelRef` is a two-field `(provider, model)` pair; it predates
  the `EnsembleMemberConfig` / `ForecastProvider` seam entirely and is not
  wired to the vote stage.
- `vote_ensemble: tuple[EnsembleMemberConfig, ...]` — the #184/#191 vote-stage
  ensemble, the one `pipeline.collect_model_votes` actually calls providers
  from, each member carrying full provenance (`model_version`,
  `training_cutoff`) that `ModelRef` does not.

Issue #240's title reads "deprecate/rename `ensemble` in favor of
`vote_ensemble`," which reads as if the two fields are redundant and one
should replace the other. They are not redundant: `ensemble` is consumed by
the triage/promotion path and is a normative SPEC §16 YAML key; `vote_ensemble`
is consumed by the vote stage and carries a different, larger shape
(`EnsembleMemberConfig` vs. `ModelRef`). Neither can be silently dropped or
folded into the other without either breaking triage/promotion or losing the
provenance fields the vote stage needs. This ADR records that the two fields
are semantically distinct and formalizes the split, rather than performing the
rename #240's title suggests.

A second, smaller problem sat alongside the naming question:
`windbreak.net.allowlist._forecast_hosts` (the SPEC §5.2/§7.1 structural
egress allowlist) derived forecast-provider hosts only from `ensemble` and
`triage_model`, never from `vote_ensemble`. A configuration whose
`vote_ensemble` named a provider absent from `ensemble` would have that
provider's live calls fail-closed-denied by the egress allowlist even though
the config explicitly configured it for voting — a latent bug this ADR's
decision also resolves.

## Decision

**No field rename and no YAML-key rename.** `ForecastConfig.ensemble` and
`ForecastConfig.vote_ensemble` both stay, with their roles now formally
documented rather than merely implied by call-site behavior:

- `vote_ensemble` is the **authoritative vote-stage ensemble**. The vote stage
  drives providers from this field alone; nothing about `ensemble` reaches
  `collect_model_votes`.
- `ensemble` **remains legacy**: it continues to back the SPEC §16
  triage/promotion path verbatim, unchanged in shape or meaning, and is
  **deprecated for vote purposes only** — it no longer sources vote-stage
  provider selection (it never did, post-#184; this ADR just says so plainly).
  It keeps contributing to egress-host derivation, since triage/promotion
  calls still need their providers reachable.

`windbreak.config.schema.ForecastConfig`'s class docstring now states this
split explicitly and cites this ADR, so a reader of the schema — not just a
reader of the pipeline call sites — can see which field governs which stage
without archaeology.

**The egress allowlist is repointed to union in `vote_ensemble`.**
`windbreak.net.allowlist._forecast_hosts` now derives the forecast-provider
host set from the union of three sources: the legacy `ensemble` `ModelRef`
set, `triage_model`, and each `vote_ensemble` member's `provider` — still
filtered through the `_FORECAST_PROVIDER_HOSTS` recognized-provider table, so
an unrecognized provider still contributes no host (fail-closed is
unconditional; it now just accounts for all three provider sources instead of
two). A configured voting provider absent from the legacy `ensemble` field is
no longer incorrectly denied network access.

### Why the rename #240's title suggested was rejected

Three independent reasons, each individually sufficient:

1. **Config-hash stability.** `windbreak.config.versioning.config_hash`
   computes its hash by flattening `dataclasses.asdict(config)` into dotted
   leaf paths keyed by *field name* (e.g. `forecast.ensemble.0.provider`) —
   see `windbreak/config/versioning.py`. Renaming the `ensemble` field would
   change every one of those dotted paths, churning the config hash that #191
   deliberately protects as a stability guarantee. There is no way to rename
   the field without that churn; the hash is name-sensitive by design.
2. **The loader hard-rejects unknown YAML keys.** `windbreak.config.loader`'s
   `_reject_unknown_keys` treats any YAML key absent from the current schema
   as fatal (SPEC §16: "unknown keys are fatal"). A YAML-key rename from
   `ensemble:` to anything else would break every existing config file that
   still writes `forecast.ensemble:` — there is no soft-deprecation path in a
   loader that fails closed on unrecognized keys.
3. **SPEC §16 pins the key normatively.** `plans/SPEC_v3.md`'s reference
   config block spells out `forecast.ensemble:` as the canonical example
   under the `forecast:` section. Renaming the field would either desync the
   schema from the SPEC's own reference config or require a SPEC amendment
   this issue does not scope.

Because none of these three obstacles can be worked around without a breaking
change this issue does not call for, the decision is: **no schema change**.
The config hash is therefore unchanged by construction — this ADR documents
behavior, it does not alter the schema shape.

## Consequences

- **One deliberate, additive behavior change.** A configuration whose
  `vote_ensemble` names a provider outside the legacy `ensemble` field now
  gets that provider's host allowlisted by `allowlist_from_config`. Every
  default configuration is host-identical to before this change (the default
  `vote_ensemble` and default `ensemble` name the same two providers,
  `anthropic` and `openai`), and no host is ever *removed* by this change —
  the union only ever grows the allowed set, never shrinks it.
- **No runtime `DeprecationWarning`.** `ensemble` staying SPEC-normative means
  a config file is *required* to keep writing `forecast.ensemble:` for
  triage/promotion; warning on a key an operator cannot stop using would be
  unactionable noise. Deprecation here is documentary (this ADR + the
  `ForecastConfig` docstring), not a runtime signal.
- **Deferred, not resolved here: wiring `vote_ensemble` into the scheduler
  composition root.** `windbreak.scheduler.loop`'s pipeline invocation calls
  `run_pipeline(...)` without an `ensemble=` argument, so
  `pipeline.collect_model_votes` falls back to its own
  `DEFAULT_VOTE_ENSEMBLE` — a triple that is mirror-equal in provenance to
  `ForecastConfig`'s `_default_vote_ensemble()`, but not the *same object*,
  and not sourced from an operator's non-default `config.forecast.vote_ensemble`
  override. A config file that customizes `vote_ensemble` away from the
  default therefore does not yet change which providers the scheduler's vote
  stage actually calls; only the egress allowlist (this ADR's change) and any
  direct `run_pipeline(ensemble=...)` caller see the override today. Threading
  `config.forecast.vote_ensemble` through `windbreak/scheduler/loop.py` into
  `run_pipeline`'s `ensemble` parameter is intentionally out of scope for this
  issue and is left as a follow-up, to be filed and scoped separately.
- **Cross-references:** #240 (this ADR's issue), #184 / ADR-0005 (introduced
  `vote_ensemble` and the `ForecastProvider` seam this decision leaves
  untouched), #183 (the epic both issues belong to), #191 (the config-hash
  stability guarantee obstacle 1 protects), SPEC §5.2/§7.1 (the egress
  allowlist this ADR repoints), SPEC §16 (the normative `forecast.ensemble:`
  key and the unknown-key-fatal loader rule).
