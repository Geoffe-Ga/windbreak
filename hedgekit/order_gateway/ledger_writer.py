"""Gateway-side ledger writers and the events they persist (issue #38).

The Order Gateway records its order-lifecycle transitions and its
exchange-status refusals into the append-only ledger. This module supplies the
seam and re-exports the event types that flow through it:

    * :class:`GatewayLedgerWriter` -- the structural protocol the Gateway
      records through, with a :class:`LoggingGatewayLedgerWriter` stand-in (for
      the credential-free CLI), an :class:`InMemoryGatewayLedgerWriter` (for
      tests), and a persisting :class:`SqliteGatewayLedgerWriter` that appends
      every event to a real :class:`~hedgekit.ledger.store.SqliteLedgerStore`,
      mirroring the Risk Kernel writer triad in
      :mod:`hedgekit.riskkernel.process`.
    * :class:`OrderTransitionLedgered` / :class:`SubmissionRefused` /
      :class:`ReduceOnlyRefused` / :class:`ReduceOnlyViolation` -- re-exported
      from :mod:`hedgekit.ledger.events`, which owns the canonical event-schema
      contract (issue #40 moved them there so recovery can reconstruct them from
      the registry). Existing importers keep importing them from here unchanged.
    * :func:`apply_and_ledger` -- computes a state transition and records it
      *before* returning the target, structurally enforcing write-before-next-
      action: if the ledger write raises, the caller never receives the target
      and can never proceed to the next action (e.g. submitting the order).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from hedgekit.ledger.events import OrderTransitionLedgered as OrderTransitionLedgered
from hedgekit.ledger.events import ReduceOnlyRefused as ReduceOnlyRefused
from hedgekit.ledger.events import ReduceOnlyViolation as ReduceOnlyViolation
from hedgekit.ledger.events import SubmissionRefused as SubmissionRefused
from hedgekit.order_gateway.state_machine import transition

if TYPE_CHECKING:
    from hedgekit.ledger.events import Event
    from hedgekit.ledger.store import LedgerStore
    from hedgekit.order_gateway.state_machine import OrderEvent, OrderState

#: Component label stamped on every Gateway event this module builds.
_COMPONENT = "order_gateway"

_LOGGER = logging.getLogger("hedgekit.order_gateway")


class GatewayLedgerWriter(Protocol):
    """The seam through which an Order Gateway event is persisted."""

    def record(self, event: Event) -> None:
        """Persist a Gateway event.

        Args:
            event: The event to persist.
        """
        ...


class LoggingGatewayLedgerWriter:
    """A :class:`GatewayLedgerWriter` that logs events instead of persisting.

    Stands in for the credential-free CLI heartbeat; it emits on the
    ``hedgekit.order_gateway`` logger with the event type in the message so
    operators can see each event.
    """

    def record(self, event: Event) -> None:
        """Log a Gateway event as a single structured line.

        Args:
            event: The event to log.
        """
        _LOGGER.info(
            "gateway event recorded event_type=%s",
            event.event_type,
            extra={"component": _COMPONENT, "event_type": event.event_type},
        )


class InMemoryGatewayLedgerWriter:
    """A :class:`GatewayLedgerWriter` that retains events in memory for tests."""

    def __init__(self) -> None:
        """Initialize with an empty, publicly readable event log."""
        self.events: list[Event] = []

    def record(self, event: Event) -> None:
        """Append a Gateway event to the in-memory log.

        Args:
            event: The event to retain.
        """
        self.events.append(event)


class SqliteGatewayLedgerWriter:
    """A :class:`GatewayLedgerWriter` persisting to a hash-chained ledger store.

    Appends each Gateway event to a
    :class:`~hedgekit.ledger.store.SqliteLedgerStore` (or any
    :class:`~hedgekit.ledger.store.LedgerStore`), so the Gateway's lifecycle and
    recovery events become durable, tamper-evident records that a restarted
    Gateway's :meth:`~hedgekit.order_gateway.gateway.OrderGateway.recover` can
    fold back (issue #40).
    """

    def __init__(self, store: LedgerStore) -> None:
        """Bind the writer to a ledger store.

        Args:
            store: The append-only ledger store every event is persisted to.
        """
        self._store = store

    def record(self, event: Event) -> None:
        """Append a Gateway event to the ledger store.

        Args:
            event: The event to persist.
        """
        self._store.append(event)


def apply_and_ledger(
    writer: GatewayLedgerWriter,
    state: OrderState,
    event: OrderEvent,
    *,
    client_order_id: str,
) -> OrderState:
    """Compute a transition, record it, and return the target only if recorded.

    Computes ``target = transition(state, event)``, records an
    :class:`OrderTransitionLedgered` for the move, and returns ``target`` *only
    after* the write returns. This structurally enforces write-before-next-
    action: the write is not wrapped in a ``try``, so if it raises, the target
    is never returned and the caller can never proceed to the next action (for
    the ``REQUEST_SUBMISSION`` write, that means the submitter is never called
    and no resting order is left on the exchange).

    Args:
        writer: The ledger writer the transition is recorded through.
        state: The current lifecycle state.
        event: The event driving the transition.
        client_order_id: The content-addressed id tying the transition to its
            intent.

    Returns:
        The target lifecycle state, once the transition has been recorded.

    Raises:
        IllegalTransitionError: If ``(state, event)`` is not a legal edge.
    """
    target = transition(state, event)
    writer.record(
        OrderTransitionLedgered(
            component=_COMPONENT,
            client_order_id=client_order_id,
            from_state=state.name,
            event=event.name,
            to_state=target.name,
        )
    )
    return target
