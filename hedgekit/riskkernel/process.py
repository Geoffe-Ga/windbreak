"""Process skeleton for the Risk Kernel (Process B, SPEC S5.1-S5.3).

This module wires the Risk Kernel's runtime surface:

- :class:`KernelLedgerWriter`, the persistence seam, with a logging and an
  in-memory implementation (mirroring
  :mod:`hedgekit.connector.snapshot`'s ``EventLedgerWriter`` trio).
- :class:`RiskKernel`, holding a :class:`~hedgekit.riskkernel.modes.Mode`
  state machine, a bounded heartbeat loop that records
  :class:`~hedgekit.ledger.events.ModeHeartbeat` events, and a ledgered
  :meth:`RiskKernel.evaluate_intent` that records one ``IntentVetoed`` event
  per rejected intent (or ``IntentApproved`` when the pipeline passes it).
- :func:`main`, a bounded CLI mirroring :mod:`hedgekit.main`'s
  ``--heartbeat-interval`` / ``--max-beats`` non-negative parsing conventions.

The heartbeat loop is always bounded by ``max_beats`` and/or a stop event --
there is never an unbounded sleep -- so the process terminates deterministically
under test and shuts down cleanly on a signal in production.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import signal
import threading
from typing import TYPE_CHECKING, Protocol

from hedgekit.ledger.events import Event, ModeHeartbeat
from hedgekit.logging_setup import configure_logging
from hedgekit.riskkernel import checks
from hedgekit.riskkernel.modes import Mode, ModeStateMachine

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import FrameType

    from hedgekit.riskkernel.context import EvaluationContext

#: Component label stamped on every event and log record this process emits.
_COMPONENT = "riskkernel"

#: Event type recorded when the kernel vetoes an intent.
_INTENT_VETOED_EVENT = "IntentVetoed"

#: Event type recorded when the kernel approves an intent (no check vetoed).
_INTENT_APPROVED_EVENT = "IntentApproved"

#: Payload schema version stamped on kernel-emitted events.
_PAYLOAD_SCHEMA_VERSION = 1

#: Whole seconds between heartbeats when ``--heartbeat-interval`` is omitted.
#: An integer (not a float) because the whole ``riskkernel`` package is on the
#: no-floats path (SPEC S6.1, enforced by ``scripts/lint_no_floats.py``).
_DEFAULT_HEARTBEAT_INTERVAL = 5

#: The ceiling the default (unconfigured) kernel promotes no higher than.
_DEFAULT_MODE_CEILING = Mode.PAPER

_LOGGER = logging.getLogger("hedgekit.riskkernel")


class KernelLedgerWriter(Protocol):
    """The seam through which a Risk Kernel event is persisted."""

    def record(self, event: Event) -> None:
        """Persist a kernel event.

        Args:
            event: The event to persist.
        """
        ...


class LoggingKernelLedgerWriter:
    """A :class:`KernelLedgerWriter` that logs events instead of persisting.

    Stands in until a real ledger provides a persisting writer; it emits on the
    ``hedgekit.riskkernel`` logger with the event type in the message so
    operators can see each event.
    """

    def record(self, event: Event) -> None:
        """Log a kernel event as a single structured line.

        Args:
            event: The event to log.
        """
        _LOGGER.info(
            "kernel event recorded event_type=%s",
            event.event_type,
            extra={"component": _COMPONENT, "event_type": event.event_type},
        )


class InMemoryKernelLedgerWriter:
    """A :class:`KernelLedgerWriter` that retains events in memory for tests."""

    def __init__(self) -> None:
        """Initialize with an empty, publicly readable event log."""
        self.events: list[Event] = []

    def record(self, event: Event) -> None:
        """Append a kernel event to the in-memory log.

        Args:
            event: The event to retain.
        """
        self.events.append(event)


class RiskKernel:
    """The Risk Kernel process: heartbeat loop and ledgered veto evaluation.

    Attributes:
        ledger_writer: The writer every emitted event is recorded through
            (read-only).
    """

    def __init__(
        self,
        ledger_writer: KernelLedgerWriter,
        mode_machine: ModeStateMachine | None = None,
    ) -> None:
        """Initialize the kernel.

        Args:
            ledger_writer: The seam every event is recorded through.
            mode_machine: The operating-mode state machine. Defaults to a fresh
                ``RESEARCH`` machine ceilinged at :data:`_DEFAULT_MODE_CEILING`.
        """
        self._ledger_writer = ledger_writer
        self._mode_machine = (
            mode_machine
            if mode_machine is not None
            else ModeStateMachine(mode_ceiling=_DEFAULT_MODE_CEILING)
        )

    @classmethod
    def for_testing(cls) -> RiskKernel:
        """Build a kernel wired to an in-memory writer for assertions.

        Returns:
            A :class:`RiskKernel` whose ``ledger_writer`` is an
            :class:`InMemoryKernelLedgerWriter` retaining every recorded event.
        """
        return cls(InMemoryKernelLedgerWriter())

    @property
    def ledger_writer(self) -> KernelLedgerWriter:
        """Return the writer events are recorded through."""
        return self._ledger_writer

    def _emit_heartbeat(self, beat: int) -> None:
        """Log and record one mode-heartbeat for the current mode.

        Args:
            beat: The 1-based heartbeat sequence number.
        """
        mode_name = self._mode_machine.mode.name
        _LOGGER.info(
            "mode=%s heartbeat beat=%d",
            mode_name,
            beat,
            extra={"component": _COMPONENT},
        )
        self._ledger_writer.record(
            ModeHeartbeat(component=_COMPONENT, mode=mode_name, beat=beat)
        )

    def run(
        self,
        *,
        max_beats: int | None = None,
        heartbeat_interval: int = _DEFAULT_HEARTBEAT_INTERVAL,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Emit heartbeats until the beat budget or stop event ends the loop.

        Args:
            max_beats: Maximum number of heartbeats to emit before returning.
                ``None`` runs until ``stop_event`` is set.
            heartbeat_interval: Whole seconds to wait between beats. ``0`` waits
                not at all; there is never an unbounded sleep.
            stop_event: Optional event that, once set, ends the loop after the
                current beat. Defaults to a fresh, never-set event.
        """
        if stop_event is None:
            stop_event = threading.Event()
        beat = 0
        while (max_beats is None or beat < max_beats) and not stop_event.is_set():
            beat += 1
            self._emit_heartbeat(beat)
            stop_event.wait(heartbeat_interval)

    def evaluate_intent(
        self, intent: checks.OrderIntent, context: EvaluationContext
    ) -> checks.Decision:
        """Evaluate an intent and record its verdict to the ledger.

        Stamps the kernel's own tracked mode onto a copy of ``context`` (via
        :func:`dataclasses.replace`) before evaluating, so a caller-supplied
        ``context.mode`` is never trusted over the kernel's authority; the
        caller's original context object is left untouched.

        Records exactly one event reflecting the true verdict: ``IntentVetoed``
        when any check vetoes (with the veto reasons), or ``IntentApproved``
        when the pipeline passes the intent (with empty reasons). Gating the
        event type on ``decision.vetoed`` -- rather than always emitting
        ``IntentVetoed`` -- keeps the audit trail correct: 7 of the 24 SPEC
        S10.3 checks remain stubs (issues #32/#34), so no real context yet
        yields a fully-approving decision, but the approving branch is already
        correct for when that logic lands.

        Args:
            intent: The order intent to evaluate.
            context: The evaluation context supplied by the caller.

        Returns:
            The :class:`~hedgekit.riskkernel.checks.Decision`, carrying the
            pipeline's ``vetoed``/``reasons`` and marked ledgered.
        """
        effective = dataclasses.replace(context, mode=self._mode_machine.mode)
        decision = checks.evaluate_intent(intent, effective)
        event_type = _INTENT_VETOED_EVENT if decision.vetoed else _INTENT_APPROVED_EVENT
        # Deliberately unguarded (no try/except around the ledger write, unlike
        # connector.snapshot's fail-open writer): for a risk kernel a ledger
        # failure must surface, never be swallowed, so the audit trail can never
        # silently miss a decision. Letting it propagate is the fail-closed choice.
        self._ledger_writer.record(
            Event(
                event_type=event_type,
                component=_COMPONENT,
                payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
                payload={
                    "intent_id": intent.intent_id,
                    "reasons": list(decision.reasons),
                },
            )
        )
        return checks.Decision(
            vetoed=decision.vetoed, reasons=decision.reasons, ledgered=True
        )


def _non_negative_int(raw: str) -> int:
    """Parse a non-negative int for use as an argparse ``type``.

    Args:
        raw: The raw command-line token.

    Returns:
        The parsed integer value.

    Raises:
        argparse.ArgumentTypeError: If ``raw`` is negative.
    """
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the ``hedgekit.riskkernel`` bounded-heartbeat CLI parser.

    Both options parse as non-negative integers (a negative value is an
    argparse usage error, exit code 2), matching :mod:`hedgekit.main`'s
    non-negative parsing convention. The interval is whole seconds because the
    ``riskkernel`` package is float-free (SPEC S6.1).

    Returns:
        A parser exposing ``--heartbeat-interval`` and ``--max-beats``.
    """
    parser = argparse.ArgumentParser(
        prog="hedgekit-riskkernel",
        description="Risk Kernel (Process B) bounded heartbeat loop.",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=_non_negative_int,
        default=_DEFAULT_HEARTBEAT_INTERVAL,
        help="Whole seconds between heartbeats (default: %(default)s).",
    )
    parser.add_argument(
        "--max-beats",
        type=_non_negative_int,
        default=None,
        help="Stop after this many heartbeats (default: run until signalled).",
    )
    return parser


def _install_signal_handlers(stop_event: threading.Event) -> None:
    """Install SIGINT/SIGTERM handlers that request a graceful shutdown.

    Args:
        stop_event: The event set when a shutdown signal is delivered, so an
            in-flight :meth:`RiskKernel.run` unwinds after its current beat.
    """

    def _handle(_signum: int, _frame: FrameType | None) -> None:
        """Request shutdown by setting the stop event."""
        stop_event.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the bounded Risk Kernel heartbeat loop.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code (always 0 on a clean run).
    """
    args = build_parser().parse_args(argv)
    configure_logging(level=logging.INFO)
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    kernel = RiskKernel(LoggingKernelLedgerWriter())
    kernel.run(
        max_beats=args.max_beats,
        heartbeat_interval=args.heartbeat_interval,
        stop_event=stop_event,
    )
    return 0
