"""Shared fixtures/builders for `tests/drills/*` (issue #59, RED).

`windbreak.drills` does not exist yet, so any test module importing from it
fails collection with `ModuleNotFoundError: No module named
'windbreak.drills'` -- the expected Gate 1 RED state for issue #59. This
module itself imports only already-shipped machinery
(`windbreak.ledger.events`, `windbreak.riskkernel.modes`), so it collects
cleanly on its own; the `ModuleNotFoundError` surfaces from the individual
`test_*.py` files that import the not-yet-existing `windbreak.drills`
submodules directly.

Builder-placement choice mirrors `tests/riskkernel/conftest.py`: plain,
explicitly-imported functions and small recording doubles rather than pytest
fixtures where a helper needs to compose freely (e.g. inside a
`DrillContext` built by hand in a given test), plus a couple of ordinary
fixtures for the two or three things nearly every test in this package needs
verbatim.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from windbreak.ledger.events import Event

#: A fixed "current instant" every drill-context clock in this package agrees
#: on, so every timestamp assertion below is an exact int rather than an
#: `isinstance` check against real wall-clock time.
FIXED_EPOCH_S = 1_700_000_000

#: Obviously-fake, low-entropy HMAC-SHA256 signing-key material (issue #59):
#: 64 hex characters of `"0"` decode to 32 zero bytes -- the minimum length
#: `SigningKeyHandle` accepts -- while being unmistakably a placeholder rather
#: than a real secret (detect-secrets and any human reviewer can tell at a
#: glance). Never a real credential.
FAKE_SIGNING_KEY_HEX = "0" * 64

#: An obviously-fake, low-entropy trade-key placeholder, used only to prove a
#: rotation replaces it -- never a real credential.
FAKE_TRADE_KEY = "fake-key-not-real"

#: An injected environment mapping carrying only obviously-fake credentials,
#: for drills that must never touch the real process environment.
FAKE_CRED_ENV: dict[str, str] = {
    "WINDBREAK_APPROVAL_TOKEN_KEY": FAKE_SIGNING_KEY_HEX,
    "WINDBREAK_TRADE_KEY": FAKE_TRADE_KEY,
}


class InMemoryDrillLedgerWriter:
    """A minimal ledger-writer double retaining every recorded event in memory.

    Mirrors `windbreak.riskkernel.process.InMemoryKernelLedgerWriter`'s shape
    exactly (a public, list-typed `.events` plus a `.record` method), reused
    here as the *operational* ledger `run_drill` appends `DrillCompleted`
    into -- distinct from any temp ledger a drill manipulates internally.
    """

    def __init__(self) -> None:
        """Initialize with an empty, publicly readable event log."""
        self.events: list[Event] = []

    def record(self, event: Event) -> None:
        """Append an event to the in-memory log.

        Args:
            event: The event to retain.
        """
        self.events.append(event)


class RecordingAlertSink:
    """A narrow alert-dispatcher double recording every dispatched call.

    Mirrors `tests/riskkernel/test_kill.py::_FakeAlertSink`'s shape (a
    `.dispatch(alert_type, message)` method plus an inspectable log), so
    drill tests composing `KillSwitch`/`ReconciliationMismatchMonitor` can
    assert exactly which alert types fired without a real sink.
    """

    def __init__(self) -> None:
        """Initialize with an empty dispatch log."""
        self.dispatched: list[tuple[object, str]] = []

    def dispatch(self, alert_type: object, message: str) -> None:
        """Record a dispatched alert type and message.

        Args:
            alert_type: The alert type dispatched.
            message: The alert body.
        """
        self.dispatched.append((alert_type, message))

    def count(self, alert_type: object) -> int:
        """Return how many times `alert_type` was dispatched.

        Args:
            alert_type: The alert type to count.

        Returns:
            The number of matching dispatch calls recorded.
        """
        return sum(1 for recorded, _ in self.dispatched if recorded == alert_type)


class RecordingDirectiveSink:
    """A narrow directive-sink double recording every submitted directive.

    Mirrors `tests/riskkernel/test_kill.py::_FakeDirectiveSink`.
    """

    def __init__(self) -> None:
        """Initialize with an empty received-directives log."""
        self.received: list[object] = []

    def submit(self, directive: object) -> None:
        """Record a submitted directive.

        Args:
            directive: The directive submitted for delivery.
        """
        self.received.append(directive)


def make_tmp_dir_factory(base: Path) -> Callable[[], Path]:
    """Build a `DrillContext.tmp_dir_factory`-shaped callable rooted at `base`.

    Each call returns a freshly created, previously-unused subdirectory of
    `base`, so a drill that calls the factory more than once (e.g. once for a
    "restore" copy and once per rebuild output directory) never collides with
    itself.

    Args:
        base: The parent directory each freshly minted scratch directory is
            created under; created if absent.

    Returns:
        A zero-argument callable returning a fresh, existing directory on
        every call.
    """
    base.mkdir(parents=True, exist_ok=True)
    counter = itertools.count()

    def _factory() -> Path:
        """Return a freshly created, previously-unused scratch directory."""
        path = base / f"tmp-{next(counter)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    return _factory


@pytest.fixture
def fixed_clock() -> Callable[[], int]:
    """Provide a zero-argument callable that always returns `FIXED_EPOCH_S`."""
    return lambda: FIXED_EPOCH_S


@pytest.fixture
def fake_cred_env() -> dict[str, str]:
    """Provide a fresh copy of the obviously-fake credential environment."""
    return dict(FAKE_CRED_ENV)
