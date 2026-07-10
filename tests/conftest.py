"""Process-wide signal-handler hygiene shared across the test suite (issue #65).

``windbreak.main._install_signal_handlers`` mutates process-global signal
dispositions via ``signal.signal(...)`` without saving or restoring the
previous handler. Any test that exercises that path -- directly, or
indirectly through ``windbreak.main.main`` -- can leave a later test, or
the pytest process itself, with a hijacked SIGINT/SIGTERM handler. This
conftest installs an autouse fixture that snapshots and restores both
dispositions around every test in the suite, so no individual test module
has to opt in.
"""

from __future__ import annotations

import signal
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def preserved_signal_handlers() -> Iterator[None]:
    """Snapshot and restore the SIGINT/SIGTERM dispositions around a block.

    The restoration happens in a ``finally`` clause, so it runs whether the
    wrapped block completes normally or raises.

    Yields:
        None. The caller's code runs with whatever SIGINT/SIGTERM handlers
        were in effect on entry still installed; it is free to replace
        them, and both dispositions are restored to their entry values on
        exit.
    """
    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGTERM, original_sigterm)


@pytest.fixture(autouse=True)
def restore_signal_handlers() -> Iterator[None]:
    """Save and restore SIGINT/SIGTERM handlers around every test.

    ``_install_signal_handlers`` mutates process-global signal
    dispositions; without this fixture a failing assertion (or any test
    that installs handlers and never restores them) could leave later
    tests, or the pytest process itself, with a hijacked SIGINT or SIGTERM
    handler. Autouse means every test in the suite gets this protection
    without opting in explicitly.
    """
    with preserved_signal_handlers():
        yield
