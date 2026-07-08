"""Process skeleton for the Risk Kernel (Process B, SPEC S5.1-S5.3).

This module wires the Risk Kernel's runtime surface:

- :class:`KernelLedgerWriter`, the persistence seam, with a logging and an
  in-memory implementation (mirroring
  :mod:`windbreak.connector.snapshot`'s ``EventLedgerWriter`` trio).
- :class:`RiskKernel`, holding a :class:`~windbreak.riskkernel.modes.Mode`
  state machine, a bounded heartbeat loop that records
  :class:`~windbreak.ledger.events.ModeHeartbeat` events, and a ledgered
  :meth:`RiskKernel.evaluate_intent` that records one ``IntentVetoed`` event
  per rejected intent (or ``IntentApproved`` when the pipeline passes it).
- :func:`main`, a bounded CLI mirroring :mod:`windbreak.main`'s
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
import time
from typing import TYPE_CHECKING, Protocol

from windbreak.config import EvaluationConfig
from windbreak.ledger.events import (
    DemotionTriggerFired,
    Event,
    ModeHeartbeat,
    PromotionEvaluated,
    SignificanceOverrideApplied,
)
from windbreak.logging_setup import configure_logging
from windbreak.riskkernel import checks
from windbreak.riskkernel.demotion import TRIGGER_ACTIONS, resolve_demotion
from windbreak.riskkernel.modes import (
    IllegalModeTransitionError,
    Mode,
    ModeStateMachine,
)
from windbreak.riskkernel.promotion import (
    OVERRIDE_CEILING,
    SIGNIFICANCE_OVERRIDE_ACK_PHRASE,
    OverrideAcknowledgementError,
    build_promotion_gates,
    effective_mode_ceiling,
    evaluate_promotion,
    override_applied_in,
)
from windbreak.riskkernel.verification import VerificationOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from types import FrameType

    from windbreak.riskkernel.context import EvaluationContext
    from windbreak.riskkernel.demotion import DemotionTrigger
    from windbreak.riskkernel.kill import KillIntegration
    from windbreak.riskkernel.promotion import (
        CriterionResult,
        GateEvidence,
        PromotionDecision,
        PromotionGate,
    )
    from windbreak.riskkernel.verification import ReadOnlyVerifier, VerificationSnapshot

#: Component label stamped on every event and log record this process emits.
_COMPONENT = "riskkernel"

#: Event type recorded when the kernel vetoes an intent.
_INTENT_VETOED_EVENT = "IntentVetoed"

#: The single hard-veto reason a KILLED kernel returns, short-circuiting the
#: whole check pipeline (issue #35) rather than accumulating pipeline reasons.
_KILLED_VETO_REASON = "KILLED"

#: Event type recorded when the kernel approves an intent (no check vetoed).
_INTENT_APPROVED_EVENT = "IntentApproved"

#: Event type recorded when a verification breach halts the kernel (issue #32).
_VERIFICATION_MISMATCH_HALT_EVENT = "VerificationMismatchHalt"

#: Payload schema version stamped on kernel-emitted events.
_PAYLOAD_SCHEMA_VERSION = 1

#: Whole seconds between heartbeats when ``--heartbeat-interval`` is omitted.
#: An integer (not a float) because the whole ``riskkernel`` package is on the
#: no-floats path (SPEC S6.1, enforced by ``scripts/lint_no_floats.py``).
_DEFAULT_HEARTBEAT_INTERVAL = 5

#: The ceiling the default (unconfigured) kernel promotes no higher than.
_DEFAULT_MODE_CEILING = Mode.PAPER

#: Demotion destinations reachable via the ordinary safety transition (the
#: rest -- ladder demotions -- go through ``demote_one_rung`` instead).
_SAFETY_DESTINATIONS: frozenset[Mode] = frozenset({Mode.PAUSED, Mode.HALT, Mode.KILLED})

_LOGGER = logging.getLogger("windbreak.riskkernel")


def _default_clock() -> int:
    """Return the current wall clock as whole epoch seconds.

    Casts :func:`time.time` to an ``int`` so the kernel's clock stays off the
    banned float path (SPEC S6.1): epoch seconds are integral here, and the
    verification snapshot's ``verified_at_epoch_s`` is an ``int``.

    Returns:
        The current time, in whole epoch seconds.
    """
    return int(time.time())


def _result_payload(result: CriterionResult) -> dict[str, object]:
    """Project one criterion result into a JSON-safe payload dict.

    Args:
        result: The evaluated criterion result to serialize.

    Returns:
        A dict keyed by field name, with ``comparison`` rendered as its
        ``.name`` string.
    """
    return {
        "criterion_id": result.criterion_id,
        "observed": result.observed,
        "threshold": result.threshold,
        "comparison": result.comparison.name,
        "passed": result.passed,
    }


def _override_promotes(
    gate: PromotionGate, decision: PromotionDecision, override_active: bool
) -> bool:
    """Return whether an active override rescues an otherwise-rejected promotion.

    The significance override may promote past a failing gate only when *every*
    failing criterion is :attr:`~windbreak.riskkernel.promotion.GateCriterion.
    overridable` -- i.e. the sole mandatory significance criterion is the only
    thing standing in the way (SPEC S10.9). A single non-overridable failure
    (e.g. too few resolved forecasts) blocks the bypass entirely, and an
    already-approved decision needs no bypass.

    Args:
        gate: The gate ``decision`` was evaluated against.
        decision: The raw, un-overridden promotion decision.
        override_active: Whether the ledgered significance override is in force.

    Returns:
        ``True`` iff the override is active, the decision was not approved, and
        every failing criterion (of which there is at least one) is overridable.
    """
    overridable_ids = {
        criterion.criterion_id for criterion in gate.criteria if criterion.overridable
    }
    failures = [result for result in decision.results if not result.passed]
    return (
        override_active
        and not decision.approved
        and bool(failures)
        and all(result.criterion_id in overridable_ids for result in failures)
    )


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
    ``windbreak.riskkernel`` logger with the event type in the message so
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
        *,
        verifier: ReadOnlyVerifier | None = None,
        clock: Callable[[], int] | None = None,
        evaluation_config: EvaluationConfig | None = None,
        kill_integration: KillIntegration | None = None,
    ) -> None:
        """Initialize the kernel.

        Args:
            ledger_writer: The seam every event is recorded through.
            mode_machine: The operating-mode state machine. Defaults to a fresh
                ``RESEARCH`` machine ceilinged at :data:`_DEFAULT_MODE_CEILING`.
            verifier: The read-only exchange verifier run each beat, or ``None``
                to disable verification (the pre-issue-#32 behavior). Issue #32.
            clock: A zero-argument callable returning the current epoch second,
                injected so verification cycles are deterministic under test.
                Defaults to :func:`_default_clock`.
            evaluation_config: The promotion-threshold config the gates are
                built from. Defaults to a stock :class:`EvaluationConfig`.
            kill_integration: The kill switch and its trigger adapters (issue
                #35), or ``None`` to run without kill wiring. When present, its
                watcher is polled each beat and its monitor is fed each
                verification outcome.
        """
        self._ledger_writer = ledger_writer
        self._kill_integration = kill_integration
        self._mode_machine = (
            mode_machine
            if mode_machine is not None
            else ModeStateMachine(mode_ceiling=_DEFAULT_MODE_CEILING)
        )
        self._verifier = verifier
        self._clock = clock if clock is not None else _default_clock
        self._latest_verification: VerificationSnapshot | None = None
        config = (
            evaluation_config if evaluation_config is not None else EvaluationConfig()
        )
        self._promotion_gates: Mapping[Mode, PromotionGate] = build_promotion_gates(
            config
        )
        self._override_applied = False

    @classmethod
    def for_testing(cls) -> RiskKernel:
        """Build a kernel wired to an in-memory writer for assertions.

        Returns:
            A :class:`RiskKernel` whose ``ledger_writer`` is an
            :class:`InMemoryKernelLedgerWriter` retaining every recorded event.
        """
        return cls(InMemoryKernelLedgerWriter())

    @classmethod
    def from_events(
        cls,
        events: Iterable[Event],
        ledger_writer: KernelLedgerWriter,
        *,
        mode_machine: ModeStateMachine | None = None,
        evaluation_config: EvaluationConfig | None = None,
    ) -> RiskKernel:
        """Rebuild a kernel, replaying durable override state from the ledger.

        The significance-override cap is durable, ledgered state rather than
        process memory, so a kernel rebuilt over a history that recorded a
        :class:`~windbreak.ledger.events.SignificanceOverrideApplied` event comes
        back with the cap already in force.

        Args:
            events: The event history to replay override state from.
            ledger_writer: The writer the rebuilt kernel records new events to.
            mode_machine: The operating-mode state machine to adopt.
            evaluation_config: The promotion-threshold config for the gates.

        Returns:
            A :class:`RiskKernel` whose override cap reflects ``events``.
        """
        kernel = cls(
            ledger_writer,
            mode_machine=mode_machine,
            evaluation_config=evaluation_config,
        )
        kernel._override_applied = override_applied_in(events)
        return kernel

    @property
    def ledger_writer(self) -> KernelLedgerWriter:
        """Return the writer events are recorded through."""
        return self._ledger_writer

    @property
    def mode(self) -> Mode:
        """Return the kernel's current operating mode."""
        return self._mode_machine.mode

    @property
    def mode_ceiling_effective(self) -> Mode:
        """Return the effective ceiling, folding in any significance override."""
        return effective_mode_ceiling(
            self._mode_machine.mode_ceiling, self._override_applied
        )

    def request_promotion(self, evidence: GateEvidence) -> PromotionDecision:
        """Evaluate the current mode's promotion gate and, if cleared, promote.

        Records exactly one
        :class:`~windbreak.ledger.events.PromotionEvaluated` event per attempt --
        whether the evidence approved or rejected the promotion -- *before*
        attempting the mode change, so the audit trail always captures the
        evaluation even when the subsequent ceiling check blocks the move. The
        ledger write is deliberately unguarded (fail-closed), matching
        :meth:`evaluate_intent`.

        With an active significance override, a promotion past a *failing*
        mandatory significance criterion is applied -- reflected in the kernel's
        mode and the event's ``override_bypassed=True`` -- even though the
        returned ``decision.approved`` remains ``False`` (the override changes
        the kernel's mode, never the pure evaluation). The override bypasses
        *only* the ``overridable`` significance criterion: any other failing
        criterion still blocks promotion. The override's permanent
        ``LIVE_MICRO`` ceiling keeps full ``LIVE`` unreachable, so a bypassed
        PAPER -> LIVE_MICRO promotion succeeds while a later LIVE_MICRO -> LIVE
        attempt still raises ``ModeCeilingExceededError``.

        Args:
            evidence: The promotion-readiness evidence snapshot.

        Returns:
            The raw :class:`~windbreak.riskkernel.promotion.PromotionDecision`
            (its ``approved`` reflects the criteria alone, ignoring any
            override).

        Raises:
            IllegalModeTransitionError: If the current mode has no promotion
                gate (a safety mode, or ``LIVE`` at the top of the ladder). No
                event is recorded in this case.
            ModeCeilingExceededError: If the promotion cleared (on its merits or
                via the override) but an active ceiling blocks the target rung.
                The ``PromotionEvaluated`` event is still recorded first, and the
                mode is left unchanged.
        """
        current = self._mode_machine.mode
        gate = self._promotion_gates.get(current)
        if gate is None:
            raise IllegalModeTransitionError(f"no promotion gate from {current.name}")
        decision = evaluate_promotion(gate, evidence)
        override_bypassed = _override_promotes(gate, decision, self._override_applied)
        self._ledger_writer.record(
            PromotionEvaluated(
                component=_COMPONENT,
                source_mode=gate.source.name,
                target_mode=gate.target.name,
                approved=decision.approved,
                override_bypassed=override_bypassed,
                evidence=evidence.to_payload(),
                results=[_result_payload(result) for result in decision.results],
            )
        )
        if decision.approved or override_bypassed:
            self._mode_machine.promote_one_rung(
                effective_ceiling=self.mode_ceiling_effective
            )
        return decision

    def fire_demotion_trigger(self, trigger: DemotionTrigger) -> Mode | None:
        """Fire a demotion trigger, ledgering the firing and any transition.

        Records exactly one
        :class:`~windbreak.ledger.events.DemotionTriggerFired` event per firing,
        including a no-op firing (``transitioned=False``). Safety destinations
        (``PAUSED``/``HALT``/``KILLED``) move via the ordinary transition; a
        one-rung ladder demotion moves via ``demote_one_rung``. Never raises
        from a safety mode -- every trigger resolves cleanly (possibly to a
        no-op) -- and a ``KILLED`` kernel stays ``KILLED``.

        Args:
            trigger: The demotion trigger to fire.

        Returns:
            The resolved destination mode, or ``None`` for a no-op firing.
        """
        current = self._mode_machine.mode
        action = TRIGGER_ACTIONS[trigger]
        destination = resolve_demotion(current, trigger)
        if destination is not None:
            self._apply_demotion(destination)
        to_mode = destination if destination is not None else current
        self._ledger_writer.record(
            DemotionTriggerFired(
                component=_COMPONENT,
                trigger=trigger.name,
                action=action.name,
                from_mode=current.name,
                to_mode=to_mode.name,
                transitioned=destination is not None,
            )
        )
        return destination

    def _apply_demotion(self, destination: Mode) -> None:
        """Move to a resolved demotion destination via the right primitive.

        Args:
            destination: The resolved destination mode (never ``None``).
        """
        if destination in _SAFETY_DESTINATIONS:
            self._mode_machine.transition(destination)
        else:
            self._mode_machine.demote_one_rung()

    def apply_ledgered_override(self, operator_ack: str) -> None:
        """Apply the one-way significance-gate override on the exact phrase.

        On a verbatim, case-sensitive match, records a
        :class:`~windbreak.ledger.events.SignificanceOverrideApplied` event and
        caps the effective ceiling at ``LIVE_MICRO`` permanently (no API ever
        unsets it). A mismatch records nothing and changes nothing.

        Args:
            operator_ack: The acknowledgement phrase the operator typed.

        Raises:
            OverrideAcknowledgementError: If ``operator_ack`` does not equal
                :data:`SIGNIFICANCE_OVERRIDE_ACK_PHRASE` exactly. No event is
                recorded and the ceiling is unchanged.
        """
        if operator_ack != SIGNIFICANCE_OVERRIDE_ACK_PHRASE:
            raise OverrideAcknowledgementError(
                "significance-override acknowledgement phrase does not match"
            )
        self._ledger_writer.record(
            SignificanceOverrideApplied(
                component=_COMPONENT,
                operator_ack=operator_ack,
                ceiling=OVERRIDE_CEILING.name,
            )
        )
        self._override_applied = True

    def run_verification_cycle(self) -> None:
        """Run one verification cycle, halting the kernel on a breach.

        A no-op when no verifier is configured. Otherwise runs the verifier at
        the injected clock's current epoch second, retains the resulting
        snapshot for the next :meth:`evaluate_intent`, and -- on a ``BREACH``
        outcome -- halts the kernel (see :meth:`_halt_on_breach`).
        """
        if self._verifier is None:
            return
        snapshot = self._verifier.run_cycle(self._clock())
        self._latest_verification = snapshot
        self._feed_mismatch_monitor(snapshot.outcome)
        if snapshot.outcome is VerificationOutcome.BREACH:
            self._halt_on_breach(snapshot)

    def _feed_mismatch_monitor(self, outcome: VerificationOutcome) -> None:
        """Feed one verification outcome to the wired mismatch monitor, if any.

        A no-op unless a kill integration with a monitor is wired (issue #35);
        the monitor auto-kills on a sustained run of breaches.

        Args:
            outcome: The latest verification outcome to fold in.
        """
        integration = self._kill_integration
        if integration is None or integration.monitor is None:
            return
        integration.monitor.observe(outcome)

    def _halt_on_breach(self, snapshot: VerificationSnapshot) -> None:
        """Transition to HALT on a breach, unless already HALT or KILLED.

        ``HALT -> HALT`` and ``KILLED -> HALT`` are both illegal on the mode
        ladder (the state machine forbids same-mode moves and treats KILLED as a
        dead end), so this guard makes a repeated breach idempotent: the halt
        fires -- and its ``VerificationMismatchHalt`` event is recorded -- exactly
        once, on the true transition.

        Args:
            snapshot: The breaching snapshot, whose drift is recorded for audit.
        """
        if self._mode_machine.mode in {Mode.HALT, Mode.KILLED}:
            return
        self._mode_machine.transition(Mode.HALT)
        self._ledger_writer.record(
            Event(
                event_type=_VERIFICATION_MISMATCH_HALT_EVENT,
                component=_COMPONENT,
                payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
                payload={
                    "cash_drift": snapshot.cash_drift.value,
                    "verified_at_epoch_s": snapshot.verified_at_epoch_s,
                },
            )
        )

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

        Each beat also runs one verification cycle (a no-op when no verifier is
        configured), so a configured kernel cross-checks the venue once per beat
        and halts on a breach.

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
            self.run_verification_cycle()
            self._poll_kill_triggers()
            stop_event.wait(heartbeat_interval)

    def _poll_kill_triggers(self) -> None:
        """Poll the wired kill-file watcher once, if any (issue #35).

        A no-op when no kill integration -- or no watcher within it -- is wired,
        so the ordinary heartbeat path is untouched until a kill switch is
        actually installed.
        """
        integration = self._kill_integration
        if integration is None or integration.watcher is None:
            return
        integration.watcher.poll_once(self._clock())

    def evaluate_intent(
        self, intent: checks.OrderIntent, context: EvaluationContext
    ) -> checks.Decision:
        """Evaluate an intent and record its verdict to the ledger.

        Stamps the kernel's own tracked mode onto a copy of ``context`` (via
        :func:`dataclasses.replace`) before evaluating, so a caller-supplied
        ``context.mode`` is never trusted over the kernel's authority; the
        caller's original context object is left untouched.

        When a verifier is configured, the kernel's own latest verification
        snapshot (or ``None`` before the first cycle, failing closed) is stamped
        onto the effective context and its observed cash / drift rewrite the
        account, so verification -- not the caller -- feeds the floor (issue
        #32). Without a verifier, the caller-supplied ``context.verification`` is
        left untouched (the unit-test seam).

        Records exactly one event reflecting the true verdict: ``IntentVetoed``
        when any check vetoes (with the veto reasons), or ``IntentApproved``
        when the pipeline passes the intent (with empty reasons). Gating the
        event type on ``decision.vetoed`` -- rather than always emitting
        ``IntentVetoed`` -- keeps the audit trail correct: 3 of the 24 SPEC
        S10.3 checks remain stubs, so no real context yet yields a
        fully-approving decision, but the approving branch is already correct
        for when that logic lands.

        Args:
            intent: The order intent to evaluate.
            context: The evaluation context supplied by the caller.

        Returns:
            The :class:`~windbreak.riskkernel.checks.Decision`, carrying the
            pipeline's ``vetoed``/``reasons`` and marked ledgered.
        """
        if self._mode_machine.mode is Mode.KILLED:
            return self._veto_killed(intent)
        effective = dataclasses.replace(context, mode=self._mode_machine.mode)
        if self._verifier is not None:
            effective = self._stamp_verification(effective)
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

    def _veto_killed(self, intent: checks.OrderIntent) -> checks.Decision:
        """Hard-veto an intent on a KILLED kernel without running the pipeline.

        The kill switch's dead-hand at the evaluation seam (issue #35): a
        ``KILLED`` kernel approves nothing, so it short-circuits the whole check
        pipeline and returns the single ``"KILLED"`` reason -- distinct from any
        ordinary multi-reason pipeline veto -- while still recording exactly one
        ``IntentVetoed`` event, so the audit trail captures every rejected
        intent even while dead.

        Args:
            intent: The order intent rejected out of hand.

        Returns:
            A vetoed, ledgered :class:`~windbreak.riskkernel.checks.Decision`
            carrying only the ``"KILLED"`` reason.
        """
        self._ledger_writer.record(
            Event(
                event_type=_INTENT_VETOED_EVENT,
                component=_COMPONENT,
                payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
                payload={
                    "intent_id": intent.intent_id,
                    "reasons": [_KILLED_VETO_REASON],
                },
            )
        )
        return checks.Decision(
            vetoed=True, reasons=(_KILLED_VETO_REASON,), ledgered=True
        )

    def _stamp_verification(self, context: EvaluationContext) -> EvaluationContext:
        """Stamp the kernel's latest verification snapshot onto a context copy.

        Overrides any caller-supplied ``context.verification`` with the kernel's
        own latest snapshot -- or ``None`` before the first cycle, failing closed
        so every reconciliation check vetoes on the missing snapshot rather than
        trusting the caller. When a snapshot exists, the account's verified
        available cash and reconciliation-uncertainty buffer are rewritten from
        it (observed cash and observed drift), so the floor invariant consumes
        the verified figures.

        Args:
            context: The mode-stamped effective context to augment.

        Returns:
            A context copy carrying the latest snapshot and, when present, the
            verification-derived account terms.
        """
        snapshot = self._latest_verification
        if snapshot is None:
            return dataclasses.replace(context, verification=None)
        account = dataclasses.replace(
            context.account,
            exchange_verified_available_cash=snapshot.exchange_verified_available_cash,
            reconciliation_uncertainty_buffer=snapshot.cash_drift,
        )
        return dataclasses.replace(context, verification=snapshot, account=account)


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
    """Build the ``windbreak.riskkernel`` bounded-heartbeat CLI parser.

    Both options parse as non-negative integers (a negative value is an
    argparse usage error, exit code 2), matching :mod:`windbreak.main`'s
    non-negative parsing convention. The interval is whole seconds because the
    ``riskkernel`` package is float-free (SPEC S6.1).

    Returns:
        A parser exposing ``--heartbeat-interval`` and ``--max-beats``.
    """
    parser = argparse.ArgumentParser(
        prog="windbreak-riskkernel",
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
