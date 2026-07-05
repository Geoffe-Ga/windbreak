## Role

You are a senior Python engineer working in `hedgekit/numeric/`, expert in fixed-point arithmetic, `hypothesis` property testing, and writing custom AST lint rules.

## Goal

The four SPEC §6.1 fixed-point types (`PricePips`, `ContractCentis`, `MoneyMicros`, `ProbabilityPpm`) exist as distinct int-backed types with conservative rounding, and an AST lint gate fails CI whenever a float enters a money/price/probability path.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_01_NUMBER (must be merged first — `hedgekit/numeric/` package exists).
- **SPEC section:** plans/SPEC_v3.md §6.1 (units: PricePips = 0.0001 payout-dollars; ContractCentis = 0.01 contracts; MoneyMicros = 1e-6 dollars; ProbabilityPpm = 1e-6 probability; "Rounding is always conservative in the direction of overstating cost/risk and understating equity"); §17.3 (AST lint + property tests; metamorphic conservatism).
- **Files involved:**
  - `hedgekit/numeric/types.py` — the four types + arithmetic and cross-unit conversion (e.g., price × count → money) (new).
  - `hedgekit/numeric/rounding.py` — `RoundingDirection.OVERSTATE_COST` / `UNDERSTATE_EQUITY` helpers (new).
  - `scripts/lint_no_floats.py` — AST walker; configured path allowlist/denylist (new).
  - `.pre-commit-config.yaml` — register the float-lint hook.
  - `tests/numeric/` — unit + hypothesis property tests (new).
- **Prior decisions:** types must be distinct under mypy --strict (NewType or subclass — pick one and document why in the module docstring); mixing units without an explicit conversion is a type error. Display formatting may produce `str`, never `float`. The lint must catch: float literals, `float` annotations, `/` true division producing floats, and `float(...)` casts inside `hedgekit/numeric/`, `hedgekit/ledger/`, and (future) `hedgekit/riskkernel/` — the path list lives in the script's config so later epics extend it.
- **State of the world:** `hedgekit/numeric/__init__.py` is an empty stub; no lint hook exists; scaffold pre-commit runs 32 generic hooks.

## Output Format

Deliverable is a single PR containing:

- [ ] The four types with checked construction, addition/subtraction within a unit, scalar multiplication, and explicit conversion functions with a required `rounding=` argument
- [ ] Conservative-rounding helpers; conversions without an explicit direction do not compile/type-check
- [ ] `scripts/lint_no_floats.py` wired into pre-commit and CI via the existing quality-gate scripts
- [ ] Hypothesis property tests: round-trip conversions never understate cost / overstate equity; associativity of within-unit addition; no operation returns `float`
- [ ] A deliberately-failing fixture test proving the lint catches each forbidden pattern
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: conservative conversion**
```python
# 3 contracts (300 centis) at 4567 pips, cost must round UP in money micros
cost = money_from_price_and_count(
    PricePips(4567), ContractCentis(300), rounding=OVERSTATE_COST
)
assert cost == MoneyMicros(137_010_000)  # never one micro less
```

**Example: lint catch**
```
$ python scripts/lint_no_floats.py hedgekit/
hedgekit/numeric/types.py:88: FLOAT-001 float literal on money path (0.25)
exit status 1
```

## Constraints

**Scope fence:** Do not implement fee math, floor formulas, or Kelly sizing (EPIC_04/EPIC_06 own those; they consume these types). Do not add float-based convenience constructors "for tests". If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — `hedgekit run` still idles with heartbeats. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines meets the repo threshold (90%); note SPEC §17.6 targets 100% branch coverage for this package by M3 — aim for it now.
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` from the Claude reviewer Action on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `foundations`
