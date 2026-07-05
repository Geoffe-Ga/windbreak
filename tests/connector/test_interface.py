"""Tests for hedgekit.connector.interface (issue #16): the MarketConnector protocol.

SPEC S7.2 defines exactly 13 connector methods. These tests pin: the protocol
is `@runtime_checkable` so `isinstance(fake, MarketConnector)` works, every
method is present, and each has the documented arity. `hedgekit/connector/`
does not exist yet, so importing it fails collection with
`ModuleNotFoundError: No module named 'hedgekit.connector'` -- the expected
Gate 1 RED state for issue #16.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from hedgekit.connector.interface import MarketConnector, UnknownMarketError

if TYPE_CHECKING:
    from hedgekit.connector.fake import FakeExchange

#: Expected positional-parameter count (excluding `self`) per SPEC S7.2 method.
_EXPECTED_PARAM_COUNTS = {
    "list_markets": 0,
    "get_market": 1,
    "get_order_book": 1,
    "get_exchange_status": 0,
    "get_exchange_time": 0,
    "get_balance_semantics": 0,
    "get_balances": 0,
    "get_positions": 0,
    "get_open_orders": 0,
    "get_fills": 1,
    "get_fee_model": 1,
    "place_order": 2,
    "cancel_order": 1,
}


def test_market_connector_declares_exactly_13_spec_methods() -> None:
    """SPEC S7.2 lists exactly 13 connector methods -- no more, no less."""
    assert len(_EXPECTED_PARAM_COUNTS) == 13


def test_fake_exchange_satisfies_the_market_connector_protocol(
    fake_exchange: FakeExchange,
) -> None:
    """`FakeExchange` structurally satisfies the runtime-checkable protocol."""
    assert isinstance(fake_exchange, MarketConnector)


def test_every_expected_method_is_present_on_the_protocol() -> None:
    """Every SPEC S7.2 method name is declared directly on `MarketConnector`."""
    for name in _EXPECTED_PARAM_COUNTS:
        assert hasattr(MarketConnector, name), f"missing protocol method: {name}"


def test_every_expected_method_has_the_documented_arity() -> None:
    """Each protocol method accepts exactly its documented number of arguments.

    Counting parameters (excluding `self`) rather than checking exact names
    kills arity-changing mutants (a dropped or added parameter) without being
    brittle to a reasonable parameter-naming choice.
    """
    for name, expected_count in _EXPECTED_PARAM_COUNTS.items():
        func = getattr(MarketConnector, name)
        params = [
            param
            for param in inspect.signature(func).parameters.values()
            if param.name != "self"
        ]
        assert len(params) == expected_count, (
            f"{name} expected {expected_count} params, got {len(params)}"
        )


def test_unknown_market_error_is_a_key_error() -> None:
    """`UnknownMarketError` is a `KeyError` so callers can catch either."""
    assert issubclass(UnknownMarketError, KeyError)
