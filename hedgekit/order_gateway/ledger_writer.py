"""Gateway-side ledger writers and the events they persist (issue #38).

The Order Gateway records its order-lifecycle transitions and its
exchange-status refusals into the append-only ledger. This module supplies the
seam and the two event types that flow through it:

    * :class:`GatewayLedgerWriter` -- the structural protocol the Gateway
      records through, with a :class:`LoggingGatewayLedgerWriter` stand-in (for
      the credential-free CLI) and an :class:`InMemoryGatewayLedgerWriter` (for
      tests), mirroring the Risk Kernel writer triad in
      :mod:`hedgekit.riskkernel.process`.
    * :class:`OrderTransitionLedgered` / :class:`SubmissionRefused` /
      :class:`ReduceOnlyRefused` / :class:`ReduceOnlyViolation` -- frozen
      :class:`~hedgekit.ledger.events.Event` subclasses following the ledger's
      ``field(init=False)`` + ``__post_init__`` derivation pattern. The two
      reduce-only events (issue #39) record a refused oversized close and a
      post-fill net-short halt, respectively.
    * :func:`apply_and_ledger` -- computes a state transition and records it
      *before* returning the target, structurally enforcing write-before-next-
      action: if the ledger write raises, the caller never receives the target
      and can never proceed to the next action (e.g. submitting the order).

The schema version and component label are held locally here rather than
imported from the ledger's private module internals, so this module owns its
own event-schema contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from hedgekit.ledger.events import Event
from hedgekit.order_gateway.state_machine import transition

if TYPE_CHECKING:
    from hedgekit.order_gateway.state_machine import OrderEvent, OrderState

#: Component label stamped on every Gateway event this module builds.
_COMPONENT = "order_gateway"

#: Schema version stamped on every Gateway event payload. Bump when a payload's
#: shape changes so old and new records remain distinguishable.
_SCHEMA_VERSION = 1

_LOGGER = logging.getLogger("hedgekit.order_gateway")


def _derive_typed_event(event: Event, payload: dict[str, object]) -> None:
    """Populate the derived ``Event`` fields on a frozen typed subclass.

    Sets ``event_type`` to the concrete class name, ``payload_schema_version``
    to this module's schema version, and ``payload`` to the assembled dict,
    using ``object.__setattr__`` because the instances are frozen. Mirrors the
    ledger's own :func:`hedgekit.ledger.events._derive_typed_event`, kept local
    so this module does not reach into that module's private internals.

    Args:
        event: The freshly constructed typed event to populate.
        payload: The type-specific payload assembled by the subclass.
    """
    object.__setattr__(event, "event_type", type(event).__name__)
    object.__setattr__(event, "payload_schema_version", _SCHEMA_VERSION)
    object.__setattr__(event, "payload", payload)


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

    Stands in until a real ledger provides a persisting writer; it emits on the
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


@dataclass(frozen=True)
class OrderTransitionLedgered(Event):
    """Records one Order Gateway state-machine transition (issue #38).

    Attributes:
        client_order_id: The content-addressed id of the intent this transition
            belongs to (see :func:`~hedgekit.order_gateway.client_order_id`).
        from_state: The state the transition moved from (``OrderState.name``).
        event: The event that drove the transition (``OrderEvent.name``).
        to_state: The state the transition moved to (``OrderState.name``).
    """

    client_order_id: str
    from_state: str
    event: str
    to_state: str
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "client_order_id": self.client_order_id,
            "from_state": self.from_state,
            "event": self.event,
            "to_state": self.to_state,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class SubmissionRefused(Event):
    """Records a submission refused before any transition or submit (issue #38).

    Emitted when the Gateway declines an intent up front -- e.g. the exchange is
    not open -- so the token is never verified or consumed and the submitter is
    never called.

    Attributes:
        client_order_id: The content-addressed id of the refused intent (see
            :func:`~hedgekit.order_gateway.client_order_id`).
        reason: A short human-readable reason for the refusal.
    """

    client_order_id: str
    reason: str
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "client_order_id": self.client_order_id,
            "reason": self.reason,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class ReduceOnlyRefused(Event):
    """Records a close refused for exceeding its closeable headroom (issue #39).

    Emitted when the Gateway declines a ``SELL_TO_CLOSE`` whose size exceeds the
    held position net of in-flight closes -- *before* the token is verified or
    consumed, so a refusal never burns the token's single use. The five count
    fields pin the exact numbers the reduce-only verdict was computed from (see
    :class:`~hedgekit.order_gateway.reduce_only.PositionSnapshot`).

    Attributes:
        client_order_id: The content-addressed id of the refused close (see
            :func:`~hedgekit.order_gateway.client_order_id`).
        ticker: The market ticker the close targeted.
        held_centis: The net held position for ``ticker``, in contract-centis.
        inflight_closing_centis: The sum of closes already in flight for
            ``ticker``, in contract-centis.
        requested_close_centis: The refused close's size, in contract-centis.
        reason: A short machine-readable reason (always ``"reduce_only"``).
    """

    client_order_id: str
    ticker: str
    held_centis: int
    inflight_closing_centis: int
    requested_close_centis: int
    reason: str
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "client_order_id": self.client_order_id,
            "ticker": self.ticker,
            "held_centis": self.held_centis,
            "inflight_closing_centis": self.inflight_closing_centis,
            "requested_close_centis": self.requested_close_centis,
            "reason": self.reason,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class ReduceOnlyViolation(Event):
    """Records a post-fill net-short breach that halts the Gateway (issue #39).

    Emitted when a close filled more than was held, leaving the position
    net-short (``net_centis < 0``). This is the fail-closed halt trigger (SPEC
    S11.5): the Gateway records this event and then refuses all further work.

    Attributes:
        client_order_id: The content-addressed id of the offending close (see
            :func:`~hedgekit.order_gateway.client_order_id`).
        ticker: The market ticker the close targeted.
        held_centis: The net held position observed for ``ticker`` after the
            fill, in contract-centis.
        filled_centis: The quantity the venue reported filled, in
            contract-centis.
        net_centis: ``held_centis - filled_centis``, negative on a breach.
    """

    client_order_id: str
    ticker: str
    held_centis: int
    filled_centis: int
    net_centis: int
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "client_order_id": self.client_order_id,
            "ticker": self.ticker,
            "held_centis": self.held_centis,
            "filled_centis": self.filled_centis,
            "net_centis": self.net_centis,
        }
        _derive_typed_event(self, payload)


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
