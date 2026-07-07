"""The Order Gateway's adverse-selection sweeper and volatility freeze (issue #41).

The Sweeper periodically retires resting orders that have gone *stale* -- either
by outliving their time-to-live, or because the market has moved strictly beyond
a tick threshold against the price they were placed at (adverse selection). It
mirrors the :class:`~hedgekit.order_gateway.reconciler.Reconciler`'s discipline:
the durable ledger is folded exactly once per cycle, the venue's open orders and
fills are each read exactly once per cycle, and when a stale order cannot be
resolved safely the sweeper *halts on that order* -- leaving it in
``CANCEL_REQUESTED`` for the Reconciler to adjudicate -- rather than guessing
(SPEC S3.2: when in doubt, halt).

Two kinds of staleness drive a cancel:

    * **TTL expiry** -- ``clock() - created_epoch_s >= ttl_seconds`` -- retires a
      single resting order.
    * **Move breach** -- ``abs(observed - baseline) > move_ticks *
      price_tick_pips`` on the order's side-matched top of book -- *freezes the
      whole ticker*: every resting order on it is cancelled, one
      :class:`~hedgekit.ledger.events.MarketFreeze` is ledgered before the
      ticker's cancels and one :class:`~hedgekit.ledger.events.ReturnToScreener`
      after they resolve, once per frozen ticker per sweep.

Every stale order's cancel always ledgers ``REQUEST_CANCEL`` first, then resolves
against the once-read venue truth: still resting -> cancel it and ledger
``CANCEL``; vanished *with* a corroborating new fill -> resolve
``(CANCEL_REQUESTED, FILL) -> FILLED`` (never a ``CANCEL`` record); vanished with
*no* fill -> leave it in ``CANCEL_REQUESTED``, still tracked, counted
``skipped_unresolved`` (a Reconciler halt to make, not the sweeper's).

:meth:`Sweeper.run` mirrors :meth:`~hedgekit.order_gateway.reconciler.
Reconciler.run`'s bounded-loop contract -- always bounded by ``max_cycles``
and/or a ``stop_event``, never an unbounded sleep -- and its interval is whole
seconds (SPEC S6.1, no floats).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from hedgekit.ledger.events import MarketFreeze, ReturnToScreener
from hedgekit.order_gateway.ledger_writer import apply_and_ledger
from hedgekit.order_gateway.recovery import fold_ledger_states, matched_fill_centis
from hedgekit.order_gateway.state_machine import OrderEvent, OrderState

if TYPE_CHECKING:
    from collections.abc import Callable

    from hedgekit.connector.models import Fill, NormalizedMarket, OrderBookSnapshot
    from hedgekit.order_gateway.gateway import OrderGateway
    from hedgekit.order_gateway.ledger_writer import GatewayLedgerWriter
    from hedgekit.order_gateway.recovery import (
        LedgerReaderProtocol,
        ReconciliationSourceProtocol,
        TrackedOrder,
    )

#: Component label stamped on every sweeper event.
_COMPONENT = "order_gateway"

#: Default seconds between sweep cycles -- a whole-second ``int`` (SPEC S6.1).
_DEFAULT_INTERVAL_S = 60

#: The machine-readable trigger label every move-breach freeze carries.
_FREEZE_TRIGGER = "cancel_on_move"

#: The machine-readable reason every return-to-screener carries.
_SCREENER_REASON = "market_freeze"

#: The exclusive, timezone-aware lower bound for reading every venue fill: fill
#: attribution is by quantity (a tracked order's already-counted fills are netted
#: out via its ``filled_centis`` baseline), so the window is deliberately open --
#: mirroring :data:`~hedgekit.order_gateway.reconciler._EPOCH`.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _default_clock() -> int:
    """Return the current wall clock as whole epoch seconds.

    Casts :func:`time.time` to an ``int`` so the sweeper stays off the banned
    float path (SPEC S6.1).

    Returns:
        The current time, in whole epoch seconds.
    """
    return int(time.time())


@dataclass(frozen=True, slots=True)
class RestingOrderMeta:
    """The sweep-relevant metadata captured when a resting order is placed.

    Attributes:
        created_epoch_s: The epoch second the order was placed (its TTL anchor).
        baseline_price_pips: The order's own limit at placement, in pips -- the
            reference a later top-of-book move is measured against, captured once
            and never updated thereafter.
    """

    created_epoch_s: int
    baseline_price_pips: int


@dataclass(frozen=True, slots=True)
class SweepPolicy:
    """The thresholds one :class:`Sweeper` enforces.

    Attributes:
        ttl_seconds: The maximum age, in whole seconds, a resting order may reach
            before it is cancelled (SPEC ``resting_order_ttl_seconds``).
        move_ticks: The number of price ticks the side-matched top of book must
            move strictly beyond, relative to the order's baseline, to breach
            (SPEC ``cancel_on_move_ticks``).
    """

    ttl_seconds: int = 900
    move_ticks: int = 2


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    """The frozen result of one :meth:`Sweeper.sweep_once` cycle.

    Attributes:
        cancelled: How many stale orders were cancelled outright this cycle.
        filled_during_cancel: How many stale orders resolved as filled during the
            cancel attempt (a corroborated out-of-band fill) this cycle.
        skipped_unresolved: How many stale orders vanished with no corroborating
            fill and were left in ``CANCEL_REQUESTED`` for the Reconciler.
        frozen_tickers: The tickers a move breach froze this cycle, in
            first-breach order.
    """

    cancelled: int
    filled_during_cancel: int
    skipped_unresolved: int
    frozen_tickers: tuple[str, ...]


class OrderCanceller(Protocol):
    """The seam the sweeper cancels a resting venue order through."""

    def cancel_order(self, order_id: str) -> None:
        """Cancel the resting order identified by ``order_id``.

        Args:
            order_id: The venue's resting-order id to cancel.
        """
        ...


class SweepPriceSource(Protocol):
    """The seam the sweeper reads a market's tick and live book through."""

    def get_market(self, ticker: str) -> NormalizedMarket:
        """Return the normalized market for ``ticker``.

        Args:
            ticker: The market ticker to look up.

        Returns:
            The market, whose ``price_tick_pips`` sizes the move threshold.
        """
        ...

    def get_order_book(self, ticker: str) -> OrderBookSnapshot:
        """Return the live order book for ``ticker``.

        Args:
            ticker: The market ticker to look up.

        Returns:
            The book, whose side-matched top level is the move reference.
        """
        ...


@dataclass(frozen=True, slots=True)
class _CycleView:
    """The once-per-cycle venue and ledger snapshot every resolution reads.

    Attributes:
        states: Each ``client_order_id``'s latest ledgered lifecycle state.
        open_ids: The venue order ids still resting.
        fills: The venue fills read for this cycle.
    """

    states: dict[str, OrderState]
    open_ids: frozenset[str]
    fills: tuple[Fill, ...]


@dataclass(frozen=True, slots=True)
class _MoveBreach:
    """A move-breach verdict carrying the numbers a freeze event records.

    Attributes:
        baseline_price_pips: The breaching order's captured limit, in pips.
        observed_price_pips: The side-matched top of book, in pips.
        price_tick_pips: The market's price tick, in pips.
    """

    baseline_price_pips: int
    observed_price_pips: int
    price_tick_pips: int


@dataclass(slots=True)
class _SweepCounts:
    """A mutable per-cycle tally the resolution helpers increment.

    Attributes:
        cancelled: Orders cancelled outright this cycle.
        filled_during_cancel: Orders resolved as filled during the cancel.
        skipped_unresolved: Orders left unresolved for the Reconciler.
    """

    cancelled: int = 0
    filled_during_cancel: int = 0
    skipped_unresolved: int = 0


class Sweeper:
    """Sweeps a live :class:`OrderGateway`'s stale resting orders (issue #41)."""

    __slots__ = (
        "_canceller",
        "_clock",
        "_gateway",
        "_interval",
        "_ledger_reader",
        "_ledger_writer",
        "_policy",
        "_price_source",
        "_source",
    )

    def __init__(
        self,
        gateway: OrderGateway,
        *,
        canceller: OrderCanceller,
        price_source: SweepPriceSource,
        reconciliation_source: ReconciliationSourceProtocol,
        ledger_reader: LedgerReaderProtocol,
        ledger_writer: GatewayLedgerWriter,
        policy: SweepPolicy,
        clock: Callable[[], int] | None = None,
        interval: int = _DEFAULT_INTERVAL_S,
    ) -> None:
        """Wire the Sweeper to the live Gateway and its venue/durable seams.

        Args:
            gateway: The live Gateway whose tracked resting orders are swept and
                whose ``retire_tracked_order`` returns close headroom on a cancel.
            canceller: The seam a still-resting order is cancelled through.
            price_source: The seam the market tick and live book are read through.
            reconciliation_source: The seam the venue's live open orders and fills
                are read through, once per cycle.
            ledger_reader: The seam the durable ledger is folded through, once per
                cycle, to find each order's current lifecycle state.
            ledger_writer: The seam cancel transitions and freeze events are
                recorded through.
            policy: The TTL and move-tick thresholds to enforce.
            clock: A zero-argument callable returning the current epoch second,
                injected so staleness is deterministic under test. Defaults to
                :func:`_default_clock` (real wall clock).
            interval: Whole seconds between cycles in :meth:`run` (SPEC S6.1).
        """
        self._gateway = gateway
        self._canceller = canceller
        self._price_source = price_source
        self._source = reconciliation_source
        self._ledger_reader = ledger_reader
        self._ledger_writer = ledger_writer
        self._policy = policy
        self._clock = clock if clock is not None else _default_clock
        self._interval = interval

    def sweep_once(self) -> SweepOutcome:
        """Sweep every tracked order for TTL/move staleness exactly once.

        Folds the ledger and reads the venue's open orders and fills once, freezes
        every ticker with a move breach (cancelling all its resting orders), then
        cancels any remaining TTL-stale order on an unfrozen ticker. A fresh,
        unbreached order is left completely untouched.

        Returns:
            The :class:`SweepOutcome` tallying this cycle.
        """
        now = self._clock()
        view = self._read_cycle()
        tracked = self._gateway.tracked_orders()
        frozen = self._detect_frozen(tracked)
        counts = _SweepCounts()
        for ticker, breach in frozen.items():
            self._freeze_ticker(ticker, breach, tracked, view, counts, now)
        for order in tracked:
            if order.ticker in frozen:
                continue
            meta = self._gateway.resting_meta(order.order_id)
            if meta is None or not self._is_ttl_stale(meta, now):
                continue
            self._resolve_stale(order, view, counts)
        return SweepOutcome(
            cancelled=counts.cancelled,
            filled_during_cancel=counts.filled_during_cancel,
            skipped_unresolved=counts.skipped_unresolved,
            frozen_tickers=tuple(frozen),
        )

    def run(
        self,
        *,
        max_cycles: int | None = None,
        interval: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Sweep on a bounded loop until the budget or stop event ends it.

        Mirrors :meth:`~hedgekit.order_gateway.reconciler.Reconciler.run`: the
        loop is always bounded by ``max_cycles`` and/or ``stop_event`` -- never an
        unbounded sleep -- so it terminates deterministically under test and shuts
        down cleanly on a signal in production.

        Args:
            max_cycles: Maximum number of cycles to run before returning. ``None``
                runs until ``stop_event`` is set.
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
            self.sweep_once()
            stop_event.wait(wait_s)

    def _read_cycle(self) -> _CycleView:
        """Fold the ledger and read the venue's open orders and fills once.

        Returns:
            The :class:`_CycleView` every resolution in this cycle reads, so a
            cycle touching K orders stays O(ledger + venue) rather than O(K * ...).
        """
        states = fold_ledger_states(self._ledger_reader.read_all())
        open_orders = self._source.get_open_orders()
        return _CycleView(
            states=states,
            open_ids=frozenset(order.id for order in open_orders),
            fills=self._source.get_fills(_EPOCH),
        )

    def _detect_frozen(
        self, tracked: tuple[TrackedOrder, ...]
    ) -> dict[str, _MoveBreach]:
        """Map each move-breached ticker to its first breaching order's numbers.

        Args:
            tracked: The Gateway's tracked resting orders, in placement order.

        Returns:
            A ticker-to-:class:`_MoveBreach` mapping in first-breach order; the
            recorded breach is the first breaching order's (by placement).
        """
        frozen: dict[str, _MoveBreach] = {}
        for order in tracked:
            if order.ticker in frozen:
                continue
            breach = self._move_breach(order)
            if breach is not None:
                frozen[order.ticker] = breach
        return frozen

    def _move_breach(self, order: TrackedOrder) -> _MoveBreach | None:
        """Return ``order``'s move-breach verdict, or ``None`` if it is unbreached.

        The reference is the side-matched top of book (best YES bid for a ``"yes"``
        order, best YES ask for a ``"no"`` one); an empty book side yields no move
        check at all (TTL remains the backstop). The breach is strict: exactly
        ``move_ticks`` ticks of move does not breach.

        Args:
            order: The tracked order to evaluate.

        Returns:
            The :class:`_MoveBreach` when the order breaches, else ``None``.
        """
        meta = self._gateway.resting_meta(order.order_id)
        if meta is None:
            return None
        observed = self._observed_price(order)
        if observed is None:
            return None
        tick_pips = self._price_source.get_market(order.ticker).price_tick_pips
        threshold = self._policy.move_ticks * tick_pips
        if abs(observed - meta.baseline_price_pips) > threshold:
            return _MoveBreach(
                baseline_price_pips=meta.baseline_price_pips,
                observed_price_pips=observed,
                price_tick_pips=tick_pips,
            )
        return None

    def _observed_price(self, order: TrackedOrder) -> int | None:
        """Return ``order``'s side-matched top-of-book price, or ``None`` if empty.

        Args:
            order: The tracked order whose book side is read.

        Returns:
            The best YES bid for a ``"yes"`` order or best YES ask for a ``"no"``
            one, in pips, or ``None`` when that side of the book is empty.
        """
        book = self._price_source.get_order_book(order.ticker)
        levels = book.yes_bids if order.side == "yes" else book.yes_asks
        if not levels:
            return None
        return levels[0].price.value

    def _is_ttl_stale(self, meta: RestingOrderMeta, now: int) -> bool:
        """Return whether ``meta``'s order has reached its time-to-live.

        Args:
            meta: The order's captured placement metadata.
            now: The current epoch second.

        Returns:
            ``True`` iff the order's age meets or exceeds ``ttl_seconds``.
        """
        return now - meta.created_epoch_s >= self._policy.ttl_seconds

    def _freeze_ticker(
        self,
        ticker: str,
        breach: _MoveBreach,
        tracked: tuple[TrackedOrder, ...],
        view: _CycleView,
        counts: _SweepCounts,
        now: int,
    ) -> None:
        """Freeze ``ticker``: ledger the freeze, cancel every order, then return.

        Ledgers exactly one :class:`~hedgekit.ledger.events.MarketFreeze` *before*
        the ticker's cancels and exactly one
        :class:`~hedgekit.ledger.events.ReturnToScreener` after they resolve, and
        resolves every tracked order on the ticker (even a young one) as stale.

        Args:
            ticker: The frozen market ticker.
            breach: The first breaching order's move numbers, recorded on the
                freeze event.
            tracked: The Gateway's tracked resting orders.
            view: This cycle's once-read venue/ledger snapshot.
            counts: The per-cycle tally each cancel increments.
            now: The current epoch second, stamped on both freeze events.
        """
        self._ledger_writer.record(
            MarketFreeze(
                component=_COMPONENT,
                ticker=ticker,
                trigger=_FREEZE_TRIGGER,
                baseline_price_pips=breach.baseline_price_pips,
                observed_price_pips=breach.observed_price_pips,
                threshold_ticks=self._policy.move_ticks,
                price_tick_pips=breach.price_tick_pips,
                epoch=now,
            )
        )
        for order in tracked:
            if order.ticker == ticker:
                self._resolve_stale(order, view, counts)
        self._ledger_writer.record(
            ReturnToScreener(
                component=_COMPONENT,
                ticker=ticker,
                reason=_SCREENER_REASON,
                epoch=now,
            )
        )

    def _resolve_stale(
        self, order: TrackedOrder, view: _CycleView, counts: _SweepCounts
    ) -> None:
        """Cancel one stale order, resolving the cancel/fill race safely.

        Ledgers ``REQUEST_CANCEL`` first (unless the order is already
        ``CANCEL_REQUESTED`` from a prior cycle), then resolves against this
        cycle's once-read venue truth: still resting -> cancel and ledger
        ``CANCEL``; vanished with a corroborating new fill -> ledger ``FILL``;
        vanished with no fill -> leave in ``CANCEL_REQUESTED`` for the Reconciler.

        Args:
            order: The stale tracked order to resolve.
            view: This cycle's once-read venue/ledger snapshot.
            counts: The per-cycle tally to increment for this order's disposition.
        """
        coid = order.client_order_id
        state = view.states.get(coid, OrderState.ACKED)
        if state is not OrderState.CANCEL_REQUESTED:
            state = apply_and_ledger(
                self._ledger_writer,
                state,
                OrderEvent.REQUEST_CANCEL,
                client_order_id=coid,
            )
        if order.order_id in view.open_ids:
            self._canceller.cancel_order(order.order_id)
            apply_and_ledger(
                self._ledger_writer, state, OrderEvent.CANCEL, client_order_id=coid
            )
            self._gateway.retire_tracked_order(order)
            counts.cancelled += 1
            return
        new_fill = (
            matched_fill_centis(view.fills, order.ticker, order.side, order.price_pips)
            - order.filled_centis
        )
        if new_fill > 0:
            apply_and_ledger(
                self._ledger_writer, state, OrderEvent.FILL, client_order_id=coid
            )
            self._gateway.retire_tracked_order(order)
            counts.filled_during_cancel += 1
            return
        counts.skipped_unresolved += 1
