## Role

You are a senior Python engineer specializing in financial accounting correctness, working in this repo's `hedgekit/riskkernel/` package with fixed-point integer arithmetic (no floats on money paths).

## Goal

The Kernel computes `worst_case_equity` exactly per SPEC §10.4 with conservative rounding, evaluates the full §10.3 per-order check list fail-closed, and approves an opening buy only when `worst_case_equity − worst_case_cost ≥ floor` — proven by unit, property, and metamorphic tests.

## Context

- **Parent epic:** #EPIC_04_NUMBER
- **Predecessor issue(s):** #EPIC_04_ISSUE_01_NUMBER (must be merged first)
- **SPEC section:** `plans/SPEC_v3.md` §10.3 (per-order checks), §10.4 (floor formula), §1.1-1 (Floor Invariant), §6.1 (numeric units), §17.3 (accounting proofs), threat T4
- **Files involved:**
  - `hedgekit/riskkernel/floor.py` — new: `worst_case_equity`, `worst_case_cost` in `MoneyMicros`/`PricePips` integer units
  - `hedgekit/riskkernel/checks.py` — replace stub checks with real implementations of the §10.3 list (those implementable now; checks whose inputs arrive in later issues stay VETO-stubbed and say so)
  - `hedgekit/accounting/` — fixed-point helpers from EPIC_01 (`MoneyMicros`, `PricePips`, `ContractCentis`); extend only with conservative-rounding utilities
  - `tests/riskkernel/test_floor.py`, `tests/riskkernel/test_checks.py`, `tests/riskkernel/test_floor_metamorphic.py`
- **Prior decisions:** Formula is fixed by spec: `worst_case_equity = exchange_verified_available_cash + guaranteed_terminal_value_of_positions − pending_kernel_reservations − unresolved_fee_upper_bounds − reconciliation_uncertainty_buffer`; opening-buy `worst_case_cost = limit_price·count + max_trading_fee + max_settlement_fee + conservative_rounding_buffer`. Rounding always overstates cost/risk, understates equity (§6.1). Any check *error* (not just failure) → VETO (§10.3). For closes, worst-case cost must be provably non-increasing or veto (§10.4).
- **State of the world:** Kernel process, mode machine, and a veto-everything check pipeline exist from the skeleton issue. Balance inputs are injected via typed interfaces (real exchange verification arrives in #EPIC_04_ISSUE_04_NUMBER — use in-memory fakes here).

## Output Format

Deliverable is a single PR containing:

- [ ] `floor.py` implementing both formulas on integer units only — an AST-level lint/test proves no `float` enters the path (§17.3)
- [ ] Real implementations in `checks.py` for: instrument whitelist, mode permission + ceiling, floor invariant, fee upper-bound present, settlement-fee upper-bound, concentration limits, daily loss limit, trailing drawdown, velocity limits, quote freshness, forecast freshness, price-band compliance, participation-cap compliance, clock-skew limit, reduce-only provable for closes — each pure, typed, individually unit-tested
- [ ] Hypothesis property tests: approval implies `worst_case_equity − worst_case_cost ≥ floor` for all generated inputs
- [ ] Metamorphic tests: adding any hypothetical adverse event (extra reservation, higher fee bound, larger uncertainty buffer) never *increases* computed worst-case equity (§17.3)
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
@given(adverse=adverse_events(), state=account_states())
def test_adverse_event_never_increases_worst_case_equity(state, adverse):
    baseline = worst_case_equity(state)
    assert worst_case_equity(apply(state, adverse)) <= baseline

def test_open_buy_that_would_breach_floor_is_vetoed():
    state = account_state(available_cash_micros=1_050_000_000,
                          floor_micros=1_000_000_000)
    intent = buy_intent(limit_price_pips=5_000, count_centis=20_000)  # ~$100 + fees
    assert kernel.evaluate_intent(intent, state).vetoed
```

## Constraints

**Scope fence:** Reservation creation/serialization is #EPIC_04_ISSUE_03_NUMBER — here, `pending_kernel_reservations` is a read-only input. Live exchange balance fetching is #EPIC_04_ISSUE_04_NUMBER. Promotion-gate checks are #EPIC_04_ISSUE_05_NUMBER; human-ack is #EPIC_04_ISSUE_06_NUMBER — leave those pipeline slots as explicit VETO stubs. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. Intents still flow in and are decided (now with real floor math); no other surface breaks.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] 100% branch coverage on `riskkernel` and fixed-point accounting paths (§17.6); ≥90% on other changed lines.
- [ ] `mypy --strict` clean; public APIs documented.
- [ ] PR body includes `Refs #EPIC_04_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer Action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `risk-kernel`
