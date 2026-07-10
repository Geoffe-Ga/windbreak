"""Failing-first tests for windbreak.drills.framework (issue #59, RED).

`windbreak.drills.framework` does not exist yet, so the import below fails
collection with `ModuleNotFoundError: No module named
'windbreak.drills.framework'` -- the expected Gate 1 RED state for issue #59.
This file also pins the new `DrillCompleted` ledger event
(`windbreak/ledger/events.py`), which likewise does not exist yet.

Pins: the `Drill` ABC's `run()` template method (preconditions -> execute ->
teardown-in-a-finally); `DrillResult`'s JSON-serializable-evidence
validation; `run_drill()`'s exactly-one `DrillCompleted` ledger append (the
class-name discriminator `"DrillCompleted"`, never a shouty-snake variant);
and the fail-closed paths (a precondition failure never reaches
execute/teardown; an unexpected, non-`DrillFailedError` exception still tears
down but re-raises and is never ledgered as a graded result; a broken
teardown is never silently swallowed).
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.drills.conftest import InMemoryDrillLedgerWriter
from windbreak.drills.framework import (
    Drill,
    DrillEvidenceError,
    DrillFailedError,
    DrillPreconditionError,
    DrillResult,
    run_drill,
)
from windbreak.ledger.events import EVENT_TYPES, DrillCompleted, canonical_json

#: A drill name shared by every fake `Drill` subclass in this file.
_DRILL_NAME = "fake-drill"


class _Sentinel:
    """A placeholder `DrillContext`-shaped object; these tests never read it."""


class _CallLog:
    """Records which of a fake drill's lifecycle hooks ran, in order."""

    def __init__(self) -> None:
        """Initialize with an empty call log."""
        self.calls: list[str] = []


class _PassingDrill(Drill):
    """A `Drill` whose `execute` always succeeds."""

    name = _DRILL_NAME

    def __init__(self, log: _CallLog) -> None:
        """Wire the shared call log."""
        self._log = log

    def check_preconditions(self, ctx: object) -> None:
        """Record that preconditions ran."""
        self._log.calls.append("check_preconditions")

    def execute(self, ctx: object) -> dict[str, object]:
        """Record that execute ran and return fixed evidence."""
        self._log.calls.append("execute")
        return {"detail": "ok"}

    def teardown(self, ctx: object) -> None:
        """Record that teardown ran."""
        self._log.calls.append("teardown")


class _FailingDrill(Drill):
    """A `Drill` whose `execute` raises `DrillFailedError`."""

    name = _DRILL_NAME

    def __init__(self, log: _CallLog) -> None:
        """Wire the shared call log."""
        self._log = log

    def check_preconditions(self, ctx: object) -> None:
        """Record that preconditions ran."""
        self._log.calls.append("check_preconditions")

    def execute(self, ctx: object) -> dict[str, object]:
        """Record that execute ran, then raise `DrillFailedError` with evidence."""
        self._log.calls.append("execute")
        raise DrillFailedError({"reason": "assertion failed"})

    def teardown(self, ctx: object) -> None:
        """Record that teardown ran."""
        self._log.calls.append("teardown")


class _CrashingDrill(Drill):
    """A `Drill` whose `execute` raises an unexpected (non-`DrillFailedError`) error."""

    name = _DRILL_NAME

    def __init__(self, log: _CallLog) -> None:
        """Wire the shared call log."""
        self._log = log

    def check_preconditions(self, ctx: object) -> None:
        """Record that preconditions ran."""
        self._log.calls.append("check_preconditions")

    def execute(self, ctx: object) -> dict[str, object]:
        """Record that execute ran, then raise an unexpected error."""
        self._log.calls.append("execute")
        raise RuntimeError("unexpected boom")

    def teardown(self, ctx: object) -> None:
        """Record that teardown ran."""
        self._log.calls.append("teardown")


class _TeardownRaisesDrill(Drill):
    """A `Drill` whose `execute` passes but whose `teardown` itself raises."""

    name = _DRILL_NAME

    def check_preconditions(self, ctx: object) -> None:
        """A no-op precondition."""

    def execute(self, ctx: object) -> dict[str, object]:
        """Return fixed evidence from an otherwise-passing execute."""
        return {"detail": "ok"}

    def teardown(self, ctx: object) -> None:
        """Always raise, simulating a broken teardown."""
        raise RuntimeError("teardown exploded")


class _PreconditionFailingDrill(Drill):
    """A `Drill` whose `check_preconditions` itself raises."""

    name = _DRILL_NAME

    def __init__(self, log: _CallLog) -> None:
        """Wire the shared call log."""
        self._log = log

    def check_preconditions(self, ctx: object) -> None:
        """Record the attempt, then raise `DrillPreconditionError`."""
        self._log.calls.append("check_preconditions")
        raise DrillPreconditionError("prerequisite missing")

    def execute(self, ctx: object) -> dict[str, object]:
        """Record that execute ran (it must not, given the precondition failed)."""
        self._log.calls.append("execute")
        return {}

    def teardown(self, ctx: object) -> None:
        """Record that teardown ran (it must not, given the precondition failed)."""
        self._log.calls.append("teardown")


# --- DrillResult: evidence must be JSON-serializable ----------------------------


def test_drill_result_accepts_json_serializable_evidence() -> None:
    """A `DrillResult` with plain JSON-serializable evidence constructs cleanly."""
    result = DrillResult(drill=_DRILL_NAME, passed=True, evidence={"n": 1, "s": "x"})

    assert result.drill == _DRILL_NAME
    assert result.passed is True
    assert result.evidence == {"n": 1, "s": "x"}


def test_drill_result_rejects_non_json_serializable_evidence() -> None:
    """Non-JSON-serializable evidence (e.g. a raw object) raises
    `DrillEvidenceError` -- fail-closed, never a silently-corrupted ledger
    append later.
    """
    with pytest.raises(DrillEvidenceError):
        DrillResult(drill=_DRILL_NAME, passed=True, evidence={"bad": object()})


# --- Drill.run(): the preconditions -> execute -> teardown template -------------


def test_run_on_a_passing_drill_calls_hooks_in_order_and_returns_passed_true() -> None:
    """A passing drill's `run()` calls preconditions, execute, then teardown,
    in that order, and returns a `passed=True` result carrying execute's
    evidence.
    """
    log = _CallLog()
    drill = _PassingDrill(log)

    result = drill.run(_Sentinel())

    assert log.calls == ["check_preconditions", "execute", "teardown"]
    assert result.passed is True
    assert result.evidence == {"detail": "ok"}
    assert result.drill == _DRILL_NAME


def test_run_returns_passed_false_with_evidence_and_tears_down_on_failure() -> None:
    """A `DrillFailedError` raised from `execute` is caught: `run()` returns
    `passed=False` carrying the evidence the failure raised with, and
    teardown still ran.
    """
    log = _CallLog()
    drill = _FailingDrill(log)

    result = drill.run(_Sentinel())

    assert result.passed is False
    assert result.evidence == {"reason": "assertion failed"}
    assert log.calls == ["check_preconditions", "execute", "teardown"]


def test_run_on_an_unexpected_exception_still_tears_down_then_reraises() -> None:
    """An exception that is *not* `DrillFailedError` is not swallowed: `run()`
    still runs teardown (fail-closed cleanup) but re-raises the original
    exception rather than reporting a false `passed=False`.
    """
    log = _CallLog()
    drill = _CrashingDrill(log)

    with pytest.raises(RuntimeError, match="unexpected boom"):
        drill.run(_Sentinel())

    assert log.calls == ["check_preconditions", "execute", "teardown"]


def test_run_when_teardown_itself_raises_propagates_the_teardown_error() -> None:
    """A teardown that raises is never silently swallowed, even though
    `execute` itself passed cleanly -- a broken teardown must be loud, not
    hidden behind a falsely-green drill result.
    """
    drill = _TeardownRaisesDrill()

    with pytest.raises(RuntimeError, match="teardown exploded"):
        drill.run(_Sentinel())


def test_check_preconditions_failure_raises_before_execute_or_teardown_run() -> None:
    """A `DrillPreconditionError` from `check_preconditions` is a fail-closed
    precondition gate, not a graded `passed=False` result: it propagates
    without reaching `execute` or `teardown`.
    """
    log = _CallLog()
    drill = _PreconditionFailingDrill(log)

    with pytest.raises(DrillPreconditionError):
        drill.run(_Sentinel())

    assert log.calls == ["check_preconditions"]


# --- run_drill(): ledgers exactly one DrillCompleted, always --------------------


def test_run_drill_on_a_pass_ledgers_one_drill_completed_with_passed_true() -> None:
    """`run_drill` ledgers exactly one `DrillCompleted` event -- discriminated
    by the literal class-name string `"DrillCompleted"`, never a shouty-snake
    variant -- carrying `passed=True` and the drill's evidence, into the
    *operational* ledger writer passed in (distinct from any temp ledger the
    drill manipulates internally).
    """
    log = _CallLog()
    drill = _PassingDrill(log)
    writer = InMemoryDrillLedgerWriter()

    result = run_drill(drill, _Sentinel(), writer)

    assert result.passed is True
    completed = [e for e in writer.events if e.event_type == "DrillCompleted"]
    assert len(completed) == 1
    assert completed[0].payload == {
        "drill": _DRILL_NAME,
        "passed": True,
        "evidence": {"detail": "ok"},
    }


def test_run_drill_ledgers_drill_completed_passed_false_on_failure() -> None:
    """A drill that fails via `DrillFailedError` is *still* ledgered: `run_drill`
    never skips the append just because the drill itself failed.
    """
    log = _CallLog()
    drill = _FailingDrill(log)
    writer = InMemoryDrillLedgerWriter()

    result = run_drill(drill, _Sentinel(), writer)

    assert result.passed is False
    completed = [e for e in writer.events if e.event_type == "DrillCompleted"]
    assert len(completed) == 1
    assert completed[0].payload["passed"] is False
    assert completed[0].payload["evidence"] == {"reason": "assertion failed"}


def test_run_drill_on_an_unexpected_exception_never_ledgers_and_reraises() -> None:
    """An unexpected (non-`DrillFailedError`) exception is not a graded outcome at
    all: `run_drill` re-raises it and appends nothing to the operational
    ledger -- there is no well-formed result to ledger.
    """
    log = _CallLog()
    drill = _CrashingDrill(log)
    writer = InMemoryDrillLedgerWriter()

    with pytest.raises(RuntimeError, match="unexpected boom"):
        run_drill(drill, _Sentinel(), writer)

    assert writer.events == []


# --- DrillCompleted: registered under the class-name discriminator -------------


def test_drill_completed_event_type_equals_its_class_name_and_is_registered() -> None:
    """`DrillCompleted.event_type` is the literal string `"DrillCompleted"`,
    and the class is reachable via `EVENT_TYPES["DrillCompleted"]` for
    envelope replay.
    """
    event = DrillCompleted(
        component="drills", drill=_DRILL_NAME, passed=True, evidence={"n": 1}
    )

    assert event.event_type == "DrillCompleted"
    assert event.payload == {
        "drill": _DRILL_NAME,
        "passed": True,
        "evidence": {"n": 1},
    }
    assert EVENT_TYPES["DrillCompleted"] is DrillCompleted


# --- Hypothesis: dict[str, int] evidence round-trips through the envelope ------


@given(evidence=st.dictionaries(st.text(min_size=1), st.integers(), max_size=10))
def test_drill_completed_evidence_round_trips_through_envelope_and_event_types(
    evidence: dict[str, int],
) -> None:
    """Any `dict[str, int]` evidence survives a full envelope round-trip: the
    envelope's `data` reconstructs an identical event via
    `EVENT_TYPES["DrillCompleted"](component=..., **data)`.
    """
    original = DrillCompleted(
        component="drills", drill=_DRILL_NAME, passed=True, evidence=evidence
    )

    envelope = json.loads(original.envelope_json)
    data = envelope["data"]
    rebuilt = EVENT_TYPES["DrillCompleted"](component="drills", **data)

    assert rebuilt.payload == original.payload
    assert rebuilt.event_type == "DrillCompleted"
    assert canonical_json(data) == canonical_json(original.payload)
