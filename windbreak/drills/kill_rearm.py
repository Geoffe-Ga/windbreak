"""The ``kill-rearm`` drill (issue #59).

Exercises the shipped :class:`~windbreak.riskkernel.kill.KillSwitch` end to end
against a :class:`~windbreak.drills.exchanges.HeldPositionsExchange`: a CLI kill
cancels every resting order and holds every position, fans exactly one
``CancelAllDirective(scope="all_open_orders")`` out to the exchange, and the
correct typed re-arm phrase moves the switch out of ``KILLED`` to ``PAUSED``.

The drill fans the switch's one directive out to the exchange via a small
adapter (:class:`_ExchangeDirectiveSink`): ``KillSwitch`` itself only ever emits
the single directive (SPEC S10.12); a downstream consumer (the real Order
Gateway in production, this adapter in the drill) is what cancels resting orders
order by order, leaving positions untouched. The drill adds no new kill logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from windbreak.drills.exchanges import HeldPositionsExchange
from windbreak.drills.framework import Drill, DrillFailedError, DrillPreconditionError
from windbreak.riskkernel.kill import KillSwitch, KillTrigger
from windbreak.riskkernel.modes import Mode, ModeStateMachine
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter

if TYPE_CHECKING:
    from windbreak.connector.models import Position
    from windbreak.drills.context import DrillContext
    from windbreak.ledger.events import CancelAllDirective, Event


class _NoopAlertSink:
    """A :class:`KillSwitch` alert-dispatcher double that records nothing."""

    def dispatch(self, alert_type: object, message: str) -> None:
        """Accept and discard every dispatched alert.

        Args:
            alert_type: The alert type (ignored).
            message: The alert body (ignored).
        """
        del alert_type, message


class _ExchangeDirectiveSink:
    """Fans a ``CancelAllDirective`` out to every open order on an exchange.

    ``KillSwitch`` emits the single directive; this adapter is what actually
    cancels the resting orders order by order, leaving positions untouched.
    """

    def __init__(self, exchange: HeldPositionsExchange) -> None:
        """Wire the sink to the exchange it cancels orders against.

        Args:
            exchange: The held-positions exchange whose orders are cancelled.
        """
        self._exchange = exchange
        self.received: list[CancelAllDirective] = []

    def submit(self, directive: CancelAllDirective) -> None:
        """Record the directive, then cancel every currently open order.

        Args:
            directive: The cancel-all directive to fan out.
        """
        self.received.append(directive)
        for order in self._exchange.get_open_orders():
            self._exchange.cancel_order(order.id)


def _count(events: list[Event], event_type: str) -> int:
    """Count ledgered events of one type.

    Args:
        events: The recorded events.
        event_type: The discriminator to count.

    Returns:
        The number of matching events.
    """
    return sum(1 for event in events if event.event_type == event_type)


class KillRearmDrill(Drill):
    """Run a clean kill-then-rearm cycle against a held-positions exchange."""

    name: ClassVar[str] = "kill-rearm"

    def check_preconditions(self, ctx: object) -> None:
        """Verify the context carries a held-positions exchange to kill against.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to inspect.

        Raises:
            DrillPreconditionError: If ``ctx.exchange`` is not a
                :class:`HeldPositionsExchange`.
        """
        context = cast("DrillContext", ctx)
        if not isinstance(context.exchange, HeldPositionsExchange):
            raise DrillPreconditionError(
                "kill-rearm requires a HeldPositionsExchange on the context"
            )

    def execute(self, ctx: object) -> dict[str, object]:
        """Kill against the exchange, then re-arm, grading the whole cycle.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to run
                against.

        Returns:
            Evidence recording the held positions, cancelled orders, and re-arm.

        Raises:
            DrillFailedError: If any step of the kill-then-rearm invariant fails.
        """
        context = cast("DrillContext", ctx)
        exchange = cast("HeldPositionsExchange", context.exchange)
        positions_before = exchange.get_positions()
        orders_before = len(exchange.get_open_orders())
        writer = InMemoryKernelLedgerWriter()
        sink = _ExchangeDirectiveSink(exchange)
        switch = KillSwitch(
            ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE),
            writer,
            _NoopAlertSink(),
            directive_sink=sink,
            state_dir=context.state_dir,
            clock=context.clock,
        )
        self._run_kill_phase(switch, exchange, positions_before, writer, sink)
        self._run_rearm_phase(switch, writer)
        return {
            "positions_held": True,
            "open_orders_cancelled": orders_before,
            "cancel_all_directives": len(sink.received),
            "rearmed": True,
            "final_mode": switch.mode.name,
        }

    def teardown(self, ctx: object) -> None:
        """No teardown: the switch writes only into the caller's state dir.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` (unused).
        """
        del ctx

    def _run_kill_phase(
        self,
        switch: KillSwitch,
        exchange: HeldPositionsExchange,
        positions_before: tuple[Position, ...],
        writer: InMemoryKernelLedgerWriter,
        sink: _ExchangeDirectiveSink,
    ) -> None:
        """Kill and assert every order cancelled while positions held.

        Args:
            switch: The kill switch to engage.
            exchange: The exchange whose orders/positions are checked.
            positions_before: The positions snapshot taken before the kill.
            writer: The ledger writer to check ``KillEngaged`` on.
            sink: The directive sink to check the single directive on.

        Raises:
            DrillFailedError: If orders were not all cancelled, a position moved, or
                the kill surface (one directive, one ``KillEngaged``) is wrong.
        """
        switch.kill(KillTrigger.CLI)
        if exchange.get_open_orders():
            raise DrillFailedError({"open_orders_after_kill": "not_empty"})
        if exchange.get_positions() != positions_before:
            raise DrillFailedError({"positions_changed": True})
        if len(sink.received) != 1:
            raise DrillFailedError({"cancel_all_directives": len(sink.received)})
        if _count(writer.events, "KillEngaged") != 1:
            raise DrillFailedError(
                {"kill_engaged_events": _count(writer.events, "KillEngaged")}
            )

    def _run_rearm_phase(
        self, switch: KillSwitch, writer: InMemoryKernelLedgerWriter
    ) -> None:
        """Re-arm with the correct phrase and assert the switch reached PAUSED.

        Args:
            switch: The killed switch to re-arm.
            writer: The ledger writer to check ``KillReArmed`` on.

        Raises:
            DrillFailedError: If the switch did not reach ``PAUSED`` or the re-arm
                was not ledgered exactly once.
        """
        switch.rearm(switch.expected_rearm_phrase(switch.active_kill_sequence))
        if switch.mode is not Mode.PAUSED:
            raise DrillFailedError({"mode_after_rearm": switch.mode.name})
        if _count(writer.events, "KillReArmed") != 1:
            raise DrillFailedError(
                {"kill_rearmed_events": _count(writer.events, "KillReArmed")}
            )
