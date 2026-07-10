"""Failing-first tests for windbreak.drills.exchanges (issue #59, RED).

`windbreak.drills.exchanges` does not exist yet, so the import below fails
collection with `ModuleNotFoundError: No module named
'windbreak.drills.exchanges'` -- the expected Gate 1 RED state for issue #59.

Pins the `HeldPositionsExchange`'s core safety invariant for the kill/ratchet
drills: `cancel_order` removes an order from the open-orders book only, never
touches positions, and the class exposes no withdraw/transfer/move_funds
surface at all -- "cannot move funds" is structural, not merely untested.

Design assumption (flagged for the implementer): `HeldPositionsExchange` is
constructed as `HeldPositionsExchange(open_orders=<tuple[OpenOrder, ...]>,
positions=<tuple[Position, ...]>)`, reusing
`windbreak.connector.models.OpenOrder`/`Position` verbatim rather than
minting parallel drill-only types.
"""

from __future__ import annotations

from windbreak.connector.models import OpenOrder, Position
from windbreak.drills.exchanges import HeldPositionsExchange
from windbreak.numeric.types import ContractCentis, PricePips

#: The market ticker every helper below defaults to, matching the ticker
#: convention used across `tests/riskkernel/conftest.py`.
_TICKER = "PRES-2028-DEM"


def _open_order(order_id: str, ticker: str = _TICKER) -> OpenOrder:
    """Build a representative resting order for the tests below."""
    return OpenOrder(
        id=order_id,
        ticker=ticker,
        side="yes",
        price=PricePips(5000),
        quantity=ContractCentis(100),
    )


def _position(ticker: str = _TICKER, quantity: int = 500) -> Position:
    """Build a representative held position for the tests below."""
    return Position(
        ticker=ticker,
        quantity=ContractCentis(quantity),
        average_price=PricePips(5000),
    )


def test_get_open_orders_and_get_positions_reflect_the_seeded_state() -> None:
    """A freshly seeded exchange reports exactly its seeded orders and
    positions."""
    order = _open_order("order-1")
    position = _position()
    exchange = HeldPositionsExchange(open_orders=(order,), positions=(position,))

    assert exchange.get_open_orders() == (order,)
    assert exchange.get_positions() == (position,)


def test_cancel_order_removes_only_the_matching_open_order() -> None:
    """Canceling one of several open orders removes exactly that one,
    leaving every other order and every position untouched.
    """
    kept = _open_order("order-keep")
    canceled = _open_order("order-cancel")
    position = _position()
    exchange = HeldPositionsExchange(
        open_orders=(kept, canceled), positions=(position,)
    )

    exchange.cancel_order("order-cancel")

    assert exchange.get_open_orders() == (kept,)
    assert exchange.get_positions() == (position,)


def test_cancel_order_never_mutates_positions_even_when_tickers_match() -> None:
    """Canceling an order never touches positions, even when the canceled
    order's ticker exactly matches a held position's ticker -- cancellation
    is order-book-only, position-hold is absolute.
    """
    order = _open_order("order-1", ticker=_TICKER)
    position = _position(ticker=_TICKER, quantity=500)
    exchange = HeldPositionsExchange(open_orders=(order,), positions=(position,))

    exchange.cancel_order("order-1")

    assert exchange.get_positions() == (position,)
    assert exchange.get_open_orders() == ()


def test_cancel_order_on_an_unknown_id_is_a_no_op() -> None:
    """Canceling an id with no matching open order changes nothing."""
    order = _open_order("order-1")
    exchange = HeldPositionsExchange(open_orders=(order,), positions=())

    exchange.cancel_order("no-such-order")

    assert exchange.get_open_orders() == (order,)


def test_get_open_orders_ordering_is_deterministic_across_repeated_calls() -> None:
    """Repeated `get_open_orders()` calls return the identical ordering."""
    orders = tuple(_open_order(f"order-{i}") for i in range(5))
    exchange = HeldPositionsExchange(open_orders=orders, positions=())

    first = exchange.get_open_orders()
    second = exchange.get_open_orders()

    assert first == second == orders


def test_held_positions_exchange_has_no_fund_movement_methods() -> None:
    """`HeldPositionsExchange` has no `withdraw`/`transfer`/`move_funds`
    attribute at all -- "cannot move funds" is a structural absence, not
    merely an untested capability.
    """
    exchange = HeldPositionsExchange(open_orders=(), positions=())

    assert not hasattr(exchange, "withdraw")
    assert not hasattr(exchange, "transfer")
    assert not hasattr(exchange, "move_funds")
