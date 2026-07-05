"""Shared fixtures for hedgekit.connector / hedgekit.screener tests (issue #16).

Every fixture below is built against the shared JSON exchange fixtures in
`tests/fixtures/exchange/`. Neither `hedgekit.connector` nor `hedgekit.screener`
exist yet, so importing this module fails collection with
`ModuleNotFoundError: No module named 'hedgekit.connector'` -- the expected
Gate 1 RED state for issue #16.

The `books_fixture_dir` / `paper_exchange` fixtures below back issue #19's
`PaperExchange` tests (`test_paper_exchange.py`, `test_paper_fill_properties.py`)
and are additive: nothing above this point is modified. `hedgekit.connector.paper`
does not exist yet either, so `paper_exchange` fails collection the same way,
with `ModuleNotFoundError: No module named 'hedgekit.connector.paper'` -- the
expected Gate 1 RED state for issue #19.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hedgekit.connector.fake import FakeExchange
from hedgekit.connector.snapshot import InMemoryEventLedgerWriter, MarketSnapshotTask
from hedgekit.screener import StubScreener

if TYPE_CHECKING:
    from hedgekit.connector.paper import PaperExchange


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


@pytest.fixture
def books_fixture_dir() -> Path:
    """Return the directory holding the `PaperExchange` book-replay fixtures.

    Each immediate subdirectory (`touch_not_fill/`, `trade_through/`,
    `deep_walk/`) is a self-contained, `FakeExchange`-shaped fixture set plus a
    `sessions.json` book-and-trade-print replay, loadable independently via
    `PaperExchange.from_fixture_dir(books_fixture_dir / "<scenario>")`.
    """
    return Path(__file__).resolve().parents[1] / "fixtures" / "books"


@pytest.fixture
def paper_exchange(books_fixture_dir: Path) -> PaperExchange:
    """Provide a `PaperExchange` loaded from the `deep_walk` scenario.

    `deep_walk` is the most full-featured fixture (both book sides populated,
    a two-step session), so it serves as the default for tests that only need
    *a* `PaperExchange` (protocol conformance, defaults) rather than a specific
    fill scenario. Tests pinning a specific scenario's golden numbers build
    their own connector directly from `books_fixture_dir / "<scenario>"`.
    """
    from hedgekit.connector.paper import PaperExchange

    return PaperExchange.from_fixture_dir(books_fixture_dir / "deep_walk")
