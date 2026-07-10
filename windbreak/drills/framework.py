"""The drill framework: the ``Drill`` template and its graded result (issue #59).

An operational drill *exercises* an already-shipped safety mechanism end to end
against an injected :class:`~windbreak.drills.context.DrillContext` and grades
the outcome; it never adds new kill/restore/ratchet/reconcile logic of its own.

This module carries the framework the five concrete drills share:

    * :class:`DrillResult` -- the frozen, JSON-serializable-by-construction
      verdict a drill run produces.
    * :class:`Drill` -- the abstract base whose :meth:`Drill.run` template runs
      ``check_preconditions -> execute -> teardown`` with a fail-closed
      contract: a :class:`DrillFailedError` from ``execute`` grades ``passed=False``
      (never a raised exception), *any other* exception still tears down but
      re-raises (never a falsely-green result), and a precondition failure never
      reaches ``execute``/``teardown`` at all.
    * :func:`run_drill` -- runs a drill and appends exactly one
      :class:`~windbreak.ledger.events.DrillCompleted` to the operational ledger
      for a *graded* result, and nothing at all for a re-raised unexpected error.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

from windbreak.ledger.events import DrillCompleted

if TYPE_CHECKING:
    from windbreak.ledger.events import Event

#: Component label stamped on every ``DrillCompleted`` event this module records.
_COMPONENT = "drills"


class DrillEvidenceError(Exception):
    """Raised when a drill's evidence is not JSON-serializable (fail closed).

    A drill's evidence is destined for the hash-chained ledger, so evidence
    that ``json.dumps`` cannot encode is rejected at :class:`DrillResult`
    construction rather than corrupting a later ledger append.
    """


class DrillPreconditionError(Exception):
    """Raised when a drill's preconditions are not met (a fail-closed gate).

    A precondition failure is *not* a graded ``passed=False`` outcome: it
    propagates out of :meth:`Drill.run` before ``execute`` or ``teardown`` runs,
    so a drill that cannot even set up is never mistaken for one that ran and
    failed.
    """


class DrillFailedError(Exception):
    """Raised inside ``execute`` to grade a drill ``passed=False`` with evidence.

    Distinct from an unexpected exception: a :class:`DrillFailedError` is the
    drill's own considered "this invariant did not hold" verdict, so
    :meth:`Drill.run` catches it and returns a graded, ledgerable result. Any
    *other* exception is a genuine fault and is re-raised.

    Attributes:
        evidence: The JSON-serializable evidence explaining the failure.
    """

    def __init__(self, evidence: dict[str, object]) -> None:
        """Wire the failure's evidence.

        Args:
            evidence: The JSON-serializable evidence carried on the graded
                ``passed=False`` result this failure produces.
        """
        self.evidence = evidence
        super().__init__(f"drill failed: {evidence}")


class DrillLedgerWriter(Protocol):
    """The narrow seam :func:`run_drill` appends a ``DrillCompleted`` through.

    Structural (mirroring
    :class:`~windbreak.riskkernel.process.KernelLedgerWriter`) so any object
    with a matching :meth:`record` -- the operational ledger, a logging writer,
    or an in-memory test double -- fits without inheritance.
    """

    def record(self, event: Event) -> None:
        """Append one event to the operational ledger.

        Args:
            event: The event to record.
        """
        ...


@dataclass(frozen=True)
class DrillResult:
    """The graded outcome of one drill run (issue #59).

    Evidence is validated JSON-serializable at construction, so a result can
    only ever be built with ledger-safe evidence.

    Attributes:
        drill: The drill's registry name.
        passed: Whether the drill's graded invariant held.
        evidence: JSON-serializable evidence supporting the verdict.
    """

    drill: str
    passed: bool
    evidence: dict[str, object]

    def __post_init__(self) -> None:
        """Reject evidence ``json.dumps`` cannot encode (fail closed).

        Raises:
            DrillEvidenceError: If ``evidence`` is not JSON-serializable.
        """
        try:
            json.dumps(self.evidence, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise DrillEvidenceError(
                f"drill {self.drill!r} evidence is not JSON-serializable: {exc}"
            ) from exc


class Drill(ABC):
    """The abstract base every operational drill extends (issue #59).

    A concrete drill supplies :meth:`check_preconditions`, :meth:`execute`, and
    :meth:`teardown`; :meth:`run` composes them into the fail-closed template the
    whole suite shares. Subclasses set the class attribute :attr:`name` to their
    registry key.
    """

    #: The drill's registry name, set by each concrete subclass.
    name: ClassVar[str]

    @abstractmethod
    def check_preconditions(self, ctx: object) -> None:
        """Verify the drill's prerequisites, raising to abort before execute.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to inspect.

        Raises:
            DrillPreconditionError: If a prerequisite is missing.
        """

    @abstractmethod
    def execute(self, ctx: object) -> dict[str, object]:
        """Run the drill's scenario and return its evidence.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to run
                against.

        Returns:
            The JSON-serializable evidence for a passing run.

        Raises:
            DrillFailedError: If the drill's graded invariant did not hold.
        """

    @abstractmethod
    def teardown(self, ctx: object) -> None:
        """Release any resources the run acquired, run in a ``finally``.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` the run
                used.
        """

    def run(self, ctx: object) -> DrillResult:
        """Run the drill: preconditions, then execute-graded, then teardown.

        The template is fail-closed: preconditions run *outside* the try, so a
        :class:`DrillPreconditionError` aborts before ``execute``/``teardown``;
        a :class:`DrillFailedError` from ``execute`` grades ``passed=False``; any
        other exception still tears down but re-raises (never a falsely-green
        result); and ``teardown`` always runs in the ``finally``, so a broken
        teardown surfaces loudly even after a clean ``execute``.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to run
                against.

        Returns:
            The graded :class:`DrillResult`.

        Raises:
            DrillPreconditionError: If a precondition is not met.
            Exception: Any non-:class:`DrillFailedError` raised by ``execute`` or
                ``teardown`` propagates unchanged.
        """
        return _run_lifecycle(self, ctx)


def _graded_execute(drill: Drill, ctx: object) -> DrillResult:
    """Run ``execute``, grading a :class:`DrillFailedError` as ``passed=False``.

    Args:
        drill: The drill whose ``execute`` hook is graded.
        ctx: The :class:`~windbreak.drills.context.DrillContext` to run against.

    Returns:
        A graded :class:`DrillResult`; a caught :class:`DrillFailedError` yields
        ``passed=False`` carrying its evidence.
    """
    try:
        evidence = drill.execute(ctx)
    except DrillFailedError as failure:
        return DrillResult(drill=drill.name, passed=False, evidence=failure.evidence)
    return DrillResult(drill=drill.name, passed=True, evidence=evidence)


def _run_lifecycle(drill: Drill, ctx: object) -> DrillResult:
    """Drive one drill's ``preconditions -> execute -> teardown`` lifecycle.

    Shared by :meth:`Drill.run` and :func:`run_drill` so both grade a drill
    identically, and so :func:`run_drill` drives the lifecycle hooks directly
    rather than requiring a bound :meth:`run` (a duck-typed drill exposing only
    the three hooks still runs). Preconditions run *outside* the try, so a
    precondition failure aborts before ``execute``/``teardown``; ``teardown``
    always runs in the ``finally``.

    Args:
        drill: The drill (or hook-compatible object) to run.
        ctx: The :class:`~windbreak.drills.context.DrillContext` to run against.

    Returns:
        The graded :class:`DrillResult`.

    Raises:
        DrillPreconditionError: If a precondition is not met.
        Exception: Any non-:class:`DrillFailedError` raised by ``execute`` or
            ``teardown`` propagates unchanged.
    """
    drill.check_preconditions(ctx)
    try:
        result = _graded_execute(drill, ctx)
    finally:
        drill.teardown(ctx)
    return result


def run_drill(
    drill: Drill, ctx: object, ledger_writer: DrillLedgerWriter
) -> DrillResult:
    """Run a drill and ledger exactly one ``DrillCompleted`` for a graded result.

    A graded result (pass *or* :class:`DrillFailedError`) appends exactly one
    :class:`~windbreak.ledger.events.DrillCompleted` to the operational
    ``ledger_writer``. An unexpected (non-:class:`DrillFailedError`) exception is not
    a graded outcome at all: it propagates and nothing is appended.

    Args:
        drill: The drill to run.
        ctx: The :class:`~windbreak.drills.context.DrillContext` to run against.
        ledger_writer: The operational ledger the ``DrillCompleted`` is appended
            to (distinct from any temp ledger the drill manipulates internally).

    Returns:
        The graded :class:`DrillResult`.

    Raises:
        Exception: Any non-:class:`DrillFailedError` raised by the drill propagates
            unchanged, and nothing is ledgered.
    """
    result = _run_lifecycle(drill, ctx)
    ledger_writer.record(
        DrillCompleted(
            component=_COMPONENT,
            drill=result.drill,
            passed=result.passed,
            evidence=result.evidence,
        )
    )
    return result
