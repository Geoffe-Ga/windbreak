"""Tests for windbreak.connector.models (issue #16): the SPEC S6.2 frozen models.

Pins `NormalizedMarket.__post_init__` validation (market_type, jurisdiction
enum, tick/min-order integrality and positivity, non-empty payload hash),
immutability, and `market_to_payload`'s JSON-safety (no float leaf anywhere,
datetimes rendered as ISO-8601 `Z` strings). `windbreak/connector/` does not
exist yet, so importing `windbreak.connector.models` fails collection with
`ModuleNotFoundError: No module named 'windbreak.connector'` -- the expected
Gate 1 RED state for issue #16.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from windbreak.connector.models import NormalizedMarket, market_to_payload

_VALID_KWARGS: dict[str, object] = {
    "exchange": "fake-exchange",
    "ticker": "KXFED-24DEC",
    "event_ticker": "KXFED-24",
    "title": "Fed raises rates in December 2024?",
    "resolution_criteria": "Resolves YES if the FOMC raises rates.",
    "category": "economics",
    "close_time": datetime(2024, 12, 18, 19, tzinfo=UTC),
    "expected_resolution_time": None,
    "market_type": "fully_collateralized_binary",
    "price_tick_pips": 100,
    "min_order_contract_centis": 100,
    "fractional_trading_enabled": False,
    "mutually_exclusive_group_id": None,
    "jurisdiction_status": "eligible",
    "raw_exchange_payload_hash": "sha256:abc123",
}


def _market(**overrides: object) -> NormalizedMarket:
    return NormalizedMarket(**{**_VALID_KWARGS, **overrides})


def test_valid_market_constructs_without_error() -> None:
    market = _market()

    assert market.ticker == "KXFED-24DEC"
    assert market.jurisdiction_status == "eligible"


def test_bad_market_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="market_type"):
        _market(market_type="binary")


def test_bad_jurisdiction_status_raises_value_error() -> None:
    with pytest.raises(ValueError, match="jurisdiction_status"):
        _market(jurisdiction_status="maybe")


@pytest.mark.parametrize("field", ["price_tick_pips", "min_order_contract_centis"])
def test_bool_tick_or_min_order_raises_type_error(field: str) -> None:
    """A stray `bool` (an `int` subclass) must never masquerade as a tick size."""
    with pytest.raises(TypeError):
        _market(**{field: True})


@pytest.mark.parametrize("field", ["price_tick_pips", "min_order_contract_centis"])
def test_non_int_tick_or_min_order_raises_type_error(field: str) -> None:
    with pytest.raises(TypeError):
        _market(**{field: "100"})


@pytest.mark.parametrize("field", ["price_tick_pips", "min_order_contract_centis"])
@pytest.mark.parametrize("bad_value", [0, -1])
def test_non_positive_tick_or_min_order_raises_value_error(
    field: str, bad_value: int
) -> None:
    with pytest.raises(ValueError, match=field):
        _market(**{field: bad_value})


def test_empty_payload_hash_raises_value_error() -> None:
    with pytest.raises(ValueError, match="raw_exchange_payload_hash"):
        _market(raw_exchange_payload_hash="")


def test_market_is_frozen() -> None:
    market = _market()

    with pytest.raises(dataclasses.FrozenInstanceError):
        market.ticker = "OTHER"  # type: ignore[misc]


# --- market_to_payload: JSON-safety -----------------------------------------------


def test_market_to_payload_is_json_dumps_clean() -> None:
    market = _market(expected_resolution_time=datetime(2024, 12, 18, 20, tzinfo=UTC))

    payload = market_to_payload(market)

    assert json.loads(json.dumps(payload)) == payload


def test_market_to_payload_datetimes_are_iso_z_strings() -> None:
    market = _market(expected_resolution_time=datetime(2024, 12, 18, 20, tzinfo=UTC))

    payload = market_to_payload(market)

    assert payload["close_time"] == "2024-12-18T19:00:00.000000Z"
    assert payload["expected_resolution_time"] == "2024-12-18T20:00:00.000000Z"


def test_market_to_payload_handles_none_expected_resolution_time() -> None:
    market = _market(expected_resolution_time=None)

    payload = market_to_payload(market)

    assert payload["expected_resolution_time"] is None


def test_market_to_payload_preserves_plain_int_unit_fields() -> None:
    market = _market(price_tick_pips=250, min_order_contract_centis=500)

    payload = market_to_payload(market)

    assert payload["price_tick_pips"] == 250
    assert payload["min_order_contract_centis"] == 500
    assert type(payload["price_tick_pips"]) is int
    assert type(payload["min_order_contract_centis"]) is int


def _assert_no_float_leaf(node: object) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _assert_no_float_leaf(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _assert_no_float_leaf(item)
    else:
        assert type(node) is not float, f"float leaf found in payload: {node!r}"


def test_market_to_payload_contains_no_float_leaf() -> None:
    market = _market()

    payload = market_to_payload(market)

    _assert_no_float_leaf(payload)
