"""Tests for windbreak.connector.snapshot (issue #16): the market-snapshot task.

Acceptance test: `MarketSnapshotTask.run_once()` records exactly one
MARKET_SNAPSHOT and one SCREEN_DECISION event per market on
`connector.list_markets()`, faithfully reflecting the screener's blocked/
eligible verdict. `windbreak/connector/` does not exist yet, so importing
`windbreak.connector.snapshot` fails collection with `ModuleNotFoundError: No
module named 'windbreak.connector'` -- the expected Gate 1 RED state for
issue #16.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from windbreak.connector.models import market_to_payload
from windbreak.connector.snapshot import (
    MARKET_SNAPSHOT_EVENT,
    SCREEN_DECISION_EVENT,
    LoggingEventLedgerWriter,
    MarketSnapshotTask,
)
from windbreak.screener import StubScreener

if TYPE_CHECKING:
    import pytest

    from windbreak.connector.fake import FakeExchange
    from windbreak.connector.snapshot import ConnectorEvent, InMemoryEventLedgerWriter

_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


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
    snapshot_task.run_once()

    market_count = len(fake_exchange.list_markets())
    snapshots = in_memory_ledger.events_by_type(MARKET_SNAPSHOT_EVENT)
    decisions = in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)

    assert len(snapshots) == market_count
    assert len(decisions) == market_count


def test_every_screen_decision_payload_has_a_valid_decision_value(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    snapshot_task.run_once()

    decisions = in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)

    assert decisions
    for event in decisions:
        assert event.payload["decision"] in ("eligible", "blocked")
        assert "ticker" in event.payload
        assert "reason" in event.payload


def test_ineligible_and_unknown_markets_are_blocked_with_jurisdiction_reason(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    snapshot_task.run_once()

    decisions = {
        event.payload["ticker"]: event.payload
        for event in in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)
    }

    assert decisions["KXBAN-24DEC"]["decision"] == "blocked"
    assert "jurisdiction" in decisions["KXBAN-24DEC"]["reason"].lower()
    assert decisions["KXWEA-24DEC"]["decision"] == "blocked"
    assert "jurisdiction" in decisions["KXWEA-24DEC"]["reason"].lower()


def test_eligible_market_is_screened_eligible(
    snapshot_task: MarketSnapshotTask, in_memory_ledger: InMemoryEventLedgerWriter
) -> None:
    snapshot_task.run_once()

    decisions = {
        event.payload["ticker"]: event.payload
        for event in in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)
    }

    assert decisions["KXFED-24DEC"]["decision"] == "eligible"


def test_market_snapshot_payload_matches_market_to_payload(
    snapshot_task: MarketSnapshotTask,
    fake_exchange: FakeExchange,
    in_memory_ledger: InMemoryEventLedgerWriter,
) -> None:
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
    snapshot_task.run_once()

    all_events = in_memory_ledger.events_by_type(
        MARKET_SNAPSHOT_EVENT
    ) + in_memory_ledger.events_by_type(SCREEN_DECISION_EVENT)

    assert all_events
    for event in all_events:
        assert _ISO_UTC.match(event.ts), f"not ISO-Z: {event.ts!r}"


def test_logging_event_ledger_writer_logs_each_event(
    fake_exchange: FakeExchange, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    task = MarketSnapshotTask(fake_exchange, StubScreener(), LoggingEventLedgerWriter())

    task.run_once()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert MARKET_SNAPSHOT_EVENT in messages
    assert SCREEN_DECISION_EVENT in messages


def test_writer_raising_does_not_propagate_out_of_run_once(
    fake_exchange: FakeExchange, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken ledger writer must not crash the snapshot task (isolation)."""
    caplog.set_level(logging.DEBUG)
    task = MarketSnapshotTask(fake_exchange, StubScreener(), _RaisingWriter())

    task.run_once()

    assert any("ledger" in record.getMessage().lower() for record in caplog.records)
