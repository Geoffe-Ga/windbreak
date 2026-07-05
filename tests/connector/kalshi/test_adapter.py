"""Tests for hedgekit.connector.kalshi.adapter (issue #17): KalshiConnector.

Acceptance test: `list_markets()` excludes every refused (non-binary)
product and ledgers a `PRODUCT_REFUSED` event for each one; `get_market`,
`get_order_book`, `get_exchange_status`, and `get_exchange_time` are backed
by the recorded fixtures; every trading/account method still raises
`NotImplementedError` (order path is M4; balances/fees are issue #3).

`hedgekit.connector.kalshi` does not exist yet, so importing `adapter` fails
collection with `ModuleNotFoundError: No module named 'hedgekit.connector.kalshi'`
-- the expected Gate 1 RED state for issue #17.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from hedgekit.connector.interface import MarketConnector, UnknownMarketError
from hedgekit.connector.kalshi import PRODUCT_REFUSED_EVENT, KalshiConnector
from hedgekit.connector.kalshi.normalize import normalize_exchange_status

if TYPE_CHECKING:
    from collections.abc import Callable

    from hedgekit.connector.kalshi.client import KalshiClient
    from hedgekit.connector.snapshot import ConnectorEvent, InMemoryEventLedgerWriter

#: (method name, positional args) for every SPEC S7.2 method this issue does
#: not implement -- order path is M4; balances/positions/fees are issue #3.
_UNIMPLEMENTED_CALLS: tuple[tuple[str, tuple[object, ...]], ...] = (
    ("place_order", (object(), object())),
    ("cancel_order", ("some-order-id",)),
    ("get_balances", ()),
    ("get_balance_semantics", ()),
    ("get_positions", ()),
    ("get_open_orders", ()),
    ("get_fills", (datetime(2024, 1, 1, tzinfo=UTC),)),
    ("get_fee_model", ("KXFED-24DEC",)),
)


class _RaisingLedgerWriter:
    """An `EventLedgerWriter` that always raises, simulating a broken ledger."""

    def record(self, event: ConnectorEvent) -> None:
        """Raise unconditionally.

        Args:
            event: The event that would have been recorded.
        """
        raise RuntimeError("ledger unavailable")


# --- list_markets: product gate + PRODUCT_REFUSED ledgering -----------------


def test_list_markets_excludes_refused_products_and_returns_only_binaries(
    kalshi_fixture_connector: KalshiConnector,
) -> None:
    """Perpetual and scalar fixture products never appear in the result."""
    markets = kalshi_fixture_connector.list_markets()

    tickers = [market.ticker for market in markets]
    assert "FAKE-PERP" not in tickers
    assert "KXSCALAR-24DEC" not in tickers
    assert "KXFED-24DEC" in tickers


def test_list_markets_ledgers_a_product_refused_event_per_refused_market(
    kalshi_fixture_connector: KalshiConnector, ledger: InMemoryEventLedgerWriter
) -> None:
    """Every refused market is ledgered exactly once, with reason and hash."""
    kalshi_fixture_connector.list_markets()

    refusals = ledger.events_by_type(PRODUCT_REFUSED_EVENT)
    refused_tickers = {event.payload["ticker"] for event in refusals}

    assert refused_tickers == {"FAKE-PERP", "KXSCALAR-24DEC"}
    for event in refusals:
        assert event.payload["reason"]
        assert event.payload["raw_exchange_payload_hash"]
        assert "event_ticker" in event.payload


def test_list_markets_wires_mutually_exclusive_group_id_from_events_fixture(
    kalshi_fixture_connector: KalshiConnector,
) -> None:
    """The event fixture's `mutually_exclusive` flag drives grouping end to end."""
    markets = {
        market.ticker: market for market in kalshi_fixture_connector.list_markets()
    }

    assert markets["KXFED-24DEC"].mutually_exclusive_group_id == "KXFED"
    assert markets["KXFED-24DEC-B75"].mutually_exclusive_group_id == "KXFED"
    assert markets["KXWEA-24DEC"].mutually_exclusive_group_id is None


def test_writer_raising_does_not_propagate_out_of_list_markets(
    fake_kalshi_client: KalshiClient,
    clock: Callable[[], datetime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken ledger writer must not crash `list_markets` (isolation)."""
    caplog.set_level(logging.DEBUG)
    connector = KalshiConnector(fake_kalshi_client, _RaisingLedgerWriter(), clock=clock)

    markets = connector.list_markets()

    assert any(market.ticker == "KXFED-24DEC" for market in markets)
    assert any("ledger" in record.getMessage().lower() for record in caplog.records)


# --- get_market --------------------------------------------------------


@pytest.mark.parametrize("ticker", ["FAKE-PERP", "NOPE"])
def test_get_market_raises_unknown_for_refused_or_absent_ticker(
    kalshi_fixture_connector: KalshiConnector, ticker: str
) -> None:
    """A refused product and an absent ticker both raise `UnknownMarketError`."""
    with pytest.raises(UnknownMarketError):
        kalshi_fixture_connector.get_market(ticker)


def test_get_market_returns_the_normalized_binary_for_a_known_ticker(
    kalshi_fixture_connector: KalshiConnector,
) -> None:
    """A known binary ticker returns its normalized market."""
    market = kalshi_fixture_connector.get_market("KXFED-24DEC")

    assert market.ticker == "KXFED-24DEC"
    assert market.market_type == "fully_collateralized_binary"


@pytest.mark.parametrize("ticker", ["KXFED-24DEC", "FAKE-PERP", "NOPE"])
def test_get_market_never_ledgers_product_refused_events(
    kalshi_fixture_connector: KalshiConnector,
    ledger: InMemoryEventLedgerWriter,
    ticker: str,
) -> None:
    """A single-ticker lookup must not ledger refusals for the whole venue.

    Only ``list_markets`` scans and refuses; ``get_market`` is a targeted
    lookup, so it emits no ``PRODUCT_REFUSED`` events regardless of whether the
    ticker is a binary, a refused product, or absent. This keeps the SPEC S7.1
    "one refusal per refused product" contract from being violated by repeated
    lookups flooding the ledger with duplicates.
    """
    with suppress(UnknownMarketError):
        kalshi_fixture_connector.get_market(ticker)

    assert ledger.events_by_type(PRODUCT_REFUSED_EVENT) == ()


# --- get_order_book ------------------------------------------------------


def test_get_order_book_raises_unknown_for_absent_ticker(
    kalshi_fixture_connector: KalshiConnector,
) -> None:
    """An unrecorded ticker raises `UnknownMarketError`, not a raw API error."""
    with pytest.raises(UnknownMarketError):
        kalshi_fixture_connector.get_order_book("NOPE")


def test_get_order_book_fetched_at_equals_the_injected_clock(
    kalshi_fixture_connector: KalshiConnector, clock: Callable[[], datetime]
) -> None:
    """`fetched_at` comes from the injected clock, not wall-clock time."""
    book = kalshi_fixture_connector.get_order_book("KXFED-24DEC")

    assert book.fetched_at == clock()


# --- get_exchange_status -------------------------------------------------


@pytest.mark.parametrize(
    ("exchange_active", "trading_active", "expected_status"),
    [
        (True, True, "open"),
        (True, False, "paused"),
        (False, True, "closed"),
        (False, False, "closed"),
    ],
)
def test_exchange_status_active_flags_map_to_the_documented_status(
    exchange_active: bool, trading_active: bool, expected_status: str
) -> None:
    """Active-flag combinations map to open/paused/closed exactly as documented."""
    raw = {"exchange_active": exchange_active, "trading_active": trading_active}

    status = normalize_exchange_status(raw, datetime(2024, 1, 1, tzinfo=UTC))

    assert status.status == expected_status


def test_get_exchange_status_fetched_at_equals_the_injected_clock(
    kalshi_fixture_connector: KalshiConnector, clock: Callable[[], datetime]
) -> None:
    """The fixture's active flags map to `"open"`; `fetched_at` is the clock."""
    status = kalshi_fixture_connector.get_exchange_status()

    assert status.fetched_at == clock()
    assert status.status == "open"


# --- get_exchange_time ---------------------------------------------------


def test_get_exchange_time_returns_the_date_header_when_present(
    kalshi_fixture_connector: KalshiConnector, kalshi_fixture_server_date: datetime
) -> None:
    """The Date response header, parsed and UTC-normalized, wins over the clock."""
    server_time = kalshi_fixture_connector.get_exchange_time()

    assert server_time == kalshi_fixture_server_date


def test_get_exchange_time_falls_back_to_clock_when_date_header_absent(
    kalshi_connector_missing_date_header: KalshiConnector,
    clock: Callable[[], datetime],
) -> None:
    """A missing `Date` header falls back to the injected clock."""
    server_time = kalshi_connector_missing_date_header.get_exchange_time()

    assert server_time == clock()


# --- unimplemented surface + protocol conformance -------------------------


@pytest.mark.parametrize(("method_name", "args"), _UNIMPLEMENTED_CALLS)
def test_unimplemented_trading_and_account_methods_raise_not_implemented(
    kalshi_fixture_connector: KalshiConnector,
    method_name: str,
    args: tuple[object, ...],
) -> None:
    """Every trading/account method not yet wired raises `NotImplementedError`."""
    method = getattr(kalshi_fixture_connector, method_name)

    with pytest.raises(NotImplementedError):
        method(*args)


def test_kalshi_connector_satisfies_the_market_connector_protocol(
    kalshi_fixture_connector: KalshiConnector,
) -> None:
    """`KalshiConnector` structurally satisfies the runtime-checkable protocol."""
    assert isinstance(kalshi_fixture_connector, MarketConnector)
