"""Pure crash-recovery core for the Order Gateway (issue #40, SPEC S11.4).

This module holds the *pure*, side-effect-free vocabulary and diff helpers the
Gateway's :meth:`~windbreak.order_gateway.gateway.OrderGateway.recover` and the
:class:`~windbreak.order_gateway.reconciler.Reconciler` fold the durable ledger,
the write-ahead log, and the venue's live truth against each other. Keeping the
classification as small, table-like predicates (never sprawling branch trees,
mirroring :mod:`~windbreak.order_gateway.state_machine`'s data-not-branches style)
keeps every function trivially auditable and well under the complexity ceiling.

    * :class:`RecoveryReport` -- the frozen result of one ``recover()``.
    * :class:`TrackedOrder` -- the Gateway's per-resting-order economic profile,
      the unit both recovery (rehydrated from the WAL) and the Reconciler
      (recorded live on placement) diff against the venue.
    * :func:`fold_ledger_states` / :func:`ledger_shows_halt` -- fold the durable
      ledger into per-``client_order_id`` state and the fail-safe halt latch (a
      :class:`~windbreak.ledger.events.ReduceOnlyViolation` or
      :class:`~windbreak.ledger.events.ReconciliationHalted` is durable and has no
      un-halt event).
    * :func:`pending_intents` / :func:`build_unaccounted_halt` -- classify a
      resting venue order with no durable ack as a ``foreign_open_order`` (no
      trace at all) or an ``ambiguous_match`` (a mid-submission crash whose
      completing WAL-ack was never written), never guessing.
    * :func:`matched_fill_centis` -- the venue-fill quantity attributable to a
      tracked order's ticker/side/limit, used to tell a benign missed fill from
      an unexplained vanish.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from windbreak.ledger.events import ReconciliationHalted
from windbreak.order_gateway.state_machine import OrderState

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from windbreak.connector.models import Fill, OpenOrder
    from windbreak.ledger.store import LedgerRecord
    from windbreak.order_gateway.wal import WalRecord
    from windbreak.riskkernel.checks import OrderIntent

#: Component label stamped on every recovery event this module builds.
_COMPONENT = "order_gateway"

#: The trade actions that *close* an existing position and are therefore subject
#: to reduce-only in-flight accounting across a restart (issue #39/#40). The
#: single source of truth the Gateway and Reconciler both import.
_CLOSING_ACTIONS: frozenset[str] = frozenset({"sell_to_close"})

#: Ledger event types whose mere presence latches a durable, fail-safe halt.
_HALT_EVENT_TYPES: frozenset[str] = frozenset(
    {"ReduceOnlyViolation", "ReconciliationHalted"}
)

_ORDER_TRANSITION = "OrderTransitionLedgered"

#: Human-readable diagnostics for each unaccounted-order halt reason.
_HALT_DETAIL: dict[str, str] = {
    "foreign_open_order": (
        "resting order on the venue has no durable ledger or write-ahead trace"
    ),
    "ambiguous_match": (
        "resting order matches an in-flight intent whose completing write-ahead "
        "ack was never durably written; cannot correlate safely"
    ),
}


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """The frozen outcome of one :meth:`OrderGateway.recover` call.

    Attributes:
        orders_reconciled: How many tracked orders recovery rehydrated/adopted.
        halted: Whether recovery finished with the Gateway fail-closed.
    """

    orders_reconciled: int
    halted: bool


@dataclass(frozen=True, slots=True)
class TrackedOrder:
    """The economic profile of one Gateway-placed resting order.

    Recorded live on placement and rehydrated from the write-ahead log on
    recovery, this is the unit reconciliation diffs against the venue's
    :class:`~windbreak.connector.models.OpenOrder`/``Fill`` truth.

    Attributes:
        client_order_id: The content-addressed id the order belongs to.
        order_id: The venue's resting-order id.
        ticker: The market ticker the order rests in.
        side: The order's book side (``"yes"``/``"no"``).
        price_pips: The order's limit price, in pips (an int).
        size_centis: The order's original size, in contract-centis (an int).
        action: The intent's trade action (e.g. ``"buy"``/``"sell_to_close"``).
        filled_centis: The quantity already attributed to this order at
            placement, in contract-centis -- the baseline a later fill total is
            measured against, so an already-counted taker fill is never
            re-healed.
    """

    client_order_id: str
    order_id: str
    ticker: str
    side: str
    price_pips: int
    size_centis: int
    action: str
    filled_centis: int


class LedgerReaderProtocol(Protocol):
    """The seam recovery folds the durable ledger back through."""

    def read_all(self) -> list[LedgerRecord]:
        """Return every persisted ledger record in ascending sequence order.

        Returns:
            The persisted records.
        """
        ...


class ReconciliationSourceProtocol(Protocol):
    """The seam recovery/reconciliation reads the venue's live truth through."""

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Return the venue's currently resting orders.

        Returns:
            The resting orders.
        """
        ...

    def get_fills(self, since: datetime, /) -> tuple[Fill, ...]:
        """Return the venue's fills executed strictly after ``since``.

        Args:
            since: The exclusive lower bound on fill time.

        Returns:
            The matching fills.
        """
        ...


def is_closing_action(action: str) -> bool:
    """Return whether ``action`` closes a position (reduce-only subject).

    Args:
        action: The intent's trade action.

    Returns:
        ``True`` iff ``action`` is a closing action.
    """
    return action in _CLOSING_ACTIONS


def fold_ledger_states(records: Iterable[LedgerRecord]) -> dict[str, OrderState]:
    """Fold the ledger into each ``client_order_id``'s latest lifecycle state.

    Replays only :class:`~windbreak.ledger.events.OrderTransitionLedgered` rows,
    recording each coid's most recent ``to_state``.

    Args:
        records: The ledger records to fold.

    Returns:
        A mapping from ``client_order_id`` to its latest
        :class:`~windbreak.order_gateway.state_machine.OrderState`.
    """
    states: dict[str, OrderState] = {}
    for record in records:
        if record.event_type != _ORDER_TRANSITION:
            continue
        data = cast("dict[str, object]", json.loads(record.payload_json)["data"])
        states[str(data["client_order_id"])] = OrderState[str(data["to_state"])]
    return states


def ledger_shows_halt(records: Iterable[LedgerRecord]) -> bool:
    """Return whether the ledger already records a durable, fail-safe halt.

    Args:
        records: The ledger records to scan.

    Returns:
        ``True`` iff any record is a ``ReduceOnlyViolation`` or
        ``ReconciliationHalted`` -- either latches the Gateway halted forever
        (there is no un-halt event).
    """
    return any(record.event_type in _HALT_EVENT_TYPES for record in records)


def pending_intents(wal_records: Iterable[WalRecord]) -> list[OrderIntent]:
    """Return WAL intents whose completing ack was never durably written.

    These are the crash-window intents: journalled before the Gateway acted,
    but with no ack record, so it is unknown whether they reached the venue.

    Args:
        wal_records: The write-ahead log records to scan.

    Returns:
        The journalled :class:`~windbreak.riskkernel.checks.OrderIntent`s that
        have no matching ack record.
    """
    records = list(wal_records)
    acked = {r.client_order_id for r in records if r.kind == "ack"}
    result: list[OrderIntent] = []
    for record in records:
        if (
            record.kind == "intent"
            and record.intent is not None
            and record.client_order_id not in acked
        ):
            result.append(record.intent)
    return result


def _intent_matches_order(intents: Iterable[OrderIntent], order: OpenOrder) -> bool:
    """Return whether any pending intent economically matches ``order``.

    Matches on the observable economic triple only (ticker, side, limit price):
    a resting order's quantity may have shrunk to a partial fill, so quantity is
    deliberately not compared.

    Args:
        intents: The pending (unacked) intents.
        order: The unaccounted resting venue order.

    Returns:
        ``True`` iff some intent shares ``order``'s ticker, side, and price.
    """
    return any(
        intent.market_ticker == order.ticker
        and intent.outcome == order.side
        and intent.price.value == order.price.value
        for intent in intents
    )


def build_unaccounted_halt(
    open_orders: Iterable[OpenOrder],
    tracked_ids: frozenset[str],
    intents: list[OrderIntent],
) -> ReconciliationHalted | None:
    """Return a halt event for the first resting order with no durable ack.

    A resting venue order whose id is not tracked is either a mid-submission
    crash (``ambiguous_match`` -- a pending intent economically matches it) or a
    genuine foreign order (``foreign_open_order`` -- nothing explains it). Either
    way recovery must halt rather than guess (SPEC S3.2/S11.4).

    Args:
        open_orders: The venue's resting orders.
        tracked_ids: The venue order ids recovery could account for.
        intents: The pending (unacked) WAL intents.

    Returns:
        The :class:`~windbreak.ledger.events.ReconciliationHalted` for the first
        unaccounted order, or ``None`` when every resting order is accounted for.
    """
    for order in open_orders:
        if order.id in tracked_ids:
            continue
        reason = (
            "ambiguous_match"
            if _intent_matches_order(intents, order)
            else "foreign_open_order"
        )
        return ReconciliationHalted(
            component=_COMPONENT,
            reason=reason,
            ticker=order.ticker,
            venue_order_id=order.id,
            client_order_id="",
            detail=_HALT_DETAIL[reason],
        )
    return None


def matched_fill_centis(
    fills: Iterable[Fill], ticker: str, side: str, price_pips: int
) -> int:
    """Sum the fill quantity attributable to a tracked order's ticker/side/limit.

    A resting fill is always emitted at the order's own limit price, so matching
    on ticker, side, and price attributes exactly the fills that touched this
    order (its taker and resting fills alike).

    Same-price collision caveat: two distinct Gateway orders resting at the same
    ticker/side/limit share one attribution pool here and cannot be told apart by
    this feed model. Content-addressed client-order-ids keep field-identical
    intents idempotent (never two live orders), and the caller nets each order's
    already-counted ``filled_centis`` baseline out of this total, deferring to the
    "when in doubt, halt" path whenever the remaining attributable quantity is
    non-positive.

    Args:
        fills: The venue fills to total.
        ticker: The tracked order's market ticker.
        side: The tracked order's book side.
        price_pips: The tracked order's limit price, in pips.

    Returns:
        The total matching fill quantity, in contract-centis.
    """
    return sum(
        fill.quantity.value
        for fill in fills
        if fill.ticker == ticker
        and fill.side == side
        and fill.price.value == price_pips
    )
