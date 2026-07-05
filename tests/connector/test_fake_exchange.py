"""Tests for hedgekit.connector.fake.FakeExchange (issue #16).

`FakeExchange` implements the full `MarketConnector` protocol from
`tests/fixtures/exchange/*.json`, wrapping every arithmetic-bearing value
(order-book prices/quantities, balances) in hedgekit's scaled-integer unit
types at load time. `hedgekit/connector/` does not exist yet, so importing it
fails collection with `ModuleNotFoundError: No module named
'hedgekit.connector'` -- the expected Gate 1 RED state for issue #16.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from hedgekit.connector.interface import UnknownMarketError
from hedgekit.connector.models import NormalizedMarket
from hedgekit.numeric import ContractCentis, MoneyMicros, PricePips

if TYPE_CHECKING:
    from pathlib import Path

    from hedgekit.connector.fake import FakeExchange

_ALL_FIXTURE_FILES = [
    "markets.json",
    "order_books.json",
    "exchange.json",
    "balances.json",
    "positions.json",
    "open_orders.json",
    "fills.json",
    "fee_models.json",
    "balance_semantics.json",
]


# --- The issue's worked example: KXFED-24DEC ------------------------------------


def test_kxfed_market_matches_the_issue_example(fake_exchange: FakeExchange) -> None:
    """KXFED-24DEC exercises every SPEC S6.2 field on a jurisdiction-eligible market."""
    market = fake_exchange.get_market("KXFED-24DEC")

    assert market.market_type == "fully_collateralized_binary"
    assert market.jurisdiction_status in ("eligible", "ineligible", "unknown")
    assert market.jurisdiction_status == "eligible"
    assert isinstance(market.price_tick_pips, int)
    assert isinstance(market.min_order_contract_centis, int)
    assert market.mutually_exclusive_group_id == "KXFED-24-GROUP"
    assert market.raw_exchange_payload_hash != ""


def test_get_market_unknown_ticker_raises_unknown_market_error(
    fake_exchange: FakeExchange,
) -> None:
    """An unrecognized ticker raises `UnknownMarketError`, not a bare `KeyError`."""
    with pytest.raises(UnknownMarketError):
        fake_exchange.get_market("NOT-A-REAL-TICKER")


def test_get_order_book_unknown_ticker_raises_unknown_market_error(
    fake_exchange: FakeExchange,
) -> None:
    with pytest.raises(UnknownMarketError):
        fake_exchange.get_order_book("NOT-A-REAL-TICKER")


def test_list_markets_returns_all_three_fixture_markets(
    fake_exchange: FakeExchange,
) -> None:
    tickers = {market.ticker for market in fake_exchange.list_markets()}

    assert tickers == {"KXFED-24DEC", "KXBAN-24DEC", "KXWEA-24DEC"}


# --- Order book: prices and quantities are unit-wrapped -------------------------


def test_order_book_levels_are_wrapped_in_the_scaled_integer_unit_types(
    fake_exchange: FakeExchange,
) -> None:
    book = fake_exchange.get_order_book("KXFED-24DEC")

    assert book.ticker == "KXFED-24DEC"
    assert book.yes_bids[0].price == PricePips(4500)
    assert book.yes_bids[0].quantity == ContractCentis(500)
    assert book.yes_asks[0].price == PricePips(4600)
    assert book.yes_asks[0].quantity == ContractCentis(300)
    assert isinstance(book.yes_bids[0].price, PricePips)
    assert isinstance(book.yes_bids[0].quantity, ContractCentis)


def test_order_book_for_market_with_no_liquidity_is_empty(
    fake_exchange: FakeExchange,
) -> None:
    book = fake_exchange.get_order_book("KXBAN-24DEC")

    assert book.yes_bids == ()
    assert book.yes_asks == ()


# --- Fills: since-filtering and unit wrapping ------------------------------------


def test_get_fills_filters_strictly_after_since(fake_exchange: FakeExchange) -> None:
    since = datetime(2024, 11, 10, tzinfo=UTC)

    fills = fake_exchange.get_fills(since)

    assert [fill.id for fill in fills] == ["fill-2", "fill-3"]


def test_get_fills_excludes_a_fill_whose_ts_equals_since(
    fake_exchange: FakeExchange,
) -> None:
    """`since` is exclusive: a fill whose ts equals `since` is not returned."""
    since = datetime(2024, 11, 15, tzinfo=UTC)  # exactly fill-2's timestamp

    fills = fake_exchange.get_fills(since)

    assert [fill.id for fill in fills] == ["fill-3"]


def test_get_fills_since_after_every_fill_returns_nothing(
    fake_exchange: FakeExchange,
) -> None:
    since = datetime(2025, 1, 1, tzinfo=UTC)

    assert fake_exchange.get_fills(since) == ()


def test_fills_price_and_quantity_are_unit_wrapped(fake_exchange: FakeExchange) -> None:
    since = datetime(2024, 1, 1, tzinfo=UTC)

    fills = fake_exchange.get_fills(since)

    first = fills[0]
    assert isinstance(first.price, PricePips)
    assert isinstance(first.quantity, ContractCentis)
    assert first.price == PricePips(4500)
    assert first.quantity == ContractCentis(100)


# --- Balances, semantics, status, time -------------------------------------------


def test_get_balance_semantics_is_the_all_unknown_stub(
    fake_exchange: FakeExchange,
) -> None:
    semantics = fake_exchange.get_balance_semantics()

    assert semantics.collateral_in_total == "unknown"
    assert semantics.collateral_excluded_from_available == "unknown"
    assert semantics.fee_debited_at_execution == "unknown"
    assert semantics.partial_fills_represented == "unknown"
    assert semantics.cancel_releases_collateral == "unknown"
    assert semantics.unsettled_proceeds_visible == "unknown"


def test_get_balances_wraps_amounts_in_money_micros(
    fake_exchange: FakeExchange,
) -> None:
    balances = fake_exchange.get_balances()

    assert balances.total == MoneyMicros(100_000_000)
    assert balances.available == MoneyMicros(95_000_000)
    assert isinstance(balances.total, MoneyMicros)


def test_get_exchange_status_is_deterministic_from_fixture(
    fake_exchange: FakeExchange,
) -> None:
    first = fake_exchange.get_exchange_status()
    second = fake_exchange.get_exchange_status()

    assert first.status == "open"
    assert first.status == second.status


def test_get_exchange_time_is_deterministic_from_fixture(
    fake_exchange: FakeExchange,
) -> None:
    first = fake_exchange.get_exchange_time()
    second = fake_exchange.get_exchange_time()

    assert first == second
    assert first == datetime(2024, 12, 1, tzinfo=UTC)


# --- Fee models -------------------------------------------------------------------


def test_get_fee_model_looks_up_by_ticker(fake_exchange: FakeExchange) -> None:
    fee_model = fake_exchange.get_fee_model("KXFED-24DEC")

    assert fee_model.schedule_id == "kxfed-promo-v1"
    assert fee_model.maker_fee_ppm == 0
    assert fee_model.taker_fee_ppm == 35_000
    assert isinstance(fee_model.taker_fee_ppm, int)


def test_get_fee_model_falls_back_to_default_for_unlisted_ticker(
    fake_exchange: FakeExchange,
) -> None:
    fee_model = fake_exchange.get_fee_model("KXWEA-24DEC")

    assert fee_model.schedule_id == "standard-v1"
    assert fee_model.taker_fee_ppm == 70_000


# --- Positions and open orders ----------------------------------------------------


def test_get_positions_returns_the_fixture_position(
    fake_exchange: FakeExchange,
) -> None:
    positions = fake_exchange.get_positions()

    assert isinstance(positions, tuple)
    assert len(positions) == 1
    assert positions[0].ticker == "KXFED-24DEC"
    assert positions[0].quantity == ContractCentis(500)
    assert positions[0].average_price == PricePips(4550)


def test_get_open_orders_returns_the_empty_fixture(fake_exchange: FakeExchange) -> None:
    open_orders = fake_exchange.get_open_orders()

    assert open_orders == ()


# --- Not-yet-implemented order actions --------------------------------------------


def test_place_order_raises_not_implemented(fake_exchange: FakeExchange) -> None:
    with pytest.raises(NotImplementedError):
        fake_exchange.place_order(object(), object())


def test_cancel_order_raises_not_implemented(fake_exchange: FakeExchange) -> None:
    with pytest.raises(NotImplementedError):
        fake_exchange.cancel_order("some-order-id")


# --- Fixture hygiene: no float leaf anywhere, every market round-trips -----------


def _walk_no_float(node: object) -> None:
    """Recursively assert that no value in `node` is a bare float."""
    if isinstance(node, dict):
        for value in node.values():
            _walk_no_float(value)
    elif isinstance(node, list):
        for item in node:
            _walk_no_float(item)
    else:
        assert type(node) is not float, f"float leaf found: {node!r}"


@pytest.mark.parametrize("filename", _ALL_FIXTURE_FILES)
def test_fixture_files_contain_no_float_leaf(fixture_dir: Path, filename: str) -> None:
    payload = json.loads((fixture_dir / filename).read_text(encoding="utf-8"))

    _walk_no_float(payload)


def test_every_market_fixture_round_trips_through_normalized_market(
    fake_exchange: FakeExchange,
) -> None:
    """Every loaded market is a valid, constructible NormalizedMarket."""
    markets = fake_exchange.list_markets()

    assert markets
    for market in markets:
        assert isinstance(market, NormalizedMarket)
