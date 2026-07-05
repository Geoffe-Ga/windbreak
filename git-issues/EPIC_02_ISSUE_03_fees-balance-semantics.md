## Role

You are a senior Python engineer working in this repo's `hedgekit/connector/` subpackage, with a background in exchange accounting semantics and contract testing.

## Goal

The connector exposes a fee-model lookup and a machine-readable `BalanceSemantics` record whose every field is proven by fixture tests — including how unsettled resolution proceeds appear before crediting (T18) — so the Risk Kernel can later refuse live trading while any field is `unknown`.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** #EPIC_02_ISSUE_02_NUMBER (must be merged first — Kalshi adapter exists)
- **SPEC section:** `plans/SPEC_v3.md` §7.3 (balance-semantics contract — "blocker for live trading"), §2 ("Why fees and microstructure are first-class" — fees ∝ `p·(1−p)`, rounded up), §4 T18 (settlement-lag mis-accounting), §20 Q1 (pull fee fields from the live schedule; golden tests against exchange-documented examples), §20 Q4 (idle-cash interest terms)
- **Files involved:**
  - `hedgekit/connector/semantics.py` — `BalanceSemantics` model: open-order collateral inclusion/exclusion, fee debit + rounding behavior, partial-fill representation, cancellation collateral release, unsettled-proceeds visibility, paused/halted-market behavior — each field a typed enum, never a bare bool, with `UNKNOWN` as an explicit member
  - `hedgekit/connector/fees.py` — `FeeModel` with `max_trading_fee_micros(price_pips, count_centis)` and `max_settlement_fee_micros(...)` upper-bound calculators, conservative rounding (§6.1)
  - `hedgekit/connector/kalshi/adapter.py` — implement `get_balance_semantics()` and `get_fee_model()`
  - `tests/connector/kalshi/test_balance_semantics.py`, `tests/connector/kalshi/test_fees.py`
  - `tests/fixtures/exchange/kalshi/` — balance/fill/settlement fixtures for each semantics question
- **Prior decisions:** All fee math is fixed-point `MoneyMicros`; rounding always overstates cost (§6.1). Fee schedules are data fetched/recorded from the exchange, never constants hardcoded from secondary sources (§20 Q1).
- **State of the world:** `KalshiConnector` covers markets/books/status; `get_balance_semantics()` and `get_fee_model()` currently raise `NotImplementedError` from ISSUE_01's stub.

## Output Format

Deliverable is a single PR containing:

- [ ] `BalanceSemantics` record answering every §7.3 question via typed enums with explicit `UNKNOWN`
- [ ] Fixture tests proving each field's value for Kalshi: open-order collateral treatment, fee debit + rounding, partial fills, cancellation release, unsettled proceeds pre-credit (T18), paused/halted behavior
- [ ] `FeeModel` upper-bound calculators with golden tests against exchange-documented fee examples
- [ ] Property test: computed fee upper bound is never below the exchange-documented fee for the same inputs
- [ ] `FakeExchange` updated to serve a fully-known `BalanceSemantics` and a simple fee model so downstream epics can test against it
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands**

```python
def test_unsettled_proceeds_excluded_from_available(kalshi_fixture_connector):
    sem = kalshi_fixture_connector.get_balance_semantics()
    assert sem.unsettled_proceeds is UnsettledProceeds.EXCLUDED_UNTIL_CREDITED

def test_fee_upper_bound_never_understates(fee_model):
    # golden example from the exchange's published fee schedule
    fee = fee_model.max_trading_fee_micros(price_pips=5000, count_centis=10_000)
    assert fee >= 175_000  # documented fee for 100 contracts @ 50¢, in micros
```

## Constraints

**Scope fence:** Do not implement the Risk Kernel's refusal logic (that is EPIC_04/M3 — this issue only makes the record available and proven). Do not implement PaperExchange fee application (ISSUE_04). If a semantics question cannot be answered from recorded fixtures or demo-environment evidence, set the field to `UNKNOWN` and document why in the fixture README — do not guess. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. If your change breaks an unrelated surface, revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `connector`
