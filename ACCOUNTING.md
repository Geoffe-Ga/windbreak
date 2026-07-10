# Accounting

This document describes windbreak's fixed-point accounting model: the four
integer unit types SPEC §6.1 mandates, the conservative rounding direction
every division uses, the SPEC §10.4 floor formula that gates every order, and
the SPEC §7.3 balance-semantics contract that blocks live trading until an
exchange adapter has proven its own accounting is fully understood.

## Fixed-point units, never floats (SPEC §6.1)

Every dollar, price, contract count, and probability on the money path is a
scaled integer, wrapped in one of four frozen, nominally distinct types
(`windbreak/numeric/types.py`), so a value in one unit can never be silently
mixed with a value in another:

| Type | Scale | Example |
|---|---|---|
| `PricePips` | 1e-4 payout-dollars | `4567` == `$0.4567` |
| `ContractCentis` | 1e-2 contracts | `300` == `3.00` contracts |
| `MoneyMicros` | 1e-6 dollars | `1_370_100` == `$1.370100` |
| `ProbabilityPpm` | 1e-6 probability | `456_700` == `45.6700%` |

Cross-unit arithmetic (e.g. adding a `PricePips` to a `ContractCentis`) is both
a `mypy --strict` error and a runtime `TypeError`; no `__truediv__` is ever
defined, so no unit value can be divided by another even accidentally. A
`no-floats-money-paths` pre-commit hook (`scripts/lint_no_floats.py`) enforces
this at the source-file level across `windbreak/numeric/`, `windbreak/ledger/`,
and `windbreak/riskkernel/`.

## Conservative rounding

Every integer division on a money/price/probability path routes through
`windbreak.numeric.rounding.divide`, which requires an explicit
`RoundingDirection`:

- `OVERSTATE_COST` rounds toward positive infinity (a ceiling) — used on the
  cost/liability side, where erring high is the safe error.
- `UNDERSTATE_EQUITY` rounds toward negative infinity (a floor) — used on the
  equity/asset side, where erring low is the safe error.

Both directions are sign-safe for any operand signs. There is no bare `//` on
a money path anywhere in the codebase; every rounding decision is visible at
its call site.

## The floor formula (SPEC §10.4)

```text
worst_case_equity =
    exchange_verified_available_cash
  + guaranteed_terminal_value_of_positions
  - pending_kernel_reservations
  - unresolved_fee_upper_bounds
  - reconciliation_uncertainty_buffer

worst_case_cost (opening buy) =
    limit_price * count + max_trading_fee
  + max_settlement_fee + conservative_rounding_buffer
```

An order is approved only when
`worst_case_equity - worst_case_cost >= floor`, where `floor` is
`config.capital.floor_micros`. For closing orders, worst-case cost must be
provably non-increasing or the order is vetoed. The Risk Kernel cross-checks
its own read-only balance call against the ledger every cycle; a mismatch
beyond tolerance halts the system rather than proceeding on an unreconciled
number.

## Balance-semantics contract (SPEC §7.3)

Before live trading, an exchange adapter must publish a machine-readable
`BalanceSemantics` record proving it understands: whether open-order
collateral is included in total balance and excluded from available balance;
how trading and settlement fees are debited and rounded; how partial fills are
represented; how cancellations release collateral; how unsettled resolution
proceeds appear before crediting; how paused/halted markets behave. **The
Risk Kernel refuses live trading while any `BalanceSemantics` field is
`unknown`** — an unprovable input to the floor formula halts the system rather
than trading on a guess.

## Settlement-lag accounting (T18)

Deployable cash is exchange-verified *available* cash per the
`BalanceSemantics` mapping above; unsettled resolution proceeds are excluded
from `worst_case_equity` until the exchange actually credits them. This
prevents counting money the exchange has not yet released as spendable — the
SPEC's T18 threat (settlement-lag mis-accounting).
