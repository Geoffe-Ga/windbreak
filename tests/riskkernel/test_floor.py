"""Failing-first tests for windbreak.riskkernel.floor (issue #30, RED).

Issue #30 gives the Risk Kernel its two worst-case arithmetic primitives:
`worst_case_equity` (cash + terminal value - reservations - fee upper bounds -
reconciliation buffer) and `worst_case_cost` (notional, rounded conservatively,
plus trading fee, settlement fee, and a rounding buffer). Both are exact
integer arithmetic over `windbreak.numeric` scaled-int types -- never a float.

`windbreak/riskkernel/floor.py` does not exist yet, so the import below fails
the whole module at collection with `ModuleNotFoundError: No module named
'windbreak.riskkernel.floor'` -- the expected Gate 1 RED state for issue #30.
Once the module exists, this file pins: the exact arithmetic (not just its
sign); that every one of the 5 equity terms and 4 cost terms independently
moves the result by exactly the expected amount per 1-unit perturbation
(catching a dropped term, a flipped sign, or a doubled term); that every
result is a true `int`-backed `MoneyMicros`, never a float; and that
`worst_case_cost` routes its notional term through `OVERSTATE_COST` rounding.
"""

from __future__ import annotations

import pytest

from windbreak.numeric import RoundingDirection
from windbreak.numeric.types import ContractCentis, MoneyMicros, PricePips
from windbreak.riskkernel.floor import worst_case_cost, worst_case_equity

# --- worst_case_equity: exact value ---------------------------------------------


def test_worst_case_equity_computes_the_exact_signed_sum() -> None:
    """`worst_case_equity` == cash + terminal - reservations - fees - buffer,
    exactly, for a fixed set of distinguishable inputs.
    """
    result = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(10_000_000),
        guaranteed_terminal_value_of_positions=MoneyMicros(2_000_000),
        pending_kernel_reservations=MoneyMicros(1_500_000),
        unresolved_fee_upper_bounds=MoneyMicros(500_000),
        reconciliation_uncertainty_buffer=MoneyMicros(250_000),
    )

    assert result == MoneyMicros(9_750_000)
    assert type(result.value) is int
    assert not isinstance(result.value, bool)


# --- worst_case_equity: ±1-micro perturbation of each of the 5 terms -----------

#: Baseline value for each `worst_case_equity` keyword argument, keyed by the
#: exact parameter name -- so a test failure names the offending parameter
#: directly, and the dict can be splatted straight into the call.
_EQUITY_BASELINE_MICROS: dict[str, int] = {
    "exchange_verified_available_cash": 10_000_000,
    "guaranteed_terminal_value_of_positions": 2_000_000,
    "pending_kernel_reservations": 1_500_000,
    "unresolved_fee_upper_bounds": 500_000,
    "reconciliation_uncertainty_buffer": 250_000,
}

#: The signed contribution of one whole unit of each term to the result: cash
#: and terminal value add; reservations, fees, and buffer subtract. A test
#: that bumps a term by +1 and sees a delta other than this exact signed 1
#: catches a dropped term, a flipped sign, or a mis-scaled term.
_EQUITY_TERM_SIGN: dict[str, int] = {
    "exchange_verified_available_cash": 1,
    "guaranteed_terminal_value_of_positions": 1,
    "pending_kernel_reservations": -1,
    "unresolved_fee_upper_bounds": -1,
    "reconciliation_uncertainty_buffer": -1,
}


@pytest.mark.parametrize("term_name", list(_EQUITY_BASELINE_MICROS))
def test_worst_case_equity_one_micro_bump_moves_result_by_its_signed_share(
    term_name: str,
) -> None:
    """Bumping any single equity term by +1 micro moves the result by exactly
    that term's signed contribution (+1 for cash/terminal, -1 for
    reservations/fees/buffer) -- every other term held fixed.
    """
    baseline_kwargs = {
        name: MoneyMicros(value) for name, value in _EQUITY_BASELINE_MICROS.items()
    }
    baseline = worst_case_equity(**baseline_kwargs)

    bumped_kwargs = {
        **baseline_kwargs,
        term_name: MoneyMicros(_EQUITY_BASELINE_MICROS[term_name] + 1),
    }
    bumped = worst_case_equity(**bumped_kwargs)

    assert bumped.value - baseline.value == _EQUITY_TERM_SIGN[term_name]


# --- worst_case_cost: exact value ------------------------------------------------


def test_worst_case_cost_computes_the_exact_sum_of_notional_and_fees() -> None:
    """`worst_case_cost` == notional (price.value * size.value, exact) +
    trading fee + settlement fee + rounding buffer, exactly.
    """
    result = worst_case_cost(
        PricePips(2_500),
        ContractCentis(400),
        max_trading_fee=MoneyMicros(300_000),
        max_settlement_fee=MoneyMicros(150_000),
        rounding_buffer=MoneyMicros(75_000),
    )

    # notional = 2_500 * 400 = 1_000_000 micros (exact: 1e-4 * 1e-2 == 1e-6).
    assert result == MoneyMicros(1_525_000)
    assert type(result.value) is int
    assert not isinstance(result.value, bool)


# --- worst_case_cost: ±1-unit perturbation of each of the 4 cost terms ---------


def test_worst_case_cost_one_pip_price_bump_moves_result_by_size_value() -> None:
    """With size fixed at 1 centi, bumping price by 1 pip moves the notional
    (and so the total cost) by exactly 1 micro -- the smallest observable
    step, isolating the price term from the size term.
    """
    size = ContractCentis(1)
    baseline = worst_case_cost(
        PricePips(5_000),
        size,
        max_trading_fee=MoneyMicros(0),
        max_settlement_fee=MoneyMicros(0),
        rounding_buffer=MoneyMicros(0),
    )
    bumped = worst_case_cost(
        PricePips(5_001),
        size,
        max_trading_fee=MoneyMicros(0),
        max_settlement_fee=MoneyMicros(0),
        rounding_buffer=MoneyMicros(0),
    )

    assert bumped.value - baseline.value == 1


def test_worst_case_cost_one_centi_size_bump_moves_result_by_price_value() -> None:
    """With price fixed at 1 pip, bumping size by 1 centi moves the notional
    by exactly 1 micro, isolating the size term from the price term.
    """
    price = PricePips(1)
    baseline = worst_case_cost(
        price,
        ContractCentis(1_000),
        max_trading_fee=MoneyMicros(0),
        max_settlement_fee=MoneyMicros(0),
        rounding_buffer=MoneyMicros(0),
    )
    bumped = worst_case_cost(
        price,
        ContractCentis(1_001),
        max_trading_fee=MoneyMicros(0),
        max_settlement_fee=MoneyMicros(0),
        rounding_buffer=MoneyMicros(0),
    )

    assert bumped.value - baseline.value == 1


def test_worst_case_cost_one_micro_trading_fee_bump_moves_result_by_one() -> None:
    """Bumping `max_trading_fee` by 1 micro moves the total cost by exactly 1
    micro, with price/size/settlement/buffer held fixed.
    """
    kwargs = {
        "max_trading_fee": MoneyMicros(300_000),
        "max_settlement_fee": MoneyMicros(150_000),
        "rounding_buffer": MoneyMicros(75_000),
    }
    baseline = worst_case_cost(PricePips(100), ContractCentis(100), **kwargs)
    bumped = worst_case_cost(
        PricePips(100),
        ContractCentis(100),
        **{**kwargs, "max_trading_fee": MoneyMicros(300_001)},
    )

    assert bumped.value - baseline.value == 1


def test_worst_case_cost_one_micro_settlement_fee_bump_moves_result_by_one() -> None:
    """Bumping `max_settlement_fee` by 1 micro moves the total cost by
    exactly 1 micro, with price/size/trading fee/buffer held fixed.
    """
    kwargs = {
        "max_trading_fee": MoneyMicros(300_000),
        "max_settlement_fee": MoneyMicros(150_000),
        "rounding_buffer": MoneyMicros(75_000),
    }
    baseline = worst_case_cost(PricePips(100), ContractCentis(100), **kwargs)
    bumped = worst_case_cost(
        PricePips(100),
        ContractCentis(100),
        **{**kwargs, "max_settlement_fee": MoneyMicros(150_001)},
    )

    assert bumped.value - baseline.value == 1


def test_worst_case_cost_one_micro_rounding_buffer_bump_moves_result_by_one() -> None:
    """Bumping `rounding_buffer` by 1 micro moves the total cost by exactly 1
    micro, with price/size/trading fee/settlement fee held fixed.
    """
    kwargs = {
        "max_trading_fee": MoneyMicros(300_000),
        "max_settlement_fee": MoneyMicros(150_000),
        "rounding_buffer": MoneyMicros(75_000),
    }
    baseline = worst_case_cost(PricePips(100), ContractCentis(100), **kwargs)
    bumped = worst_case_cost(
        PricePips(100),
        ContractCentis(100),
        **{**kwargs, "rounding_buffer": MoneyMicros(75_001)},
    )

    assert bumped.value - baseline.value == 1


# --- worst_case_cost: rounds its notional term OVERSTATE_COST -------------------


def test_worst_case_cost_computes_notional_with_overstate_cost_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`worst_case_cost` computes its notional term via
    `money_from_price_and_count(price, size, rounding=OVERSTATE_COST)` --
    never `UNDERSTATE_EQUITY` -- since a cost must never be understated.

    Assumes `floor.py` imports `money_from_price_and_count` directly at
    module scope (`from windbreak.numeric import ... money_from_price_and_count`),
    matching `windbreak.connector.fees`'s own "import the function, call it
    bare" convention (see that module's `from windbreak.numeric import
    RoundingDirection, divide`), so it is spyable at
    `windbreak.riskkernel.floor.money_from_price_and_count`.
    """
    calls: list[tuple[PricePips, ContractCentis, RoundingDirection]] = []

    def _spy(
        price: PricePips, size: ContractCentis, *, rounding: RoundingDirection
    ) -> MoneyMicros:
        calls.append((price, size, rounding))
        return MoneyMicros(0)

    monkeypatch.setattr("windbreak.riskkernel.floor.money_from_price_and_count", _spy)
    price = PricePips(2_500)
    size = ContractCentis(400)

    result = worst_case_cost(
        price,
        size,
        max_trading_fee=MoneyMicros(500_000),
        max_settlement_fee=MoneyMicros(0),
        rounding_buffer=MoneyMicros(0),
    )

    assert calls == [(price, size, RoundingDirection.OVERSTATE_COST)]
    assert result == MoneyMicros(500_000)
