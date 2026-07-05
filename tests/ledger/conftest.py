"""Shared fixtures for the ledger test suite (issue #13).

Provides:

- ``deterministic_clock``: an injectable, fully reproducible clock so
  ``created_at`` values (and therefore ``event_hash`` values) never depend
  on wall-clock time or test ordering.
- ``ledger_store_factory``: builds tmp-path-backed ``SqliteLedgerStore``
  instances using the deterministic clock by default, and closes every
  store it creates at teardown.

Tests that reach around the public API (via raw ``sqlite3``) to tamper
with a row write the ``ledger`` table name as a literal in their SQL: it
is a fixed part of the on-disk contract pinned by this suite, and keeping
the SQL fully literal avoids string-built-query false positives from the
security scanner (bandit B608).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from hedgekit.ledger.store import SqliteLedgerStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


class DeterministicClock:
    """A callable clock returning strictly increasing, fixed UTC datetimes.

    Each call advances an internal counter by one ``step`` from a fixed
    epoch (2024-01-01T00:00:00+00:00 by default), giving tests
    reproducible ``created_at`` values without ever touching the real
    wall clock.
    """

    def __init__(
        self,
        start: datetime | None = None,
        step: timedelta = timedelta(seconds=1),
    ) -> None:
        """Initialize the clock at `start` (default: a fixed 2024 epoch).

        Args:
            start: The datetime the first call returns. Defaults to
                2024-01-01T00:00:00+00:00.
            step: The amount advanced between successive calls.
        """
        self._current = start or datetime(2024, 1, 1, tzinfo=UTC)
        self._step = step
        self._calls = 0

    def __call__(self) -> datetime:
        """Return the next deterministic UTC datetime.

        Returns:
            The fixed start time on the first call, then a value
            advanced by ``step`` on every subsequent call.
        """
        if self._calls > 0:
            self._current = self._current + self._step
        self._calls += 1
        return self._current


@pytest.fixture
def deterministic_clock() -> DeterministicClock:
    """Provide a fresh, independent deterministic clock per test."""
    return DeterministicClock()


@pytest.fixture
def ledger_store_factory(
    tmp_path: Path, deterministic_clock: DeterministicClock
) -> Iterator[Callable[..., SqliteLedgerStore]]:
    """Provide a factory building tmp-path-backed `SqliteLedgerStore`s.

    Every store built by the returned factory is closed automatically at
    fixture teardown, and defaults to the shared `deterministic_clock`
    unless a different `now` callable is supplied.

    Yields:
        A callable ``factory(name="ledger.db", *, now=None)`` returning a
        new `SqliteLedgerStore` rooted at ``tmp_path / name``.
    """
    created: list[SqliteLedgerStore] = []

    def _factory(
        name: str = "ledger.db",
        *,
        now: Callable[[], datetime] | None = None,
    ) -> SqliteLedgerStore:
        """Build (or reopen) a `SqliteLedgerStore` at `tmp_path / name`."""
        db_path = tmp_path / name
        store = SqliteLedgerStore(db_path, now=now or deterministic_clock)
        created.append(store)
        return store

    yield _factory

    for store in created:
        store.close()
