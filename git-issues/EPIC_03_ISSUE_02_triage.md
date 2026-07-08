## Role

You are a senior Python engineer working in this repo's `windbreak/forecast/` package, comfortable with cost-budgeted LLM orchestration and integer fixed-point accounting.

## Goal

Two-stage triage (SPEC §8.4) gates the expensive research pipeline: a cheap Stage-0 prior costing ≤ ~2% of the full-pipeline budget runs first, and the full pipeline runs only if `|prior − executable_price| ≥ triage_threshold_ppm` (config, default 50000), the market is operator-flagged, or a refresh trigger fired — with both stages' costs ledgered and triage-only records permanently live-ineligible.

## Context

- **Parent epic:** #4
- **Predecessor issue(s):** #22 (must be merged first — pipeline skeleton and cassette harness exist).
- **SPEC section:** `plans/SPEC_v3.md` §8.4 (two-stage triage), §4 T11 (LLM cost blowout), §2 "Why the LLM cost model is part of the strategy", §16 `forecast.triage_model` / `triage_threshold_ppm` / `budget` config keys, §6.3 `triage_stage` and `research_cost_micros` fields.
- **Files involved:**
  - `windbreak/forecast/triage.py` — Stage-0 prior + gating decision (new)
  - `windbreak/forecast/pipeline.py` — entry point routes through triage before full run (modify)
  - `windbreak/forecast/records.py` — enforce `triage_stage="triage_only"` ⇒ `eligible_for_live=False` invariant (modify)
  - `tests/forecast/test_triage.py` — gating, cost, eligibility tests (new)
- **Prior decisions:** costs are `MoneyMicros` ints; the triage model is a separate pinned config entry (`forecast.triage_model`); every triage decision (run-full vs. stop) is a ledgered event.
- **State of the world:** pipeline skeleton runs end-to-end on cassettes; `triage_stage` exists on the record but is hardcoded `"full"`; no gating or cost accounting yet.

## Output Format

Deliverable is a single PR containing:

- [ ] Production code in `windbreak/forecast/triage.py` + pipeline wiring
- [ ] Tests in `tests/forecast/test_triage.py` proving: below-threshold prior stops the pipeline and stores a `triage_only` record; at/above threshold proceeds to full; operator-flag and refresh-trigger overrides proceed; both stages' costs accumulate into `research_cost_micros`; ledger events emitted for both outcomes
- [ ] Property test: a `triage_only` record can never have `eligible_for_live=True` (construction raises)
- [ ] Docstring / doc updates for the new public entry point
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_triage_gates_full_pipeline(fixture_market, fixture_baseline):
    # cassette prior: 520000 ppm; baseline price: 500000 ppm equiv; threshold: 50000
    record = run_pipeline(fixture_market, fixture_baseline, cassette_dir=CASSETTES,
                          triage_threshold_ppm=50_000)
    assert record.triage_stage == "triage_only"      # |0.52 - 0.50| < 0.05 → stop
    assert record.eligible_for_live is False
    assert 0 < record.research_cost_micros <= FULL_BUDGET_MICROS // 50   # ≤ ~2%
```

```python
def test_triage_only_record_cannot_be_live_eligible():
    with pytest.raises(ValidationError):
        make_forecast_record(triage_stage="triage_only", eligible_for_live=True)
```

## Constraints

**Scope fence:** Do not implement real research tools or sandbox enforcement (#24), ensemble aggregation (#25), or per-day budget kill-switches (#28 — this issue ledgers costs; global budget *enforcement* is polish). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — the skeleton pipeline (full path) must still produce records from cassettes when triage says "go".

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #4` and `Closes #23`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `forecast-engine`
