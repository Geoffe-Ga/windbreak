"""The ``reconciliation-mismatch`` drill (issue #59).

Composes two independent, already-shipped mechanisms into one scenario:

    * :class:`~windbreak.order_gateway.reconciler.Reconciler`, driven by a
      lightweight scripted gateway/source double, whose ``run_once`` halts on a
      tracked order that vanished from the venue with no corroborating fill, and
      reconciles cleanly on a fresh cycle against a matching (empty) venue.
    * :class:`~windbreak.riskkernel.kill.ReconciliationMismatchMonitor`, whose
      first ``BREACH`` observation at ``threshold=1`` kills the switch and
      dispatches exactly one ``HALT_KILL`` alert.

The scripted doubles here mirror those the tests drive; the drill adds no new
reconciliation or kill logic, only orchestrates the shipped seams.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from windbreak.alerts.registry import AlertType
from windbreak.drills.framework import Drill, DrillFailedError
from windbreak.order_gateway.reconciler import Reconciler
from windbreak.order_gateway.recovery import TrackedOrder
from windbreak.riskkernel.kill import KillSwitch, ReconciliationMismatchMonitor
from windbreak.riskkernel.modes import Mode, ModeStateMachine
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter
from windbreak.riskkernel.verification import VerificationOutcome

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from windbreak.connector.models import Fill, OpenOrder
    from windbreak.drills.context import DrillContext
    from windbreak.ledger.store import LedgerRecord
    from windbreak.order_gateway.gateway import OrderGateway
    from windbreak.order_gateway.reconciler import ReconcileOutcome

#: The market ticker the scripted scenario runs on.
_TICKER = "MKT-RECON"

#: The closed-set halt reason a vanished-order-no-fill mismatch must produce.
_VANISHED_NO_FILL = "vanished_order_no_fill"


class _FakeGateway:
    """A minimal :class:`Reconciler`-facing gateway double.

    Exposes exactly the surface the Reconciler reads/mutates, never a real
    :class:`OrderGateway`, since this drill's scenario is scripted end to end.
    """

    def __init__(self, tracked: tuple[TrackedOrder, ...]) -> None:
        """Seed the gateway with its resting tracked orders.

        Args:
            tracked: The tracked resting orders the venue is diffed against.
        """
        self._tracked = list(tracked)
        self.halted = False
        self.accepting_approvals = True

    def tracked_orders(self) -> tuple[TrackedOrder, ...]:
        """Return the currently tracked resting orders.

        Returns:
            The tracked orders.
        """
        return tuple(self._tracked)

    def mark_halted(self) -> None:
        """Latch halted and stop accepting approvals."""
        self.halted = True
        self.accepting_approvals = False

    def retire_tracked_order(self, order: TrackedOrder) -> None:
        """Remove ``order`` from the tracked set.

        Args:
            order: The tracked order to retire.
        """
        self._tracked = [o for o in self._tracked if o.order_id != order.order_id]


class _ScriptedSource:
    """A reconciliation source returning one fixed venue snapshot every call."""

    def __init__(
        self, open_orders: tuple[OpenOrder, ...], fills: tuple[Fill, ...]
    ) -> None:
        """Seed the fixed ``(open_orders, fills)`` snapshot.

        Args:
            open_orders: The venue's resting orders.
            fills: The venue's fills.
        """
        self._open_orders = open_orders
        self._fills = fills

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Return the seeded open orders.

        Returns:
            The seeded open orders.
        """
        return self._open_orders

    def get_fills(self, since: datetime, /) -> tuple[Fill, ...]:
        """Return the seeded fills, ignoring ``since``.

        Args:
            since: The (ignored) lower bound on fill time.

        Returns:
            The seeded fills.
        """
        del since
        return self._fills


class _EmptyLedgerReader:
    """A ledger reader double that always reads back no records."""

    def read_all(self) -> list[LedgerRecord]:
        """Return an empty record list.

        Returns:
            An empty list.
        """
        return []


class _RecordingAlertSink:
    """A :class:`KillSwitch` alert-dispatcher double recording dispatched types."""

    def __init__(self) -> None:
        """Initialize with an empty dispatch log."""
        self.dispatched: list[object] = []

    def dispatch(self, alert_type: object, message: str) -> None:
        """Record the dispatched alert type, discarding the message.

        Args:
            alert_type: The alert type dispatched.
            message: The alert body (ignored).
        """
        del message
        self.dispatched.append(alert_type)

    def count(self, alert_type: object) -> int:
        """Return how many times ``alert_type`` was dispatched.

        Args:
            alert_type: The alert type to count.

        Returns:
            The matching dispatch count.
        """
        return sum(1 for recorded in self.dispatched if recorded == alert_type)


def _tracked_order() -> TrackedOrder:
    """Build one representative Gateway-placed tracked order.

    Returns:
        A tracked buy order resting on the scenario ticker.
    """
    return TrackedOrder(
        client_order_id="coid-1",
        order_id="venue-1",
        ticker=_TICKER,
        side="yes",
        price_pips=5000,
        size_centis=100,
        action="buy",
        filled_centis=0,
    )


def _reconcile_once(gateway: _FakeGateway, source: _ScriptedSource) -> ReconcileOutcome:
    """Run one reconciliation cycle over a scripted gateway/venue snapshot.

    Args:
        gateway: The scripted gateway double.
        source: The scripted venue source double.

    Returns:
        The cycle's :class:`ReconcileOutcome`.
    """
    reconciler = Reconciler(
        cast("OrderGateway", gateway),
        ledger_reader=_EmptyLedgerReader(),
        reconciliation_source=source,
        ledger_writer=InMemoryKernelLedgerWriter(),
    )
    return reconciler.run_once()


class ReconciliationMismatchDrill(Drill):
    """Halt on a scripted mismatch, reconcile clean, and auto-kill on breach."""

    name: ClassVar[str] = "reconciliation-mismatch"

    def check_preconditions(self, ctx: object) -> None:
        """No preconditions: the scenario is fully self-scripted.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` (unused).
        """
        del ctx

    def execute(self, ctx: object) -> dict[str, object]:
        """Run the halt cycle, the clean cycle, and the auto-kill alert leg.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext`; only its
                injected clock is used (by the alert leg's kill switch).

        Returns:
            Evidence recording each phase's graded outcome.

        Raises:
            DrillFailedError: If any phase deviates from the pinned contract.
        """
        context = cast("DrillContext", ctx)
        halt = _reconcile_once(
            _FakeGateway((_tracked_order(),)), _ScriptedSource((), ())
        )
        clean = _reconcile_once(_FakeGateway(()), _ScriptedSource((), ()))
        killed, halt_kill_alerts = self._alert_leg(context.clock)
        self._grade(halt, clean, killed, halt_kill_alerts)
        return {
            "first_cycle_halted": halt.halted,
            "halt_reason": halt.halt_reason,
            "second_cycle_reconciled": not clean.halted,
            "auto_killed": killed,
            "halt_kill_alerts": halt_kill_alerts,
        }

    def teardown(self, ctx: object) -> None:
        """No teardown: the scenario holds no external resources.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` (unused).
        """
        del ctx

    def _alert_leg(self, clock: Callable[[], int]) -> tuple[bool, int]:
        """Observe one breach at ``threshold=1`` and read the kill/alert result.

        Args:
            clock: The injected epoch-second clock (so the kill switch never
                reads the wall clock inside the drill).

        Returns:
            A ``(killed, halt_kill_alerts)`` pair: whether the switch reached
            ``KILLED`` and how many ``HALT_KILL`` alerts fired.
        """
        sink = _RecordingAlertSink()
        mode_machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE)
        switch = KillSwitch(
            mode_machine, InMemoryKernelLedgerWriter(), sink, clock=clock
        )
        ReconciliationMismatchMonitor(switch, threshold=1).observe(
            VerificationOutcome.BREACH
        )
        return mode_machine.mode is Mode.KILLED, sink.count(AlertType.HALT_KILL)

    def _grade(
        self,
        halt: ReconcileOutcome,
        clean: ReconcileOutcome,
        killed: bool,
        halt_kill_alerts: int,
    ) -> None:
        """Fail the drill unless every phase matched the pinned contract.

        Args:
            halt: The first (halting) cycle's outcome.
            clean: The second (clean) cycle's outcome.
            killed: Whether the alert leg killed the switch.
            halt_kill_alerts: How many ``HALT_KILL`` alerts the alert leg fired.

        Raises:
            DrillFailedError: If the halt cycle did not halt with the expected
                reason, the clean cycle halted, or the alert leg did not kill and
                fire exactly one ``HALT_KILL``.
        """
        if not halt.halted or halt.halt_reason != _VANISHED_NO_FILL:
            raise DrillFailedError(
                {"halted": halt.halted, "halt_reason": halt.halt_reason}
            )
        if clean.halted:
            raise DrillFailedError({"clean_cycle_halted": True})
        if not killed or halt_kill_alerts != 1:
            raise DrillFailedError(
                {"auto_killed": killed, "halt_kill_alerts": halt_kill_alerts}
            )
