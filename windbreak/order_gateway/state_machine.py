"""The Order Gateway's order-lifecycle state machine (SPEC S5.1).

An order moves through a fixed lifecycle -- from a freshly created intent, into
submission, through fills or cancellation, and finally into reconciliation or
dispute -- driven by discrete events. :class:`OrderState` names the 14 lifecycle
states, :class:`OrderEvent` the 13 events, and :data:`LEGAL_TRANSITIONS` the
exact set of ``(state, event) -> target`` edges the machine permits.

The transition table is pure, read-only data (a :class:`types.MappingProxyType`
a caller cannot corrupt), and :func:`transition` is a single dict lookup over
it: a legal ``(state, event)`` returns its pinned target, and every other pair
raises :class:`IllegalTransitionError` naming both the offending state and event so
an operator can diagnose a rejected move from a single log line. Keeping the
edges as data -- never as branching code -- keeps :func:`transition` trivial and
the legal set auditable in one place.
"""

from __future__ import annotations

from enum import Enum, auto
from types import MappingProxyType
from typing import Final


class OrderState(Enum):
    """A lifecycle state an order may occupy (SPEC S5.1).

    Attributes:
        INTENT_CREATED: A verified intent exists but has not been approved.
        APPROVED: The intent cleared approval and awaits submission.
        SUBMISSION_REQUESTED: Submission to the venue has been requested.
        SUBMITTED: The order was sent to the venue, awaiting acknowledgement.
        ACKED: The venue acknowledged the order.
        PARTIAL_FILL: The order is partially filled and still working.
        FILLED: The order is completely filled.
        CANCEL_REQUESTED: A cancellation has been requested.
        CANCELLED: The order was cancelled.
        EXPIRED: The order expired before completing.
        REJECTED: The venue rejected the order.
        RECONCILED: The terminal order was reconciled against the venue.
        DISPUTED: A reconciliation discrepancy is under dispute.
        SETTLEMENT_REVERSED: A prior settlement was reversed.
    """

    INTENT_CREATED = auto()
    APPROVED = auto()
    SUBMISSION_REQUESTED = auto()
    SUBMITTED = auto()
    ACKED = auto()
    PARTIAL_FILL = auto()
    FILLED = auto()
    CANCEL_REQUESTED = auto()
    CANCELLED = auto()
    EXPIRED = auto()
    REJECTED = auto()
    RECONCILED = auto()
    DISPUTED = auto()
    SETTLEMENT_REVERSED = auto()


class OrderEvent(Enum):
    """An event that may drive an :class:`OrderState` transition (SPEC S5.1).

    Attributes:
        APPROVE: Approve a created intent.
        REQUEST_SUBMISSION: Request submission of an approved order.
        SUBMIT: Send the order to the venue.
        ACK: Record the venue's acknowledgement.
        PARTIAL_FILL: Record a partial fill.
        FILL: Record a complete fill.
        REQUEST_CANCEL: Request cancellation of a working order.
        CANCEL: Record the cancellation.
        EXPIRE: Record that the order expired.
        REJECT: Record the venue's rejection.
        RECONCILE: Reconcile a terminal order against the venue.
        DISPUTE: Raise a reconciliation dispute.
        SETTLEMENT_REVERSE: Record a settlement reversal.
    """

    APPROVE = auto()
    REQUEST_SUBMISSION = auto()
    SUBMIT = auto()
    ACK = auto()
    PARTIAL_FILL = auto()
    FILL = auto()
    REQUEST_CANCEL = auto()
    CANCEL = auto()
    EXPIRE = auto()
    REJECT = auto()
    RECONCILE = auto()
    DISPUTE = auto()
    SETTLEMENT_REVERSE = auto()


class IllegalTransitionError(Exception):
    """Raised when no legal edge exists for a ``(state, event)`` pair.

    Attributes:
        state: The state the illegal move was attempted from.
        event: The event that has no legal edge out of ``state``.
    """

    def __init__(self, state: OrderState, event: OrderEvent) -> None:
        """Build the error, recording and naming both offending inputs.

        Args:
            state: The state the illegal move was attempted from.
            event: The event with no legal edge out of ``state``.
        """
        self.state = state
        self.event = event
        super().__init__(f"no legal transition from {state.name} on {event.name}")


#: The exact legal lifecycle edges, as pure ``(state, event) -> target`` data.
#: Kept mutable and private here, then exposed read-only as
#: :data:`LEGAL_TRANSITIONS`.
_LEGAL_TRANSITIONS: dict[tuple[OrderState, OrderEvent], OrderState] = {
    (OrderState.INTENT_CREATED, OrderEvent.APPROVE): OrderState.APPROVED,
    (OrderState.APPROVED, OrderEvent.REQUEST_SUBMISSION): (
        OrderState.SUBMISSION_REQUESTED
    ),
    (OrderState.SUBMISSION_REQUESTED, OrderEvent.SUBMIT): OrderState.SUBMITTED,
    (OrderState.SUBMITTED, OrderEvent.ACK): OrderState.ACKED,
    (OrderState.SUBMITTED, OrderEvent.REJECT): OrderState.REJECTED,
    (OrderState.ACKED, OrderEvent.PARTIAL_FILL): OrderState.PARTIAL_FILL,
    (OrderState.ACKED, OrderEvent.FILL): OrderState.FILLED,
    (OrderState.ACKED, OrderEvent.REQUEST_CANCEL): OrderState.CANCEL_REQUESTED,
    (OrderState.ACKED, OrderEvent.EXPIRE): OrderState.EXPIRED,
    (OrderState.PARTIAL_FILL, OrderEvent.PARTIAL_FILL): OrderState.PARTIAL_FILL,
    (OrderState.PARTIAL_FILL, OrderEvent.FILL): OrderState.FILLED,
    (OrderState.PARTIAL_FILL, OrderEvent.REQUEST_CANCEL): (OrderState.CANCEL_REQUESTED),
    (OrderState.PARTIAL_FILL, OrderEvent.EXPIRE): OrderState.EXPIRED,
    (OrderState.CANCEL_REQUESTED, OrderEvent.CANCEL): OrderState.CANCELLED,
    (OrderState.CANCEL_REQUESTED, OrderEvent.FILL): OrderState.FILLED,
    (OrderState.CANCEL_REQUESTED, OrderEvent.PARTIAL_FILL): (
        OrderState.CANCEL_REQUESTED
    ),
    (OrderState.FILLED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.FILLED, OrderEvent.DISPUTE): OrderState.DISPUTED,
    (OrderState.CANCELLED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.CANCELLED, OrderEvent.DISPUTE): OrderState.DISPUTED,
    (OrderState.EXPIRED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.EXPIRED, OrderEvent.DISPUTE): OrderState.DISPUTED,
    (OrderState.REJECTED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.REJECTED, OrderEvent.DISPUTE): OrderState.DISPUTED,
    (OrderState.DISPUTED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.RECONCILED, OrderEvent.SETTLEMENT_REVERSE): (
        OrderState.SETTLEMENT_REVERSED
    ),
}

#: The read-only view of :data:`_LEGAL_TRANSITIONS` exposed to callers, so the
#: machine's rules can never be corrupted at runtime.
LEGAL_TRANSITIONS: Final = MappingProxyType(_LEGAL_TRANSITIONS)


def transition(state: OrderState, event: OrderEvent) -> OrderState:
    """Return the target state for ``(state, event)`` or reject the move.

    Args:
        state: The current lifecycle state.
        event: The event driving the transition.

    Returns:
        The pinned target state for a legal edge.

    Raises:
        IllegalTransitionError: If ``(state, event)`` is not a legal edge; the error
            names both ``state`` and ``event``.
    """
    try:
        return LEGAL_TRANSITIONS[(state, event)]
    except KeyError as exc:
        raise IllegalTransitionError(state, event) from exc
