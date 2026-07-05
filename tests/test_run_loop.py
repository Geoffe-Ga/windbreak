"""Tests for the heartbeat engine and signal handling (issue #10).

`run_loop` is the deterministic core of `hedgekit run`: it never calls
`time.sleep` and never touches real OS signal delivery. Every test here
drives it via injected `max_beats`, an injected `stop_event`, or a fake
`threading.Event` subclass, and via direct invocation of the installed
signal handler -- no subprocesses, no real signals.

Assumed API surface (matches the architect's plan and this module's
naming so the implementation targets the same contract):

- `run_loop(interval_seconds, *, max_beats=None, stop_event=None)` logs
  `mode=RESEARCH heartbeat seq=<n>` per beat (seq starting at 1, strictly
  increasing) and, on exit, `shutdown reason=<...>`. Waiting between
  beats is `stop_event.wait(interval_seconds)` on an injectable
  `threading.Event`.
- `_install_signal_handlers(state)` installs SIGINT/SIGTERM handlers
  that, when invoked, set `state.stop_event` and record
  `signal.Signals(signum).name` on `state.reason`. `state` is a plain
  object exposing a `stop_event: threading.Event` attribute and a
  `reason: str | None` attribute.
"""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from hedgekit.main import ShutdownState, _install_signal_handlers, run_loop

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class _ShutdownState:
    """Minimal stand-in for the shared mutable shutdown-state object.

    `_install_signal_handlers` is expected to accept any object exposing
    these two attributes and mutate both from within a signal handler.
    """

    stop_event: threading.Event
    reason: str | None = None


class _RecordingEvent(threading.Event):
    """threading.Event stand-in that records the timeout passed to wait().

    `wait()` never actually blocks -- it records the requested timeout
    and returns `False` immediately (as if the wait always timed out
    without the event being set). This lets tests assert on the *value*
    passed to `wait()` (killing interval-arithmetic mutants) without
    ever sleeping.
    """

    def __init__(self) -> None:
        super().__init__()
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        """Record the requested timeout and report "not set" immediately."""
        self.wait_calls.append(timeout)
        return False


class _SignalOnBeatEvent(threading.Event):
    """Event that simulates a signal delivered during a specific wait().

    On the ``fire_on_call``-th call to `wait()` it records a signal name
    on the shared state and sets itself -- reproducing the race where a
    signal arrives during the final inter-beat wait at the exact
    ``max_beats`` boundary. Earlier calls return "not set" immediately so
    the loop keeps beating up to that boundary without sleeping.
    """

    def __init__(self, *, fire_on_call: int, state: ShutdownState, reason: str) -> None:
        super().__init__()
        self._fire_on_call = fire_on_call
        self._state = state
        self._reason = reason
        self._calls = 0

    def wait(self, timeout: float | None = None) -> bool:
        """Fire the simulated signal on the target call, else report unset."""
        self._calls += 1
        if self._calls == self._fire_on_call:
            self._state.reason = self._reason
            self.set()
        return self.is_set()


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    """Save and restore SIGINT/SIGTERM handlers around a test.

    `_install_signal_handlers` mutates process-global signal
    dispositions; without this fixture a failing assertion could leave
    later tests (or the pytest process itself) with a hijacked SIGINT
    handler.
    """
    original_sigint = signal.getsignal(signal.SIGINT)
    original_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGTERM, original_sigterm)


def test_run_loop_emits_seq_1_through_3_then_max_beats_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """3 beats bounded by max_beats emit seq=1..3, then a shutdown line."""
    caplog.set_level(logging.INFO)

    run_loop(0, max_beats=3)

    heartbeat_lines = [
        record.message for record in caplog.records if "heartbeat" in record.message
    ]
    assert heartbeat_lines == [
        "mode=RESEARCH heartbeat seq=1",
        "mode=RESEARCH heartbeat seq=2",
        "mode=RESEARCH heartbeat seq=3",
    ]

    shutdown_lines = [
        record.message
        for record in caplog.records
        if record.message.startswith("shutdown reason=")
    ]
    assert shutdown_lines == ["shutdown reason=max_beats"]


def test_run_loop_prefers_signal_reason_over_max_beats_on_boundary_race(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A signal during the final wait at the max_beats boundary wins.

    When a signal arrives mid-wait at the exact beat where ``seq ==
    max_beats``, the loop exits because its stop event is now set, so the
    shutdown reason must be the recorded signal name -- not ``max_beats``.
    This pins the *actual* break site (stop event checked before the beat
    budget) as the single source of truth, guarding against the
    reason-misattribution race where both conditions hold at once.
    """
    caplog.set_level(logging.INFO)
    state = ShutdownState()
    event = _SignalOnBeatEvent(fire_on_call=2, state=state, reason="SIGINT")

    run_loop(0, max_beats=2, stop_event=event, state=state)

    heartbeat_lines = [
        record.message for record in caplog.records if "heartbeat" in record.message
    ]
    shutdown_lines = [
        record.message
        for record in caplog.records
        if record.message.startswith("shutdown reason=")
    ]
    assert heartbeat_lines == [
        "mode=RESEARCH heartbeat seq=1",
        "mode=RESEARCH heartbeat seq=2",
    ]
    assert shutdown_lines == ["shutdown reason=SIGINT"]


def test_run_loop_with_preset_stop_event_emits_zero_beats(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stop_event that is already set before the loop starts yields 0 beats.

    The shutdown line must still be emitted -- shutdown is not silent.
    """
    caplog.set_level(logging.INFO)
    event = threading.Event()
    event.set()

    run_loop(0, stop_event=event)

    heartbeat_lines = [
        record.message for record in caplog.records if "heartbeat" in record.message
    ]
    shutdown_lines = [
        record.message
        for record in caplog.records
        if record.message.startswith("shutdown reason=")
    ]

    assert not heartbeat_lines
    assert len(shutdown_lines) == 1


def test_run_loop_reports_signal_name_in_shutdown_line(
    caplog: pytest.LogCaptureFixture,
    restore_signal_handlers: None,
) -> None:
    """A signal-triggered shutdown logs the specific signal name.

    Faithful end-to-end path: install the real handlers, invoke the SIGINT
    handler directly (which records the name on the shared state and sets its
    stop event), then let the loop unwind. The shutdown line must surface
    ``SIGINT`` -- the normative DoD output -- not a generic ``signal``.
    """
    caplog.set_level(logging.INFO)
    state = ShutdownState()
    _install_signal_handlers(state)
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    handler(signal.SIGINT, None)

    run_loop(0, state=state)

    shutdown_lines = [
        record.message
        for record in caplog.records
        if record.message.startswith("shutdown reason=")
    ]
    assert shutdown_lines == ["shutdown reason=SIGINT"]


def test_run_loop_without_state_falls_back_to_generic_signal_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stopping via a bare stop_event (no state) logs the generic reason.

    When no shutdown state carries a signal name, the loop must still emit
    a shutdown line, defaulting the reason to ``signal``.
    """
    caplog.set_level(logging.INFO)
    event = threading.Event()
    event.set()

    run_loop(0, stop_event=event)

    shutdown_lines = [
        record.message
        for record in caplog.records
        if record.message.startswith("shutdown reason=")
    ]
    assert shutdown_lines == ["shutdown reason=signal"]


def test_run_loop_calls_stop_event_wait_with_exact_interval_seconds() -> None:
    """`stop_event.wait()` must receive interval_seconds unmodified.

    Using a fake Event (rather than a real one) makes the test fast and
    deterministic while still exercising the real call site; asserting
    the exact value passed kills mutants that scale, offset, or drop
    the interval argument.
    """
    fake_event = _RecordingEvent()

    run_loop(2.5, max_beats=3, stop_event=fake_event)

    assert fake_event.wait_calls, "run_loop never called stop_event.wait()"
    assert all(timeout == 2.5 for timeout in fake_event.wait_calls)


def test_install_signal_handlers_sigint_sets_event_and_records_reason(
    restore_signal_handlers: None,
) -> None:
    """The installed SIGINT handler sets the event and reason="SIGINT".

    Invoked directly as `handler(signum, frame)` -- no real signal is
    delivered to this process.
    """
    state = _ShutdownState(stop_event=threading.Event())

    _install_signal_handlers(state)
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    handler(signal.SIGINT, None)

    assert state.stop_event.is_set()
    assert state.reason == "SIGINT"


def test_install_signal_handlers_sigterm_sets_event_and_records_reason(
    restore_signal_handlers: None,
) -> None:
    """The installed SIGTERM handler sets the event and reason="SIGTERM"."""
    state = _ShutdownState(stop_event=threading.Event())

    _install_signal_handlers(state)
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)

    assert state.stop_event.is_set()
    assert state.reason == "SIGTERM"
