"""Regression tests for process-wide signal-handler leakage (issue #65).

`tests/test_cli.py::test_main_run_emits_heartbeats_and_max_beats_shutdown`
drives `windbreak.main.main` end-to-end, which calls
`_install_signal_handlers` -- a function that installs SIGINT/SIGTERM
handlers via bare `signal.signal(...)` calls with no save/restore. Left
unchecked, that leaks a hijacked handler into every test that runs after
it (and into the pytest process itself). `tests/conftest.py` fixes this
with an autouse fixture; these tests pin that fixture's behavior using
`pytest`'s in-process `pytester` plugin so the leak-and-restore cycle is
exercised deterministically, in a fully isolated inner pytest run, rather
than by relying on real test-ordering within this suite (which `xdist`
could split across workers and make non-deterministic).
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.conftest import preserved_signal_handlers

if TYPE_CHECKING:
    from types import FrameType

_CONFTEST_SOURCE = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")

_LEAKING_TEST_SOURCE = """
    import signal

    def _sentinel_int(signum, frame):
        return None

    def _sentinel_term(signum, frame):
        return None

    def test_leaks_handlers():
        signal.signal(signal.SIGINT, _sentinel_int)
        signal.signal(signal.SIGTERM, _sentinel_term)
"""

_LEAKING_THEN_RAISING_TEST_SOURCE = """
    import signal

    def _sentinel_int(signum, frame):
        return None

    def _sentinel_term(signum, frame):
        return None

    def test_leaks_handlers_then_raises():
        signal.signal(signal.SIGINT, _sentinel_int)
        signal.signal(signal.SIGTERM, _sentinel_term)
        raise RuntimeError("boom")
"""


def _sentinel_handler(signum: int, frame: FrameType | None) -> None:
    """Fake disposition used only to prove a handler changed and reverted."""


def test_autouse_fixture_restores_leaked_handlers_after_passing_test(
    pytester: pytest.Pytester,
) -> None:
    """The autouse fixture restores handlers a passing inner test leaked.

    This is the primary regression for issue #65: a test that installs
    SIGINT/SIGTERM handlers and never restores them must not leave those
    handlers installed once it (and pytest's fixture teardown) has run.
    """
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)

    pytester.makeconftest(_CONFTEST_SOURCE)
    pytester.makepyfile(_LEAKING_TEST_SOURCE)

    result = pytester.runpytest_inprocess()

    result.assert_outcomes(passed=1)
    assert signal.getsignal(signal.SIGINT) is before_int
    assert signal.getsignal(signal.SIGTERM) is before_term


def test_autouse_fixture_restores_leaked_handlers_after_failing_test(
    pytester: pytest.Pytester,
) -> None:
    """The autouse fixture restores handlers even when the inner test raises.

    Proves the fixture's teardown runs via `finally`, not merely on the
    happy path -- the acceptance criterion for issue #65.
    """
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)

    pytester.makeconftest(_CONFTEST_SOURCE)
    pytester.makepyfile(_LEAKING_THEN_RAISING_TEST_SOURCE)

    result = pytester.runpytest_inprocess()

    result.assert_outcomes(failed=1)
    assert signal.getsignal(signal.SIGINT) is before_int
    assert signal.getsignal(signal.SIGTERM) is before_term


def test_preserved_signal_handlers_restores_after_normal_exit() -> None:
    """The context manager restores both dispositions on a clean exit."""
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)

    with preserved_signal_handlers():
        signal.signal(signal.SIGINT, _sentinel_handler)
        signal.signal(signal.SIGTERM, _sentinel_handler)
        assert signal.getsignal(signal.SIGINT) is _sentinel_handler
        assert signal.getsignal(signal.SIGTERM) is _sentinel_handler

    assert signal.getsignal(signal.SIGINT) is before_int
    assert signal.getsignal(signal.SIGTERM) is before_term


def test_preserved_signal_handlers_restores_when_body_raises() -> None:
    """The context manager restores both dispositions when the body raises."""
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)

    with pytest.raises(RuntimeError, match="boom"), preserved_signal_handlers():
        signal.signal(signal.SIGINT, _sentinel_handler)
        signal.signal(signal.SIGTERM, _sentinel_handler)
        raise RuntimeError("boom")

    assert signal.getsignal(signal.SIGINT) is before_int
    assert signal.getsignal(signal.SIGTERM) is before_term
