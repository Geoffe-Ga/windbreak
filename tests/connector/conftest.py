"""Shared fixtures for windbreak.connector / windbreak.screener tests (issue #16).

Every fixture below is built against the shared JSON exchange fixtures in
`tests/fixtures/exchange/`. Neither `windbreak.connector` nor `windbreak.screener`
exist yet, so importing this module fails collection with
`ModuleNotFoundError: No module named 'windbreak.connector'` -- the expected
Gate 1 RED state for issue #16.

The `books_fixture_dir` / `paper_exchange` fixtures below back issue #19's
`PaperExchange` tests (`test_paper_exchange.py`, `test_paper_fill_properties.py`)
and are additive: nothing above this point is modified. `windbreak.connector.paper`
does not exist yet either, so `paper_exchange` fails collection the same way,
with `ModuleNotFoundError: No module named 'windbreak.connector.paper'` -- the
expected Gate 1 RED state for issue #19.

Issue #106 rewires `snapshot_task` onto the real `Screener` (the stub is
deleted) with a fixed clock chosen so the fixture markets' close times
(December 2024) sit inside the default `horizon_days` window; a wall clock
would drift the fixtures out of range as time passes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from windbreak.config import ScreenerConfig
from windbreak.connector.fake import FakeExchange
from windbreak.connector.snapshot import InMemoryEventLedgerWriter, MarketSnapshotTask
from windbreak.screener import Screener

if TYPE_CHECKING:
    from collections.abc import Callable

    from windbreak.connector.paper import PaperExchange

#: A fixed reference "now" inside every fixture market's close-time horizon
#: (December 2024), so horizon-window assertions never drift with wall-clock
#: time.
_SNAPSHOT_NOW = datetime(2024, 12, 10, tzinfo=UTC)


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
def snapshot_clock() -> Callable[[], datetime]:
    """Provide the fixed clock `snapshot_task`'s `Screener` measures horizons from."""
    return lambda: _SNAPSHOT_NOW


@pytest.fixture
def snapshot_task(
    fake_exchange: FakeExchange,
    in_memory_ledger: InMemoryEventLedgerWriter,
    snapshot_clock: Callable[[], datetime],
) -> MarketSnapshotTask:
    """Provide a `MarketSnapshotTask` wired to the fake exchange and the real Screener.

    The task and its `Screener` share `in_memory_ledger`, so `MARKET_SNAPSHOT`
    and `SCREEN_DECISION` events land in the same ledger (issue #106: the real
    `Screener` is now the single `SCREEN_DECISION` emitter).
    """
    screener = Screener(ScreenerConfig(), in_memory_ledger, clock=snapshot_clock)
    return MarketSnapshotTask(fake_exchange, screener, in_memory_ledger)


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
    from windbreak.connector.paper import PaperExchange

    return PaperExchange.from_fixture_dir(books_fixture_dir / "deep_walk")
