## Role

You are a senior Python engineer specializing in deterministic, pure-functional financial decision code, working in this repo's `windbreak/selector/` package.

## Goal

The Trade Selector exists as a pure, credentialless function with the full typed signature from SPEC §9.1, returns zero intents (stub) with a ledgerable decision record explaining why, and a golden-test harness proves byte-identical output across repeated runs on recorded inputs.

## Context

- **Parent epic:** #7
- **Predecessor issue(s):** none — this is the skeleton issue for this epic. (Cross-epic: requires the domain types from EPIC_01 — `ForecastRecord`, `NormalizedOrderIntent`, fixed-point numeric types — and recorded order-book fixtures from EPIC_02.)
- **SPEC section:** `plans/SPEC_v3.md` §9.1 (responsibility, purity, determinism), §6.4 (`NormalizedOrderIntent`), §6.1 (numeric units)
- **Files involved:**
  - `windbreak/selector/__init__.py` — public API: `select(inputs: SelectorInputs) -> SelectorDecision`
  - `windbreak/selector/types.py` — `SelectorInputs` (forecast record, calibration map version, order-book snapshot, fee model, slippage model, position read model, risk-config snapshot, correlation tags) and `SelectorDecision` (zero or more intents + machine-readable skip/veto reasons)
  - `tests/selector/test_determinism_golden.py` — golden harness
  - `tests/selector/fixtures/` — at least two recorded book + forecast input bundles
- **Prior decisions:** the selector never holds credentials, never performs I/O, and never reads the clock — all freshness checks compare timestamps *carried in the inputs*. All money/price/probability values are fixed-point integers (`PricePips`, `ContractCentis`, `MoneyMicros`, `ProbabilityPpm`); floats on these paths are forbidden (§6.1, §17.3).
- **State of the world:** nothing exists under `windbreak/selector/` yet. Domain types exist from EPIC_01; recorded fixtures exist from EPIC_02.

## Output Format

Deliverable is a single PR containing:

- [ ] Production code: `windbreak/selector/types.py`, `windbreak/selector/__init__.py` with the stub `select()` returning zero intents and a populated `SelectorDecision.reasons` list (e.g., `["stub: selection logic not yet implemented"]`)
- [ ] Canonical serialization for `SelectorDecision` (stable field order, no floats, no timestamps generated inside the function) so byte-identical comparison is meaningful
- [ ] Golden tests in `tests/selector/test_determinism_golden.py`: same inputs → byte-identical serialized decision, run twice in-process and once from a fresh interpreter
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_select_is_byte_identical_on_recorded_inputs(recorded_inputs_bundle_a):
    first = serialize_decision(select(recorded_inputs_bundle_a))
    second = serialize_decision(select(recorded_inputs_bundle_a))
    assert first == second  # byte-identical, §9.1

def test_stub_returns_zero_intents_with_reason(recorded_inputs_bundle_a):
    decision = select(recorded_inputs_bundle_a)
    assert decision.intents == ()
    assert decision.reasons  # never silently empty
```

## Constraints

**Scope fence:** Do not implement edge computation, sizing, price bands, or any real selection logic — those belong to issues #44 through #47. Do not touch `windbreak/riskkernel/` or `windbreak/order_gateway/`. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. `windbreak run` (RESEARCH idle loop from EPIC_01) must still start and heartbeat; the selector is callable but produces no intents.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Public API documented with docstrings citing SPEC §9.1.
- [ ] PR body includes `Refs #7` and `Closes #43`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `selector`
