## Role

You are a senior Python engineer with quantitative-evaluation sensibilities (calibration, proper scoring rules, gating) working across `hedgekit/forecast/` and the evaluation-facing seams.

## Goal

Live eligibility becomes *earned per provider*: a versioned calibration-map loader applies fitted maps when M6 produces them (identity v0 until then), and a provider track-record gate keeps any member family's votes out of live-eligible records until that provider has ≥ N resolved paper forecasts whose Brier skill beats the market baseline — so the system can never trade live on a forecaster that hasn't demonstrably outperformed doing nothing.

## Context

- **Parent epic:** #183
- **Predecessor issue(s):** #193 (merged first — quorum semantics interact with per-provider exclusion).
- **SPEC section:** `plans/SPEC_v3.md` §8.2 (versioned calibration map stage), §13/§16 `evaluation:` block (`min_resolved_for_calibration: 150`, `brier_skill_required_ppm: 10000`), §19 (no unmeasured-edge claims), §9.6 (dispersion feeds sizing — semantics must stay meaningful across heterogeneous providers).
- **Files involved:**
  - `hedgekit/forecast/calibration.py` — versioned map loader: map id + version recorded in provenance; identity v0 when no fitted map exists; loading a map whose version postdates the forecast's `created_at` is rejected (temporal integrity) (new)
  - `hedgekit/forecast/pipeline.py` — `apply_calibration_map` consumes the loaded map; provider track-record gate ANDed into `eligible_for_live` beside the canary gate (modify)
  - `hedgekit/forecast/providers/track_record.py` — `ProviderTrackRecord` read model: consumes M6 evaluation outputs (resolved count, Brier skill vs. market baseline, per provider); this module *reads* evaluation artifacts, it never recomputes scores (new)
  - `hedgekit/forecast/ensemble.py` — document + test dispersion semantics when members span families (research forecaster vs. LLM vote): dispersion must still be the honest disagreement signal §9.6 sizes against (modify, minimal)
  - `hedgekit/config/` — `forecast.provider_gate: {min_resolved: int, min_brier_skill_ppm: int}` defaults aligned with the `evaluation:` block (modify)
  - `tests/forecast/test_calibration_loader.py`, `tests/forecast/test_provider_gate.py` (new)
- **Prior decisions:** calibration *fitting* belongs to M6 (epic #8) — this issue only loads/applies/records versions; evaluation gate math (clustered bootstrap etc.) stays in `hedgekit/evaluation/`; `is_live_eligible` is the single choke point for eligibility flags — extend it, don't fork it; all thresholds integer ppm.
- **State of the world:** `apply_calibration_map` is a hardcoded identity with no versioning or provenance; nothing distinguishes a provider with 500 resolved, market-beating forecasts from one wired up yesterday.

## Output Format

Deliverable is a single PR containing:

- [ ] Versioned calibration loader with provenance (map id/version on the record path) and temporal-integrity rejection
- [ ] Provider track-record read model + gate: an ungated provider's votes still *run and are recorded* (that's how the track record accrues in paper) but force `eligible_for_live=False`, with the gating decision ledgered
- [ ] Dispersion semantics documented and property-tested across heterogeneous member families
- [ ] Config defaults consistent with the SPEC `evaluation:` block, unknown keys fatal
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: trust is earned**
```python
def test_unproven_provider_forces_paper_only(pipeline_env):
    record = run_pipeline_with_track_records(
        pipeline_env, {"futuresearch": resolved(12, brier_skill_ppm=40000)}
    )  # 12 < min_resolved=150 → not yet proven
    assert record.eligible_for_live is False
    assert pipeline_env.ledger.has_event("PROVIDER_GATE_HELD")
```

**Example: stale calibration map cannot leak the future**
```python
def test_calibration_map_from_the_future_is_rejected():
    with pytest.raises(TemporalIntegrityError):
        load_calibration_map(version="2026-09-01", forecast_created_at=JULY_1)
```

## Constraints

**Scope fence:** No score computation, no bootstrap, no gate-report changes — that is epic #8's surface; consume its artifacts read-only. No selector/sizing changes (§9.6 consumes dispersion as-is). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** With no fitted map and no track records on disk, behavior is byte-identical to today (identity map, gate held) — the honest default is "not proven yet", and the system stays fully demoable in paper mode.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes; float-lint clean.
- [ ] Docstrings updated; EVALUATION.md cross-reference added where the gate is described.
- [ ] PR body includes `Refs #183` and `Closes #194`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `forecast-engine`, `evaluation`
