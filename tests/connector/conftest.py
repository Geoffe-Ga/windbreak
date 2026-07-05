"""Shared fixtures for hedgekit.connector / hedgekit.screener tests (issue #16).

Every fixture below is built against the shared JSON exchange fixtures in
`tests/fixtures/exchange/`. Neither `hedgekit.connector` nor `hedgekit.screener`
exist yet, so importing this module fails collection with
`ModuleNotFoundError: No module named 'hedgekit.connector'` -- the expected
Gate 1 RED state for issue #16.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hedgekit.connector.fake import FakeExchange
from hedgekit.connector.snapshot import InMemoryEventLedgerWriter, MarketSnapshotTask
from hedgekit.screener import StubScreener


@pytest.fixture
def fixture_dir() -> Path:
    """Return the path to the shared exchange JSON fixtures."""
    return Path(__file__).resolve().parents[1] / "fixtures" / "exchange"


@pytest.fixture
def fake_exchange(fixture_dir: Path) -> FakeExchange:
    """Provide a `FakeExchange` loaded from the shared fixtures."""
    return FakeExchange.from_fixture_dir(fixture_dir)


@pytest.fixture
def in_memory_ledger() -> InMemoryEventLedgerWriter:
    """Provide a fresh in-memory event ledger writer."""
    return InMemoryEventLedgerWriter()


@pytest.fixture
def snapshot_task(
    fake_exchange: FakeExchange, in_memory_ledger: InMemoryEventLedgerWriter
) -> MarketSnapshotTask:
    """Provide a `MarketSnapshotTask` wired to the fake exchange and stub screener."""
    return MarketSnapshotTask(fake_exchange, StubScreener(), in_memory_ledger)
