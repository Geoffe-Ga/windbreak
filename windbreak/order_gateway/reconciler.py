"""The Order Gateway's continuous reconciler (issue #40, SPEC S11.4/S3.2).

The Reconciler periodically diffs the Gateway's tracked resting orders against
the venue's live open-order and fill truth, folding out-of-band effects a real
venue fill feed would surface asynchronously. It follows a **closed allowlist**:
exactly two benign heals are permitted, and everything else halts (SPEC S3.2 --
when in doubt, halt):

    * a Gateway-placed resting order that filled out-of-band (tracked order gone
      from the venue, with a corroborating fill) is a benign missed fill: the
      Reconciler ledgers the ``FILL`` edge and a
      :class:`~windbreak.ledger.events.ReconciliationHealed`, and -- for a close
      -- retires that order's in-flight-closing tally, returning its headroom.

Any unexplained mismatch -- a resting order the Gateway never placed
(``foreign_open_order``), or a tracked order that vanished with no corroborating
fill (``vanished_order_no_fill``) -- ledgers a
:class:`~windbreak.ledger.events.ReconciliationHalted` and latches the *live*
Gateway halted (stopping ``accepting_approvals``), never silently guessing a
benign fill.

:meth:`Reconciler.run` mirrors :meth:`~windbreak.order_gateway.gateway.
OrderGateway.run`'s bounded-loop contract -- always bounded by ``max_cycles``
and/or a ``stop_event``, never an unbounded sleep -- and its interval is whole
seconds (SPEC S6.1, no floats).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from windbreak.ledger.events import ReconciliationHalted, ReconciliationHealed
from windbreak.order_gateway.ledger_writer import apply_and_ledger
from windbreak.order_gateway.recovery import fold_ledger_states, matched_fill_centis
from windbreak.order_gateway.state_machine import OrderEvent, OrderState

if TYPE_CHECKING:
    from windbreak.connector.models import OpenOrder
    from windbreak.order_gateway.gateway import OrderGateway
    from windbreak.order_gateway.ledger_writer import GatewayLedgerWriter
    from windbreak.order_gateway.recovery import (
        LedgerReaderProtocol,
        ReconciliationSourceProtocol,
        TrackedOrder,
    )

#: Component label stamped on every reconciler event.
_COMPONENT = "order_gateway"

#: Default seconds between reconcile cycles -- a whole-second ``int`` (SPEC S6.1).
_DEFAULT_INTERVAL_S = 60

#: The exclusive, timezone-aware lower bound for reading every venue fill: fill
#: attribution is by quantity (a tracked order's already-counted fills are netted
#: out via its ``filled_centis`` baseline), so the window is deliberately open.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Human-readable diagnostics for each halt reason the Reconciler emits.
_HALT_DETAIL: dict[str, str] = {
    "foreign_open_order": (
        "resting order on the venue was never placed through the Gateway"
    ),
    "vanished_order_no_fill": (
        "tracked resting order vanished from the venue with no corroborating fill"
    ),
}


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """The frozen result of one :meth:`Reconciler.run_once` cycle.

    Attributes:
        halted: Whether this cycle latched the Gateway halted.
        healed: How many tracked orders this cycle benignly healed.
        halt_reason: The closed-set halt reason when ``halted``, else ``None``.
    """

    halted: bool
    healed: int
    halt_reason: str | None


class Reconciler:
    """Continuously reconciles a live :class:`OrderGateway` against the venue."""

    __slots__ = (
        "_gateway",
        "_interval",
        "_ledger_reader",
        "_ledger_writer",
        "_source",
    )

    def __init__(
        self,
        gateway: OrderGateway,
        *,
        ledger_reader: LedgerReaderProtocol,
        reconciliation_source: ReconciliationSourceProtocol,
        ledger_writer: GatewayLedgerWriter,
        interval: int = _DEFAULT_INTERVAL_S,
    ) -> None:
        """Wire the Reconciler to the live Gateway and its durable/venue seams.

        Args:
            gateway: The live Gateway whose halt latch and ``accepting_approvals``
                gate this Reconciler flips on a mismatch, and whose tracked
                orders it diffs against the venue.
            ledger_reader: The seam the durable ledger is folded through to find
                a tracked order's current lifecycle state before healing it.
            reconciliation_source: The seam the venue's live open orders and
                fills are read through.
            ledger_writer: The seam heal/halt events are recorded through.
            interval: Whole seconds between cycles in :meth:`run` (SPEC S6.1).
        """
        self._gateway = gateway
        self._ledger_reader = ledger_reader
        self._source = reconciliation_source
        self._ledger_writer = ledger_writer
        self._interval = interval

    def run_once(self) -> ReconcileOutcome:
        """Reconcile every tracked order against the venue exactly once.

        Halts on the first unexplained mismatch (a foreign resting order, or a
        tracked order gone with no fill); otherwise heals every tracked order the
        venue confirms filled out-of-band.

        Returns:
            The :class:`ReconcileOutcome` for this cycle.
        """
        open_orders = self._source.get_open_orders()
        fills = self._source.get_fills(_EPOCH)
        tracked = self._gateway.tracked_orders()
        tracked_ids = frozenset(order.order_id for order in tracked)
        foreign = self._first_foreign(open_orders, tracked_ids)
        if foreign is not None:
            return self._halt("foreign_open_order", foreign.ticker, foreign.id, "")
        open_ids = frozenset(order.id for order in open_orders)
        # Fold the ledger once per cycle, not once per healed order, so a cycle
        # healing K orders stays O(ledger) rather than O(K * ledger).
        states = fold_ledger_states(self._ledger_reader.read_all())
        healed = 0
        for order in tracked:
            if order.order_id in open_ids:
                continue
            new_centis = (
                matched_fill_centis(fills, order.ticker, order.side, order.price_pips)
                - order.filled_centis
            )
            if new_centis <= 0:
                return self._halt(
                    "vanished_order_no_fill",
                    order.ticker,
                    order.order_id,
                    order.client_order_id,
                )
            self._heal(order, states)
            healed += 1
        return ReconcileOutcome(halted=False, healed=healed, halt_reason=None)

    def run(
        self,
        *,
        max_cycles: int | None = None,
        interval: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Reconcile on a bounded loop until the budget or stop event ends it.

        Mirrors :meth:`OrderGateway.run`: the loop is always bounded by
        ``max_cycles`` and/or ``stop_event`` -- never an unbounded sleep -- so it
        terminates deterministically under test and shuts down cleanly on a
        signal in production.

        Args:
            max_cycles: Maximum number of cycles to run before returning.
                ``None`` runs until ``stop_event`` is set.
            interval: Whole seconds to wait between cycles; defaults to the
                interval supplied at construction. ``0`` waits not at all.
            stop_event: Optional event that, once set, ends the loop after the
                current cycle. Defaults to a fresh, never-set event.
        """
        wait_s = self._interval if interval is None else interval
        if stop_event is None:
            stop_event = threading.Event()
        cycles = 0
        while (max_cycles is None or cycles < max_cycles) and not stop_event.is_set():
            cycles += 1
            self.run_once()
            stop_event.wait(wait_s)

    def _first_foreign(
        self, open_orders: tuple[OpenOrder, ...], tracked_ids: frozenset[str]
    ) -> OpenOrder | None:
        """Return the first resting venue order the Gateway never placed.

        Args:
            open_orders: The venue's resting orders.
            tracked_ids: The venue order ids the Gateway is tracking.

        Returns:
            The first untracked resting order, or ``None`` when all are tracked.
        """
        for order in open_orders:
            if order.id not in tracked_ids:
                return order
        return None

    def _halt(
        self, reason: str, ticker: str, order_id: str, coid: str
    ) -> ReconcileOutcome:
        """Ledger a reconciliation halt and latch the live Gateway fail-closed.

        Args:
            reason: The closed-set halt reason.
            ticker: The market ticker the mismatch was on.
            order_id: The venue order id involved.
            coid: The correlated client-order-id, or ``""`` for a foreign order.

        Returns:
            A halted :class:`ReconcileOutcome`.
        """
        self._ledger_writer.record(
            ReconciliationHalted(
                component=_COMPONENT,
                reason=reason,
                ticker=ticker,
                venue_order_id=order_id,
                client_order_id=coid,
                detail=_HALT_DETAIL[reason],
            )
        )
        self._gateway.mark_halted()
        return ReconcileOutcome(halted=True, healed=0, halt_reason=reason)

    def _heal(self, order: TrackedOrder, states: dict[str, OrderState]) -> None:
        """Ledger a benign missed-fill heal and retire the tracked order.

        Advances the order's ledgered lifecycle across the ``FILL`` edge, records
        a :class:`~windbreak.ledger.events.ReconciliationHealed`, and retires the
        order from Gateway tracking (returning a close's in-flight headroom).

        Args:
            order: The tracked order the venue confirmed filled out-of-band.
            states: The per-``client_order_id`` lifecycle states folded once for
                this cycle, defaulting to ``ACKED`` when the order is absent.
        """
        state = states.get(order.client_order_id, OrderState.ACKED)
        apply_and_ledger(
            self._ledger_writer,
            state,
            OrderEvent.FILL,
            client_order_id=order.client_order_id,
        )
        self._ledger_writer.record(
            ReconciliationHealed(
                component=_COMPONENT,
                client_order_id=order.client_order_id,
                action="fill_confirmed",
                detail="matched an out-of-band fill on a tracked resting order",
            )
        )
        self._gateway.retire_tracked_order(order)
