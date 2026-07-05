"""Metamorphic (Hypothesis) tests for hedgekit.riskkernel.floor (issue #30, RED).

These tests generate integer inputs -- never floats, per SPEC S6.1 -- and
check *relationships* that must hold for every input, rather than pinning one
fixed expected value (that is `test_floor.py`'s job): adding any adverse
event to `worst_case_equity`'s inputs never increases the result;
`worst_case_cost` is monotone non-decreasing in every one of its inputs;
whenever `checks.floor_invariant` approves an open, the recomputed
`worst_case_equity - worst_case_cost` really is `>=` the floor; and every
result is a true `int`-backed `MoneyMicros`.

`hedgekit/riskkernel/floor.py` and `hedgekit/riskkernel/context.py` do not
exist yet, so the imports below fail collection with `ModuleNotFoundError` --
the expected Gate 1 RED state for issue #30.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from hedgekit.numeric.types import ContractCentis, MoneyMicros, PricePips
from hedgekit.riskkernel.checks import DEFAULT_CHECKS
from hedgekit.riskkernel.floor import worst_case_cost, worst_case_equity
from tests.riskkernel.conftest import make_context, make_intent

#: `floor_invariant`, looked up by its own `.name` (see `test_checks.py`'s
#: `_real_check` for the same self-describing lookup rationale).
_FLOOR_INVARIANT_CHECK = next(
    check for check in DEFAULT_CHECKS if check.name == "floor_invariant"
)

#: A representative bound for the equity/cost terms below: wide enough to
#: exercise large and negative reservations/fees/buffers, narrow enough that
#: `+ delta` perturbations never risk silent overflow-adjacent surprises.
_TERM_BOUND = 10**12

#: A non-negative "how much worse" step applied on top of a baseline term.
_delta_strategy = st.integers(min_value=0, max_value=10**9)
_term_strategy = st.integers(min_value=-_TERM_BOUND, max_value=_TERM_BOUND)
_non_negative_term_strategy = st.integers(min_value=0, max_value=_TERM_BOUND)
_price_strategy = st.integers(min_value=0, max_value=10_000)
_size_strategy = st.integers(min_value=1, max_value=100_000)


# --- worst_case_equity: adverse moves never increase the result -----------------


@given(
    cash=_term_strategy,
    terminal=_term_strategy,
    reservations=_term_strategy,
    fees=_term_strategy,
    buffer=_term_strategy,
    delta=_delta_strategy,
)
def test_decreasing_cash_never_increases_worst_case_equity(
    cash: int, terminal: int, reservations: int, fees: int, buffer: int, delta: int
) -> None:
    """Decreasing `exchange_verified_available_cash` by any amount never
    increases `worst_case_equity`."""
    baseline = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )
    adverse = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash - delta),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )

    assert adverse.value <= baseline.value


@given(
    cash=_term_strategy,
    terminal=_term_strategy,
    reservations=_term_strategy,
    fees=_term_strategy,
    buffer=_term_strategy,
    delta=_delta_strategy,
)
def test_decreasing_terminal_value_never_increases_worst_case_equity(
    cash: int, terminal: int, reservations: int, fees: int, buffer: int, delta: int
) -> None:
    """Decreasing `guaranteed_terminal_value_of_positions` by any amount
    never increases `worst_case_equity`."""
    baseline = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )
    adverse = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal - delta),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )

    assert adverse.value <= baseline.value


@given(
    cash=_term_strategy,
    terminal=_term_strategy,
    reservations=_term_strategy,
    fees=_term_strategy,
    buffer=_term_strategy,
    delta=_delta_strategy,
)
def test_increasing_reservations_never_increases_worst_case_equity(
    cash: int, terminal: int, reservations: int, fees: int, buffer: int, delta: int
) -> None:
    """Increasing `pending_kernel_reservations` by any amount never increases
    `worst_case_equity`."""
    baseline = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )
    adverse = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations + delta),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )

    assert adverse.value <= baseline.value


@given(
    cash=_term_strategy,
    terminal=_term_strategy,
    reservations=_term_strategy,
    fees=_term_strategy,
    buffer=_term_strategy,
    delta=_delta_strategy,
)
def test_increasing_fee_upper_bound_never_increases_worst_case_equity(
    cash: int, terminal: int, reservations: int, fees: int, buffer: int, delta: int
) -> None:
    """Increasing `unresolved_fee_upper_bounds` by any amount never increases
    `worst_case_equity`."""
    baseline = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )
    adverse = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees + delta),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )

    assert adverse.value <= baseline.value


@given(
    cash=_term_strategy,
    terminal=_term_strategy,
    reservations=_term_strategy,
    fees=_term_strategy,
    buffer=_term_strategy,
    delta=_delta_strategy,
)
def test_increasing_reconciliation_buffer_never_increases_worst_case_equity(
    cash: int, terminal: int, reservations: int, fees: int, buffer: int, delta: int
) -> None:
    """Increasing `reconciliation_uncertainty_buffer` by any amount never
    increases `worst_case_equity`."""
    baseline = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )
    adverse = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer + delta),
    )

    assert adverse.value <= baseline.value


# --- worst_case_cost: monotone non-decreasing in every input --------------------


@given(
    price=_price_strategy,
    size=_size_strategy,
    trading_fee=_non_negative_term_strategy,
    settlement_fee=_non_negative_term_strategy,
    buffer=_non_negative_term_strategy,
    delta=st.integers(min_value=0, max_value=1_000),
)
def test_worst_case_cost_is_monotone_non_decreasing_in_price(
    price: int,
    size: int,
    trading_fee: int,
    settlement_fee: int,
    buffer: int,
    delta: int,
) -> None:
    """Increasing `price` (all else fixed) never decreases `worst_case_cost`."""
    baseline = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )
    increased = worst_case_cost(
        PricePips(price + delta),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )

    assert increased.value >= baseline.value


@given(
    price=_price_strategy,
    size=_size_strategy,
    trading_fee=_non_negative_term_strategy,
    settlement_fee=_non_negative_term_strategy,
    buffer=_non_negative_term_strategy,
    delta=st.integers(min_value=0, max_value=1_000),
)
def test_worst_case_cost_is_monotone_non_decreasing_in_size(
    price: int,
    size: int,
    trading_fee: int,
    settlement_fee: int,
    buffer: int,
    delta: int,
) -> None:
    """Increasing `size` (all else fixed) never decreases `worst_case_cost`."""
    baseline = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )
    increased = worst_case_cost(
        PricePips(price),
        ContractCentis(size + delta),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )

    assert increased.value >= baseline.value


@given(
    price=_price_strategy,
    size=_size_strategy,
    trading_fee=_non_negative_term_strategy,
    settlement_fee=_non_negative_term_strategy,
    buffer=_non_negative_term_strategy,
    delta=_delta_strategy,
)
def test_worst_case_cost_is_monotone_non_decreasing_in_trading_fee(
    price: int,
    size: int,
    trading_fee: int,
    settlement_fee: int,
    buffer: int,
    delta: int,
) -> None:
    """Increasing `max_trading_fee` (all else fixed) never decreases the
    total cost."""
    baseline = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )
    increased = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee + delta),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )

    assert increased.value >= baseline.value


@given(
    price=_price_strategy,
    size=_size_strategy,
    trading_fee=_non_negative_term_strategy,
    settlement_fee=_non_negative_term_strategy,
    buffer=_non_negative_term_strategy,
    delta=_delta_strategy,
)
def test_worst_case_cost_is_monotone_non_decreasing_in_settlement_fee(
    price: int,
    size: int,
    trading_fee: int,
    settlement_fee: int,
    buffer: int,
    delta: int,
) -> None:
    """Increasing `max_settlement_fee` (all else fixed) never decreases the
    total cost."""
    baseline = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )
    increased = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee + delta),
        rounding_buffer=MoneyMicros(buffer),
    )

    assert increased.value >= baseline.value


@given(
    price=_price_strategy,
    size=_size_strategy,
    trading_fee=_non_negative_term_strategy,
    settlement_fee=_non_negative_term_strategy,
    buffer=_non_negative_term_strategy,
    delta=_delta_strategy,
)
def test_worst_case_cost_is_monotone_non_decreasing_in_rounding_buffer(
    price: int,
    size: int,
    trading_fee: int,
    settlement_fee: int,
    buffer: int,
    delta: int,
) -> None:
    """Increasing `rounding_buffer` (all else fixed) never decreases the
    total cost."""
    baseline = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )
    increased = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer + delta),
    )

    assert increased.value >= baseline.value


# --- floor_invariant: an approval always really satisfies the inequality --------


@given(
    cash=_non_negative_term_strategy,
    price=_price_strategy,
    size=_size_strategy,
    trading_fee=_non_negative_term_strategy,
    settlement_fee=_non_negative_term_strategy,
    floor=_term_strategy,
)
def test_floor_invariant_approval_always_satisfies_the_recomputed_inequality(
    cash: int,
    price: int,
    size: int,
    trading_fee: int,
    settlement_fee: int,
    floor: int,
) -> None:
    """Whenever `floor_invariant` approves an open, independently
    recomputing `worst_case_equity - worst_case_cost` from the same context
    really does satisfy `>= floor` -- the check's approval is never
    "optimistic" relative to the arithmetic it is meant to enforce.
    """
    context = make_context(
        floor=MoneyMicros(floor),
        exchange_verified_available_cash=MoneyMicros(cash),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(0),
    )
    intent = make_intent(
        action="buy", price=PricePips(price), size=ContractCentis(size)
    )

    result = _FLOOR_INVARIANT_CHECK(intent, context)

    if result.vetoed:
        return
    account = context.account
    equity = worst_case_equity(
        exchange_verified_available_cash=account.exchange_verified_available_cash,
        guaranteed_terminal_value_of_positions=(
            account.guaranteed_terminal_value_of_positions
        ),
        pending_kernel_reservations=account.pending_kernel_reservations,
        unresolved_fee_upper_bounds=account.unresolved_fee_upper_bounds,
        reconciliation_uncertainty_buffer=account.reconciliation_uncertainty_buffer,
    )
    cost = worst_case_cost(
        intent.price,
        intent.size,
        max_trading_fee=context.fees.max_trading_fee,
        max_settlement_fee=context.fees.max_settlement_fee,
        rounding_buffer=context.limits.rounding_buffer,
    )
    assert equity.value - cost.value >= context.limits.floor.value


# --- Both functions always return true, non-bool ints ---------------------------


@given(
    cash=_term_strategy,
    terminal=_term_strategy,
    reservations=_term_strategy,
    fees=_term_strategy,
    buffer=_term_strategy,
)
def test_worst_case_equity_result_value_is_always_a_true_int(
    cash: int, terminal: int, reservations: int, fees: int, buffer: int
) -> None:
    """`worst_case_equity(...).value` is always a true `int`, never a `bool`
    or a `float`, for any combination of integer inputs."""
    result = worst_case_equity(
        exchange_verified_available_cash=MoneyMicros(cash),
        guaranteed_terminal_value_of_positions=MoneyMicros(terminal),
        pending_kernel_reservations=MoneyMicros(reservations),
        unresolved_fee_upper_bounds=MoneyMicros(fees),
        reconciliation_uncertainty_buffer=MoneyMicros(buffer),
    )

    assert type(result.value) is int
    assert not isinstance(result.value, bool)


@given(
    price=_price_strategy,
    size=_size_strategy,
    trading_fee=_non_negative_term_strategy,
    settlement_fee=_non_negative_term_strategy,
    buffer=_non_negative_term_strategy,
)
def test_worst_case_cost_result_value_is_always_a_true_int(
    price: int, size: int, trading_fee: int, settlement_fee: int, buffer: int
) -> None:
    """`worst_case_cost(...).value` is always a true `int`, never a `bool`
    or a `float`, for any combination of integer inputs."""
    result = worst_case_cost(
        PricePips(price),
        ContractCentis(size),
        max_trading_fee=MoneyMicros(trading_fee),
        max_settlement_fee=MoneyMicros(settlement_fee),
        rounding_buffer=MoneyMicros(buffer),
    )

    assert type(result.value) is int
    assert not isinstance(result.value, bool)
