"""Tests for windbreak.connector.snapshot (issues #16, #106): the snapshot task.

Acceptance test: `MarketSnapshotTask.run_once()` records exactly one
MARKET_SNAPSHOT and one SCREEN_DECISION event per market on
`connector.list_markets()`.

Issue #106 rewires the task onto the real `windbreak.screener.Screener`: the
task's `MarketScreener` seam becomes `screen_book(market, order_book) ->
ScreenResult`, and the real `Screener` -- not the task -- is the single
`SCREEN_DECISION` emitter. The `snapshot_task` fixture (see
`tests/connector/conftest.py`) now wires a real `Screener(ScreenerConfig(),
in_memory_ledger, clock=snapshot_clock)` sharing the task's ledger writer, with
a fixed clock (2024-12-10) chosen so the fixture markets' December-2024 close
times sit inside the default `horizon_days` window.

Until `MarketSnapshotTask` calls `screen_book` (it still calls the old
single-argument `screen(market)`, which the real `Screener` does not expose --
it requires `stats` too), `run_once()` raises `TypeError` on the first market:
the expected Gate 1 RED state for issue #106.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from windbreak.config import ScreenerConfig
from windbreak.connector.models import market_to_payload
from windbreak.connector.snapshot import (
    MARKET_SNAPSHOT_EVENT,
    SCREEN_DECISION_EVENT,
    LoggingEventLedgerWriter,
    MarketSnapshotTask,
)
from windbreak.screener import Screener
from windbreak.screener.filters import (
    CATEGORY_BLOCKLIST,
    HORIZON_DAYS,
    MIN_DEPTH,
    MIN_VOLUME_24H,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    import pytest

    from windbreak.connector.fake import FakeExchange
    from windbreak.connector.snapshot import ConnectorEvent, InMemoryEventLedgerWriter

_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

#: The canonical filter-name order `blocked_by` respects.
_CANONICAL_ORDER = (CATEGORY_BLOCKLIST, MIN_VOLUME_24H, MIN_DEPTH, HORIZON_DAYS)


class _RaisingWriter:
    """A fake EventLedgerWriter that always raises, simulating a broken ledger."""

    def record(self, event: ConnectorEvent) -> None:
        """Raise unconditionally."""
        raise RuntimeError("ledger unavailable")


def test_run_once_records_one_snapshot_and_one_decision_per_market(
    snapshot_task: MarketSnapshotTask,
    fake_exchange: FakeExchange,
    in_memory_ledger: InMemoryEventLedgerWriter,
) -> None:
    """Exactly one MARKET_SNAPSHOT and one SCREEN_DECISION land per market.

    Pins the "no double-emit" contract now that the real `Screener` (not the
    task) is the single `SCREEN_DECISION` emitter.
    """
    snapshot_task.run_once()

    market_count = len(fake_exchange.list_markets())
    snapshots = in_memory_ledger.events_by_type(MARKET_SNAPSHOT_EVENT)
    decisions = in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)

    assert len(snapshots) == market_count
    assert len(decisions) == market_count


def test_every_screen_decision_payload_has_the_canonical_shape(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    """Each SCREEN_DECISION has exactly `ticker`, `eligible`, `blocked_by`, `filters`.

    The old `decision`/`reason` shape (StubScreener, jurisdiction-only) is
    gone; the real Screener's four-filter shape has no jurisdiction filter.
    """
    snapshot_task.run_once()

    decisions = in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)

    assert decisions
    for event in decisions:
        assert set(event.payload) == {"ticker", "eligible", "blocked_by", "filters"}
        assert set(event.payload["filters"]) == set(_CANONICAL_ORDER)
        assert "decision" not in event.payload
        assert "reason" not in event.payload


def test_below_threshold_volume_market_lists_min_volume_24h_micros_in_blocked_by(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    """KXBAN-24DEC's fixture volume sits below the default 5,000,000,000 floor."""
    snapshot_task.run_once()

    decisions = {
        event.payload["ticker"]: event.payload
        for event in in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)
    }

    assert MIN_VOLUME_24H in decisions["KXBAN-24DEC"]["blocked_by"]
    assert decisions["KXBAN-24DEC"]["filters"][MIN_VOLUME_24H]["passed"] is False


def test_above_threshold_volume_markets_pass_the_volume_filter(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    """KXFED-24DEC and KXWEA-24DEC both sit above the default volume floor."""
    snapshot_task.run_once()

    decisions = {
        event.payload["ticker"]: event.payload
        for event in in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)
    }

    assert decisions["KXFED-24DEC"]["filters"][MIN_VOLUME_24H]["passed"] is True
    assert decisions["KXWEA-24DEC"]["filters"][MIN_VOLUME_24H]["passed"] is True


def test_screen_decision_measured_volume_matches_the_market_fixture_field(
    snapshot_task: MarketSnapshotTask,
    fake_exchange: FakeExchange,
    in_memory_ledger: InMemoryEventLedgerWriter,
) -> None:
    """The ledgered measured volume is exactly the fixture's `volume_24h_micros`."""
    snapshot_task.run_once()

    decisions = {
        event.payload["ticker"]: event.payload
        for event in in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)
    }

    for ticker, market in fake_exchange.markets.items():
        measured = decisions[ticker]["filters"][MIN_VOLUME_24H]["measured"]
        assert measured == market.volume_24h_micros


def test_screen_decision_measured_depth_matches_the_order_book_fixture_min_side(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    """The ledgered measured depth is `min(sum(yes_bids), sum(yes_asks))`.

    KXFED-24DEC's fixture book sums to 1500 (bids) and 1100 (asks); KXBAN-24DEC
    has no resting liquidity at all; KXWEA-24DEC sums to 200 on both sides.
    """
    snapshot_task.run_once()

    decisions = {
        event.payload["ticker"]: event.payload
        for event in in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)
    }

    assert decisions["KXFED-24DEC"]["filters"][MIN_DEPTH]["measured"] == 1100
    assert decisions["KXBAN-24DEC"]["filters"][MIN_DEPTH]["measured"] == 0
    assert decisions["KXWEA-24DEC"]["filters"][MIN_DEPTH]["measured"] == 200
    for ticker in ("KXFED-24DEC", "KXBAN-24DEC", "KXWEA-24DEC"):
        assert decisions[ticker]["filters"][MIN_DEPTH]["passed"] is False


def test_screen_decision_measured_horizon_matches_whole_days_vs_the_injected_clock(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    """The ledgered horizon is the whole-day floor vs the fixed 2024-12-10 clock.

    KXFED-24DEC closes 2024-12-18T19:00Z (8 days, 19h out -> floor 8); both
    KXBAN-24DEC and KXWEA-24DEC close 2024-12-31T05:00Z (21 days, 5h out ->
    floor 21). Both sit inside the default `[2, 120]` window.
    """
    snapshot_task.run_once()

    decisions = {
        event.payload["ticker"]: event.payload
        for event in in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)
    }

    assert decisions["KXFED-24DEC"]["filters"][HORIZON_DAYS]["measured"] == 8
    assert decisions["KXBAN-24DEC"]["filters"][HORIZON_DAYS]["measured"] == 21
    assert decisions["KXWEA-24DEC"]["filters"][HORIZON_DAYS]["measured"] == 21
    for ticker in ("KXFED-24DEC", "KXBAN-24DEC", "KXWEA-24DEC"):
        assert decisions[ticker]["filters"][HORIZON_DAYS]["passed"] is True


def test_market_snapshot_payload_matches_market_to_payload(
    snapshot_task: MarketSnapshotTask,
    fake_exchange: FakeExchange,
    in_memory_ledger: InMemoryEventLedgerWriter,
) -> None:
    """The recorded MARKET_SNAPSHOT payload is exactly `market_to_payload`'s output."""
    snapshot_task.run_once()

    snapshots = {
        event.payload["ticker"]: event.payload
        for event in in_memory_ledger.events_by_type(MARKET_SNAPSHOT_EVENT)
    }
    kxfed = fake_exchange.get_market("KXFED-24DEC")

    assert snapshots["KXFED-24DEC"] == market_to_payload(kxfed)


def test_every_recorded_event_has_an_iso_utc_timestamp(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    """Every recorded event's `ts` is ISO-8601 UTC with a trailing `Z`."""
    snapshot_task.run_once()

    all_events = in_memory_ledger.events_by_type(
        MARKET_SNAPSHOT_EVENT
    ) + in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)

    assert all_events
    for event in all_events:
        assert _ISO_UTC.match(event.ts), f"not ISO-Z: {event.ts!r}"


def test_logging_event_ledger_writer_logs_each_event(
    fake_exchange: FakeExchange,
    snapshot_clock: Callable[[], datetime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real `Screener` and the task, sharing one `LoggingEventLedgerWriter`.

    Both event types must appear in the log once the task calls `screen_book`.
    """
    caplog.set_level(logging.INFO)
    writer = LoggingEventLedgerWriter()
    screener = Screener(ScreenerConfig(), writer, clock=snapshot_clock)
    task = MarketSnapshotTask(fake_exchange, screener, writer)

    task.run_once()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert MARKET_SNAPSHOT_EVENT in messages
    assert SCREEN_DECISION_EVENT in messages


def test_writer_raising_does_not_propagate_out_of_run_once(
    fake_exchange: FakeExchange,
    snapshot_clock: Callable[[], datetime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken ledger writer, shared by the task and its Screener, is isolated.

    Neither the `MARKET_SNAPSHOT` write nor the `SCREEN_DECISION` write may
    crash the run; the failure is logged and swallowed for every market.
    """
    caplog.set_level(logging.DEBUG)
    raising_writer = _RaisingWriter()
    screener = Screener(ScreenerConfig(), raising_writer, clock=snapshot_clock)
    task = MarketSnapshotTask(fake_exchange, screener, raising_writer)

    task.run_once()

    assert any("ledger" in record.getMessage().lower() for record in caplog.records)
