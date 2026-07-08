## Role

You are a senior Python engineer working in this repo's `windbreak/forecast/` package, experienced with typed pipeline architectures and deterministic record/replay test harnesses for LLM-backed systems.

## Goal

All eleven pipeline stages of SPEC §8.2 are wired end-to-end as pass-through stubs that transform a fixture `NormalizedMarket` + baseline quote snapshot into a schema-valid, immutable `ForecastRecord` (§6.3), with an LLM cassette record/replay harness so the whole run is offline and byte-deterministic in CI.

## Context

- **Parent epic:** #4
- **Predecessor issue(s):** none — this is the skeleton issue (EPIC_01 and EPIC_02 must be complete: fixed-point types, ledger, config loader, `NormalizedMarket` all exist).
- **SPEC section:** `plans/SPEC_v3.md` §8.1–§8.2 (pipeline stages), §6.3 (`ForecastRecord`), §17.1 (cassette requirement), §6.1 (no floats in probability paths — `ProbabilityPpm` int units).
- **Files involved:**
  - `windbreak/forecast/records.py` — frozen `ForecastRecord`, `ModelVote`, `Citation` models (new)
  - `windbreak/forecast/pipeline.py` — stage orchestration, one function per §8.2 stage (new)
  - `windbreak/forecast/cassettes.py` — LLM call record/replay layer keyed by request hash (new)
  - `tests/forecast/test_pipeline_skeleton.py` — smoke tests (new)
  - `tests/fixtures/forecast/` — fixture markets, baseline snapshots, recorded cassettes (new)
- **Prior decisions:** all probability/money fields are integer fixed-point (`probability_ppm: int`, `research_cost_micros: int`) per §6.1 — no `float` anywhere in the record or stage signatures. `ForecastRecord` is immutable after creation (§6.3); attempted mutation must raise.
- **State of the world:** `windbreak/forecast/` does not exist. `windbreak/` currently contains M0/M1 output (config, numeric types, ledger, connector). `windbreak/main.py` is the generated hello-world stub.

## Output Format

Deliverable is a single PR containing:

- [ ] Production code in `windbreak/forecast/{records,pipeline,cassettes}.py`; every §8.2 stage present as a typed function returning typed data (identity/stub logic is fine, control flow is real)
- [ ] Smoke tests in `tests/forecast/test_pipeline_skeleton.py` proving: fixture in → schema-valid `ForecastRecord` out; same inputs → byte-identical record; mutation attempt raises
- [ ] At least one recorded cassette fixture and a test that fails if any stage attempts a live network call in replay mode
- [ ] Docstrings on all public functions
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_pipeline_produces_schema_valid_immutable_record(fixture_market, fixture_baseline):
    record = run_pipeline(fixture_market, fixture_baseline, cassette_dir=CASSETTES)
    assert 0 <= record.probability_ppm <= 1_000_000
    assert record.triage_stage == "full"
    assert record.market_price_baseline_pips == fixture_baseline.price_pips
    with pytest.raises((AttributeError, ValidationError, FrozenInstanceError)):
        record.probability_ppm = 999
```

```python
def test_pipeline_is_deterministic(fixture_market, fixture_baseline):
    a = run_pipeline(fixture_market, fixture_baseline, cassette_dir=CASSETTES)
    b = run_pipeline(fixture_market, fixture_baseline, cassette_dir=CASSETTES)
    assert a == b
```

## Constraints

**Scope fence:** Do not implement real LLM calls, real web research, triage logic (#23), sandbox enforcement (#24), or real aggregation/coherence math (#25). Stubs return fixture-derived typed data. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. `windbreak run` (M0 idle loop) must still work; if your change breaks an unrelated surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #4` and `Closes #22`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `forecast-engine`
