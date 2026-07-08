"""Failing-first tests for the adverse-selection sweeper (issue #41, RED).

`windbreak/order_gateway/sweeper.py` does not exist yet, so the module-level
import below fails collection with `ModuleNotFoundError: No module named
'windbreak.order_gateway.sweeper'` -- the expected Gate 1 RED state for issue
#41. `OrderGateway` also does not yet expose `.resting_meta()` /
`.attach_sweeper()` / `.sweep()`, and `windbreak.ledger.events` does not yet
define `MarketFreeze` / `ReturnToScreener` (pinned separately in
`tests/ledger/test_ledger_events.py`); once the module-level import above is
satisfied, those gaps surface as `AttributeError`/`RuntimeError` instead.

Design assumptions pinned by this file (per the issue's exact public-API
sketch -- flagged for the implementer where the sketch leaves a choice open):

    * When two tracked orders on the *same* ticker both individually breach
      the move threshold in one sweep, exactly one `MarketFreeze` /
      `ReturnToScreener` pair is ledgered for that ticker, carrying the
      *first* breaching order's own baseline/observed pips -- "first" meaning
      the order returned earliest by `gateway.tracked_orders()` (insertion,
      i.e. placement, order). If the implementation picks a different
      deterministic tie-break, only the `baseline_price_pips` assertion in
      `test_sweep_once_freezes_ticker_and_cancels_every_resting_order_on_a_three_tick_move`
      needs updating -- every other assertion in that test (both orders
      cancelled, exactly one freeze pair, `price_tick_pips`/`threshold_ticks`/
      `observed_price_pips`) holds regardless.
    * `MarketFreeze.event_type` is the literal class name `"MarketFreeze"`
      (never `"MARKET_FREEZE"`) -- the issue sketch spells the *concept* in
      shouty-snake-case, but every other concrete `Event` subtype in
      `windbreak.ledger.events` derives `event_type` from `type(self).__name__`
      via `_derive_typed_event`, and nothing in the issue asks `MarketFreeze`
      to special-case that.
    * The move-breach reference price is the side-matched top of book at
      sweep time -- best `yes_bids[0].price` for a `"yes"` resting order, best
      `yes_asks[0].price` for a `"no"` one -- compared against the order's own
      limit price (`RestingOrderMeta.baseline_price_pips`, captured once at
      placement, never updated thereafter). All three pip quantities are
      plain `int`s (SPEC S6.1, no floats).

This module pins:

    * Staleness: `sweep_once()` cancels a resting order once
      `clock() - created_epoch_s >= ttl_seconds`. `+899` leaves it untouched;
      `+900` cancels it (`SweepPolicy`'s default `ttl_seconds=900`).
    * A strict move breach (`abs(observed - baseline) > move_ticks *
      price_tick_pips`) freezes the *entire* ticker: every tracked order on
      it is cancelled, exactly one `MarketFreeze` + `ReturnToScreener` pair is
      ledgered per frozen ticker per sweep even when two resting orders on it
      independently breach, and a young (non-TTL-stale) second order on that
      ticker is swept up too. Exactly two ticks (`move_ticks *
      price_tick_pips` pips) does *not* breach (strict `>`, never `>=`).
    * The cancel path always ledgers `REQUEST_CANCEL` (`ACKED` ->
      `CANCEL_REQUESTED`) first. If the order is still resting on the venue,
      `canceller.cancel_order` plus a ledgered `CANCEL` (-> `CANCELLED`)
      follow, then `gateway.retire_tracked_order`. If the order has vanished
      from the venue's open orders *with* a corroborating new fill
      (`matched_fill_centis - filled_centis > 0`), it resolves via the
      `(CANCEL_REQUESTED, FILL) -> FILLED` edge instead -- no `CANCEL`
      record, retired exactly once (and a closing order's in-flight-closing
      headroom returns exactly once, never double-counted). Vanished with
      *no* fill leaves the order in `CANCEL_REQUESTED`, still tracked,
      increments `skipped_unresolved`, and ledgers neither `CANCEL` nor
      `FILL` (a Reconciler halt to make, not this sweeper's job).
    * The tracer invariant: a fresh, unbreached resting order leaves
      `sweep_once()` a complete no-op -- zero new ledger records, the order
      still tracked and resting, and an all-zero/empty `SweepOutcome`.
    * `run(max_cycles=N, interval=0, stop_event=...)` terminates
      deterministically (mirroring `OrderGateway.run`'s/`Reconciler.run`'s own
      bounded-loop contract), a pre-set `stop_event` ends it immediately with
      zero cycles, and the default `interval` is the whole-second `int` `60`
      (SPEC S6.1).
    * `OrderGateway.sweep()` raises `RuntimeError` with no `Sweeper` attached,
      and delegates to the attached `Sweeper.sweep_once()` otherwise.

Fixture note: `tests/fixtures/books/volatile_markets/gap_move/` is a new,
two-ticker `PaperExchange` fixture mirroring `deep_walk`'s/
`resting_full_consume`'s shape exactly (`exchange.json`/`markets.json`/
`sessions.json`/`balance_semantics.json`/`balances.json`/`fee_models.json`).
Both tickers carry `price_tick_pips=100` (so 2 ticks = 200 pips, 3 ticks =
300 pips) and a 2-step session: step 0 rests a buy order below the ask
(never crossing), step 1 gaps the top-of-book YES bid *without* any recorded
trade print, so a single `PaperExchange.advance()` call moves the reference
price without ever filling the resting order:

    * `KXFED-25SEP-CUT25` gaps its top bid 4500 -> 4800 pips (3 ticks -- a
      strict breach against the default 2-tick policy).
    * `KXCPI-25AUG-T400` gaps its top bid 4500 -> 4700 pips (exactly 2 ticks
      -- the non-breaching boundary).

The TTL-only, vanished-no-fill, and tracer-invariant cases below reuse the
shared `paper_exchange` fixture (`deep_walk`, `tests/order_gateway/conftest.py`)
at a non-crossing resting price (`4400`, below `deep_walk`'s `4600` ask)
since they need a stable book, not a moving one. The cancel/fill-race cases
reuse `test_reconciler.py`'s `resting_full_consume` fixture and its
trade-through helpers verbatim, rather than duplicating that scenario.
"""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.order_gateway.conftest import (
    DEFAULT_MARKET_TICKER,
    DEFAULT_NOW_EPOCH_S,
    KEY_MATERIAL,
    issue_matching_token,
    make_intent,
)
from tests.order_gateway.test_reconciler import (
    _FULLCONSUME_PRICE,
    _FULLCONSUME_TICKER,
    _build_gateway_over,
    _resting_full_consume_exchange,
)
from tests.order_gateway.test_reduce_only import _position, _StubPositionSource
from windbreak.ledger.store import SqliteLedgerStore
from windbreak.numeric.types import ContractCentis, PricePips
from windbreak.order_gateway.gateway import OrderGateway, PaperSubmitter, SubmitOutcome
from windbreak.order_gateway.ledger_writer import SqliteGatewayLedgerWriter
from windbreak.order_gateway.recovery import fold_ledger_states
from windbreak.order_gateway.state_machine import OrderState
from windbreak.order_gateway.sweeper import (
    RestingOrderMeta,
    Sweeper,
    SweepOutcome,
    SweepPolicy,
)
from windbreak.order_gateway.wal import WriteAheadLog

if TYPE_CHECKING:
    from collections.abc import Callable

    from windbreak.connector.paper import PaperExchange
    from windbreak.ledger.store import LedgerRecord

#: The `deep_walk` fixture's ask sits at 4600 pips; 4400 stays safely below
#: it, so a resting order placed here always fills zero and rests in full --
#: the TTL-only, vanished-no-fill, and tracer-invariant cases need a stable,
#: non-crossing resting order, not a moving book.
_TTL_PRICE = PricePips(4400)

#: The default resting-order size every helper below places, in
#: contract-centis. A module-level singleton (ruff B008): the wrapper type is
#: frozen, so sharing one instance as a default argument value is safe.
_DEFAULT_SIZE = ContractCentis(100)

#: The `volatile_markets/gap_move` fixture's move-breach ticker (see the
#: module docstring's fixture note): its top-of-book YES bid gaps
#: 4500 -> 4800 pips (3 ticks) on one `advance()`, with no trade print.
_GAP_TICKER = "KXFED-25SEP-CUT25"

#: The first (move-breaching) order's own limit on `_GAP_TICKER` -- also the
#: baseline this file's single `MarketFreeze` assertion pins (see the module
#: docstring's design-assumption note on ordering).
_GAP_ORDER1_PRICE = PricePips(4500)

#: The second, independently move-breaching order's own limit on `_GAP_TICKER`.
_GAP_ORDER2_PRICE = PricePips(4300)

#: `_GAP_TICKER`'s step-1 top-of-book YES bid: the shared observed reference
#: both resting orders above are diffed against.
_GAP_OBSERVED_PRICE_PIPS = 4800

#: Every `volatile_markets/gap_move` ticker's `price_tick_pips` (matches
#: `deep_walk`'s), so 2 ticks = 200 pips and 3 ticks = 300 pips.
_GAP_PRICE_TICK_PIPS = 100

#: The `volatile_markets/gap_move` fixture's non-breaching boundary ticker:
#: its top-of-book YES bid gaps exactly 2 ticks (200 pips), which never
#: breaches a 2-tick policy (strict `>`, never `>=`).
_BOUNDARY_TICKER = "KXCPI-25AUG-T400"

#: The boundary order's own limit on `_BOUNDARY_TICKER`.
_BOUNDARY_ORDER_PRICE = PricePips(4500)


def _gap_move_exchange() -> PaperExchange:
    """Load the two-ticker `volatile_markets/gap_move` books fixture.

    Returns:
        A fresh `PaperExchange` positioned at each ticker's step 0 -- a
        resting buy below the ask, never crossing. One `advance()` call gaps
        each ticker's top-of-book YES bid with no trade print (see the module
        docstring's fixture note for the exact per-ticker gap sizes).
    """
    from windbreak.connector.paper import PaperExchange

    books_dir = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "books"
        / "volatile_markets"
        / "gap_move"
    )
    return PaperExchange.from_fixture_dir(books_dir)


class _MutableClock:
    """A settable epoch-second clock double for the `Sweeper` under test.

    Kept independent of the Gateway's own fixed `clock=lambda:
    DEFAULT_NOW_EPOCH_S` (which only stamps token expiry /
    `RestingOrderMeta.created_epoch_s` at placement) so a test can advance
    *only* the Sweeper's notion of "now" to cross a TTL boundary without
    perturbing anything already placed.
    """

    def __init__(self, now: int) -> None:
        """Initialize with the clock's current value.

        Args:
            now: The epoch second this clock currently reports.
        """
        self.now = now

    def __call__(self) -> int:
        """Return the current configured epoch second.

        Returns:
            The clock's current value.
        """
        return self.now


def _place_resting_order(
    gateway: OrderGateway,
    *,
    market_ticker: str,
    price: PricePips,
    idempotency_key: str,
    size: ContractCentis = _DEFAULT_SIZE,
    action: str = "buy",
) -> tuple[str, str]:
    """Place a fully-resting intent through `gateway` at a non-crossing price.

    Args:
        gateway: The Gateway to submit through.
        market_ticker: The target market ticker.
        price: The limit price; callers choose one that never crosses, so
            the order rests in full (`ack.filled == 0`).
        idempotency_key: The intent's idempotency key -- the field this
            helper relies on to vary the derived client-order-id across
            calls within one test.
        size: The order size, in contract-centis.
        action: The trade action; `"buy"` (an opening BUY_TO_OPEN) by
            default.

    Returns:
        The `(client_order_id, venue_order_id)` pair for the resting order.
    """
    intent = make_intent(
        market_ticker=market_ticker,
        outcome="yes",
        action=action,
        price=price,
        size=size,
        idempotency_key=idempotency_key,
    )
    token = issue_matching_token(intent)
    result = gateway.process_intent(intent, token)
    assert result.outcome is SubmitOutcome.ACKED
    assert result.ack is not None
    assert result.ack.order_id is not None
    assert result.client_order_id is not None
    return result.client_order_id, result.ack.order_id


def _build_sweeper(
    gateway: OrderGateway,
    exchange: PaperExchange,
    store: SqliteLedgerStore,
    *,
    clock: Callable[[], int],
    policy: SweepPolicy | None = None,
) -> Sweeper:
    """Build a `Sweeper` wired over `gateway`/`exchange`/`store`.

    Args:
        gateway: The live Gateway whose tracked orders the Sweeper sweeps.
        exchange: The paper exchange serving as `canceller`, `price_source`,
            and `reconciliation_source` alike -- it structurally satisfies
            all three seams.
        store: The SQLite-backed ledger, read through as `ledger_reader` and
            written through a fresh `SqliteGatewayLedgerWriter`.
        clock: The Sweeper's own now-clock (independent of the Gateway's).
        policy: The sweep policy; defaults to `SweepPolicy()` (900s TTL, 2
            ticks).

    Returns:
        A fully wired `Sweeper`, not yet run.
    """
    return Sweeper(
        gateway,
        canceller=exchange,
        price_source=exchange,
        reconciliation_source=exchange,
        ledger_reader=store,
        ledger_writer=SqliteGatewayLedgerWriter(store),
        policy=policy if policy is not None else SweepPolicy(),
        clock=clock,
    )


def _transition_events_for(records: list[LedgerRecord], coid: str) -> list[str]:
    """Return `coid`'s `OrderTransitionLedgered` `event` values, in order.

    Args:
        records: Every ledger record to scan.
        coid: The client-order-id to filter to.

    Returns:
        The `event` field of each matching row, in ledger (append) order.
    """
    events: list[str] = []
    for record in records:
        if record.event_type != "OrderTransitionLedgered":
            continue
        data = json.loads(record.payload_json)["data"]
        if data["client_order_id"] == coid:
            events.append(str(data["event"]))
    return events


def _events_of_type(
    records: list[LedgerRecord], event_type: str
) -> list[dict[str, object]]:
    """Return every ledgered event's `data` payload matching `event_type`.

    Args:
        records: Every ledger record to scan.
        event_type: The exact `Event.event_type` class-name string to match
            (e.g. `"MarketFreeze"`, never a shouty-snake-case variant).

    Returns:
        The `data` payload of each matching row, in ledger order.
    """
    return [
        json.loads(record.payload_json)["data"]
        for record in records
        if record.event_type == event_type
    ]


# --- Staleness: TTL expiry cancels, with an exact boundary --------------------


def test_sweep_once_cancels_a_ttl_stale_resting_order(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """A resting order older than `ttl_seconds` is cancelled outright.

    `clock() - created_epoch_s == 900 >= ttl_seconds` cancels: `sweep_once()`
    ledgers `REQUEST_CANCEL` then `CANCEL`, in that order, the venue order is
    gone, the Gateway stops tracking it, and its `resting_meta` is popped.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    coid, order_id = _place_resting_order(
        gateway,
        market_ticker=DEFAULT_MARKET_TICKER,
        price=_TTL_PRICE,
        idempotency_key="idem-ttl-cancel",
    )
    assert gateway.resting_meta(order_id) == RestingOrderMeta(
        created_epoch_s=DEFAULT_NOW_EPOCH_S, baseline_price_pips=_TTL_PRICE.value
    )
    sweeper = _build_sweeper(
        gateway,
        paper_exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 900),
    )

    outcome = sweeper.sweep_once()

    assert outcome == SweepOutcome(
        cancelled=1, filled_during_cancel=0, skipped_unresolved=0, frozen_tickers=()
    )
    assert fold_ledger_states(store.read_all())[coid] is OrderState.CANCELLED
    assert _transition_events_for(store.read_all(), coid)[-2:] == [
        "REQUEST_CANCEL",
        "CANCEL",
    ]
    assert paper_exchange.get_open_orders() == ()
    assert gateway.tracked_orders() == ()
    assert gateway.resting_meta(order_id) is None
    store.close()


def test_sweep_once_leaves_a_resting_order_untouched_one_second_before_ttl(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """A resting order aged `ttl_seconds - 1` is left completely alone.

    `899 < 900 == ttl_seconds`: `sweep_once()` writes no new ledger record,
    the order stays tracked and resting, and the returned outcome is
    all-zero.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    coid, order_id = _place_resting_order(
        gateway,
        market_ticker=DEFAULT_MARKET_TICKER,
        price=_TTL_PRICE,
        idempotency_key="idem-ttl-untouched",
    )
    before = len(store.read_all())
    sweeper = _build_sweeper(
        gateway,
        paper_exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 899),
    )

    outcome = sweeper.sweep_once()

    assert outcome == SweepOutcome(
        cancelled=0, filled_during_cancel=0, skipped_unresolved=0, frozen_tickers=()
    )
    assert len(store.read_all()) == before
    assert fold_ledger_states(store.read_all())[coid] is OrderState.ACKED
    assert paper_exchange.get_open_orders() != ()
    assert gateway.tracked_orders() != ()
    assert gateway.resting_meta(order_id) is not None
    store.close()


# --- Move breach: a strict beyond-N-ticks gap freezes the whole ticker -------


def test_sweep_once_freezes_ticker_and_cancels_every_resting_order_on_a_three_tick_move(
    tmp_path: Path,
) -> None:
    """A strict 3-tick move freezes the whole ticker, cancelling every order.

    Two independently-breaching orders on `KXFED-25SEP-CUT25` are both
    cancelled; exactly one `MarketFreeze` + `ReturnToScreener` pair is
    ledgered for the ticker (never one per breaching order); neither order is
    anywhere near its own TTL, proving the freeze -- not staleness -- is what
    swept both up (see the module docstring's design-assumption note on
    which order's baseline the single `MarketFreeze` carries).
    """
    exchange = _gap_move_exchange()
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(exchange, store, wal)
    assert gateway.recover().halted is False

    coid1, order_id1 = _place_resting_order(
        gateway,
        market_ticker=_GAP_TICKER,
        price=_GAP_ORDER1_PRICE,
        idempotency_key="idem-gap-order-1",
    )
    coid2, order_id2 = _place_resting_order(
        gateway,
        market_ticker=_GAP_TICKER,
        price=_GAP_ORDER2_PRICE,
        idempotency_key="idem-gap-order-2",
    )
    exchange.advance()

    sweeper = _build_sweeper(
        gateway,
        exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 5),
    )

    outcome = sweeper.sweep_once()

    assert outcome == SweepOutcome(
        cancelled=2,
        filled_during_cancel=0,
        skipped_unresolved=0,
        frozen_tickers=(_GAP_TICKER,),
    )
    states = fold_ledger_states(store.read_all())
    assert states[coid1] is OrderState.CANCELLED
    assert states[coid2] is OrderState.CANCELLED
    assert exchange.get_open_orders() == ()
    assert gateway.tracked_orders() == ()
    assert gateway.resting_meta(order_id1) is None
    assert gateway.resting_meta(order_id2) is None

    # `event_type` round-trips as the literal class name "MarketFreeze" --
    # never the issue sketch's shouty-snake-case "MARKET_FREEZE" (see the
    # module docstring's design-assumption note).
    freezes = _events_of_type(store.read_all(), "MarketFreeze")
    assert len(freezes) == 1
    freeze = freezes[0]
    assert freeze["ticker"] == _GAP_TICKER
    assert freeze["trigger"] == "cancel_on_move"
    assert freeze["baseline_price_pips"] == _GAP_ORDER1_PRICE.value
    assert freeze["observed_price_pips"] == _GAP_OBSERVED_PRICE_PIPS
    assert freeze["threshold_ticks"] == 2
    assert freeze["price_tick_pips"] == _GAP_PRICE_TICK_PIPS
    assert isinstance(freeze["epoch"], int)

    screeners = _events_of_type(store.read_all(), "ReturnToScreener")
    assert len(screeners) == 1
    assert screeners[0]["ticker"] == _GAP_TICKER
    assert screeners[0]["reason"] == "market_freeze"
    store.close()


def test_sweep_once_does_not_freeze_on_an_exactly_two_tick_move(
    tmp_path: Path,
) -> None:
    """An exactly-2-tick move (the policy's own threshold) never breaches.

    `abs(4700 - 4500) == 200 == move_ticks * price_tick_pips` is not *beyond*
    the threshold (strict `>`, never `>=`): the order is left resting,
    tracked, and un-ledgered.
    """
    exchange = _gap_move_exchange()
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(exchange, store, wal)
    assert gateway.recover().halted is False

    coid, order_id = _place_resting_order(
        gateway,
        market_ticker=_BOUNDARY_TICKER,
        price=_BOUNDARY_ORDER_PRICE,
        idempotency_key="idem-gap-boundary",
    )
    exchange.advance()
    before = len(store.read_all())
    sweeper = _build_sweeper(
        gateway,
        exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 5),
    )

    outcome = sweeper.sweep_once()

    assert outcome == SweepOutcome(
        cancelled=0, filled_during_cancel=0, skipped_unresolved=0, frozen_tickers=()
    )
    assert len(store.read_all()) == before
    assert fold_ledger_states(store.read_all())[coid] is OrderState.ACKED
    assert exchange.get_open_orders() != ()
    assert gateway.tracked_orders() != ()
    assert gateway.resting_meta(order_id) is not None
    store.close()


# --- Cancel/fill race: an out-of-band consumption resolves FILLED, never ----
# --- double-cancelled or double-counted -------------------------------------


def test_sweep_once_resolves_a_ttl_stale_order_consumed_by_advance_as_filled(
    tmp_path: Path,
) -> None:
    """A TTL-stale order fully consumed out-of-band resolves FILLED.

    `exchange.advance()` runs *before* the sweep and fully consumes the
    resting order via `resting_full_consume`'s recorded trade-through print
    (see `test_reconciler.py`). `sweep_once()` still ledgers `REQUEST_CANCEL`
    first -- the cancel attempt always starts there -- then finds the order
    already gone from the venue *with* a corroborating fill and resolves it
    via the `(CANCEL_REQUESTED, FILL) -> FILLED` edge: never a `CANCEL`
    record, retired exactly once.
    """
    exchange = _resting_full_consume_exchange()
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(exchange, store, wal)
    assert gateway.recover().halted is False

    coid, order_id = _place_resting_order(
        gateway,
        market_ticker=_FULLCONSUME_TICKER,
        price=_FULLCONSUME_PRICE,
        idempotency_key="idem-fillrace-open",
    )
    assert exchange.get_open_orders() != ()
    exchange.advance()
    assert exchange.get_open_orders() == ()

    sweeper = _build_sweeper(
        gateway,
        exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 900),
    )

    outcome = sweeper.sweep_once()

    assert outcome == SweepOutcome(
        cancelled=0, filled_during_cancel=1, skipped_unresolved=0, frozen_tickers=()
    )
    assert fold_ledger_states(store.read_all())[coid] is OrderState.FILLED
    events = _transition_events_for(store.read_all(), coid)
    assert events[-2:] == ["REQUEST_CANCEL", "FILL"]
    assert "CANCEL" not in events
    assert gateway.tracked_orders() == ()
    assert gateway.resting_meta(order_id) is None
    store.close()


def test_sweep_once_retires_inflight_closing_headroom_exactly_once_on_race(
    tmp_path: Path,
) -> None:
    """A settled SELL_TO_CLOSE race-resolution retires headroom exactly once.

    Mirrors `test_reconciler.py`'s own closing-heal test: a subsequent,
    equal-size close against the *same* held position ACKs only if the
    in-flight-closing tally was retired -- not left standing, and not
    retired twice.
    """
    exchange = _resting_full_consume_exchange()
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    position_source = _StubPositionSource((_position(100, ticker=_FULLCONSUME_TICKER),))
    gateway = _build_gateway_over(exchange, store, wal, position_source=position_source)
    assert gateway.recover().halted is False

    coid, _order_id = _place_resting_order(
        gateway,
        market_ticker=_FULLCONSUME_TICKER,
        price=_FULLCONSUME_PRICE,
        idempotency_key="idem-fillrace-close",
        action="sell_to_close",
    )
    exchange.advance()
    assert exchange.get_open_orders() == ()

    sweeper = _build_sweeper(
        gateway,
        exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 900),
    )

    outcome = sweeper.sweep_once()

    assert outcome.filled_during_cancel == 1
    assert fold_ledger_states(store.read_all())[coid] is OrderState.FILLED
    assert gateway.tracked_orders() == ()

    second_intent = make_intent(
        market_ticker=_FULLCONSUME_TICKER,
        outcome="yes",
        action="sell_to_close",
        price=_FULLCONSUME_PRICE,
        size=ContractCentis(100),
        idempotency_key="idem-fillrace-close-second",
    )
    second_token = issue_matching_token(second_intent)
    second_result = gateway.process_intent(second_intent, second_token)

    assert second_result.outcome is SubmitOutcome.ACKED
    store.close()


def test_sweep_once_leaves_a_vanished_order_with_no_fill_unresolved_and_skipped(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """A TTL-stale order vanished out-of-band with *no* corroborating fill.

    An out-of-band cancellation (no Gateway involvement, no `Fill` emitted)
    leaves the sweep unable to safely resolve the race: it ledgers
    `REQUEST_CANCEL` (the cancel attempt always starts there), finds the
    order gone from the venue with *zero* attributable new fill quantity, and
    stops -- never ledgering `CANCEL` or `FILL`, leaving the order tracked in
    `CANCEL_REQUESTED`, and counting it `skipped_unresolved` (a Reconciler
    halt to make, not this sweeper's job).
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    coid, order_id = _place_resting_order(
        gateway,
        market_ticker=DEFAULT_MARKET_TICKER,
        price=_TTL_PRICE,
        idempotency_key="idem-vanish-no-fill-sweep",
    )
    paper_exchange.cancel_order(order_id)
    assert paper_exchange.get_open_orders() == ()

    sweeper = _build_sweeper(
        gateway,
        paper_exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 900),
    )

    outcome = sweeper.sweep_once()

    assert outcome == SweepOutcome(
        cancelled=0, filled_during_cancel=0, skipped_unresolved=1, frozen_tickers=()
    )
    assert fold_ledger_states(store.read_all())[coid] is OrderState.CANCEL_REQUESTED
    events = _transition_events_for(store.read_all(), coid)
    assert events[-1] == "REQUEST_CANCEL"
    assert "CANCEL" not in events
    assert "FILL" not in events
    assert gateway.tracked_orders() != ()
    assert gateway.resting_meta(order_id) is not None
    store.close()


def test_sweep_over_an_already_cancel_requested_order_is_idempotent(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """A second sweep over a stuck `CANCEL_REQUESTED` order stays legal.

    The first sweep of a vanished-with-no-fill order leaves it in
    `CANCEL_REQUESTED` (a Reconciler halt to make). A *second* sweep must not
    re-issue `REQUEST_CANCEL` -- there is no legal `(CANCEL_REQUESTED,
    REQUEST_CANCEL)` edge, so doing so would raise `IllegalTransitionError`.
    Instead the sweep skips straight to resolution, re-reports the order
    `skipped_unresolved`, writes no new ledger record, and leaves exactly one
    `REQUEST_CANCEL` in its history.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    coid, order_id = _place_resting_order(
        gateway,
        market_ticker=DEFAULT_MARKET_TICKER,
        price=_TTL_PRICE,
        idempotency_key="idem-reentrant-cancel-requested",
    )
    paper_exchange.cancel_order(order_id)
    sweeper = _build_sweeper(
        gateway,
        paper_exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 900),
    )

    first = sweeper.sweep_once()
    assert first == SweepOutcome(
        cancelled=0, filled_during_cancel=0, skipped_unresolved=1, frozen_tickers=()
    )
    after_first = len(store.read_all())

    second = sweeper.sweep_once()

    assert second == SweepOutcome(
        cancelled=0, filled_during_cancel=0, skipped_unresolved=1, frozen_tickers=()
    )
    assert len(store.read_all()) == after_first
    assert fold_ledger_states(store.read_all())[coid] is OrderState.CANCEL_REQUESTED
    events = _transition_events_for(store.read_all(), coid)
    assert events.count("REQUEST_CANCEL") == 1
    assert "CANCEL" not in events
    assert "FILL" not in events
    store.close()


# --- Tracer invariant: a fresh, unbreached order is a complete no-op --------


def test_sweep_once_on_a_fresh_unbreached_order_writes_zero_ledger_records(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """A fresh, unbreached resting order leaves `sweep_once()` a total no-op.

    Neither TTL-stale nor beyond the move threshold: `sweep_once()` writes no
    new ledger record at all, the order stays tracked and resting unchanged,
    and the returned `SweepOutcome` is all-zero/empty.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    coid, order_id = _place_resting_order(
        gateway,
        market_ticker=DEFAULT_MARKET_TICKER,
        price=_TTL_PRICE,
        idempotency_key="idem-tracer",
    )
    expected_meta = gateway.resting_meta(order_id)
    before = len(store.read_all())
    sweeper = _build_sweeper(
        gateway,
        paper_exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S),
    )

    outcome = sweeper.sweep_once()

    assert outcome == SweepOutcome(
        cancelled=0, filled_during_cancel=0, skipped_unresolved=0, frozen_tickers=()
    )
    assert len(store.read_all()) == before
    assert fold_ledger_states(store.read_all())[coid] is OrderState.ACKED
    assert paper_exchange.get_open_orders() != ()
    assert gateway.tracked_orders() != ()
    assert gateway.resting_meta(order_id) == expected_meta
    store.close()


# --- Loop discipline: bounded, deterministic, whole-second default ----------


def test_run_terminates_deterministically_after_max_cycles(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """`run(max_cycles=3, interval=0, stop_event=...)` returns deterministically.

    Mirrors `Reconciler.run`'s (and `OrderGateway.run`'s) bounded-loop
    contract: the call must return -- never hang -- once the cycle budget is
    exhausted.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False
    sweeper = _build_sweeper(
        gateway,
        paper_exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S),
    )

    sweeper.run(max_cycles=3, interval=0, stop_event=threading.Event())

    # Reaching this line (no hang, no exception) is the pass condition.
    store.close()


def test_run_ends_immediately_when_stop_event_is_already_set(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """A pre-set `stop_event` ends `run()` before any cycle executes."""
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False
    sweeper = _build_sweeper(
        gateway,
        paper_exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S),
    )
    stop_event = threading.Event()
    stop_event.set()
    before = len(store.read_all())

    sweeper.run(max_cycles=None, interval=0, stop_event=stop_event)

    assert len(store.read_all()) == before
    store.close()


def test_sweeper_default_interval_is_the_whole_second_int_sixty() -> None:
    """The default `interval` is the int `60` -- never a float (SPEC S6.1)."""
    default = inspect.signature(Sweeper.__init__).parameters["interval"].default

    assert default == 60
    assert isinstance(default, int)
    assert not isinstance(default, bool)


# --- Gateway surface: attach-gated delegation --------------------------------


def test_gateway_sweep_without_an_attached_sweeper_raises_runtime_error(
    paper_exchange: PaperExchange,
) -> None:
    """`gateway.sweep()` raises `RuntimeError` until a `Sweeper` is attached."""
    gateway = OrderGateway(
        PaperSubmitter(paper_exchange),
        verification_key=KEY_MATERIAL,
        clock=lambda: DEFAULT_NOW_EPOCH_S,
    )

    with pytest.raises(RuntimeError):
        gateway.sweep()


def test_gateway_sweep_delegates_to_the_attached_sweeper(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """`gateway.sweep()` delegates to the attached `Sweeper.sweep_once()`.

    Attaching a `Sweeper` wired for a TTL-stale resting order and calling
    `gateway.sweep()` -- never `sweeper.sweep_once()` directly -- produces
    the exact cancellation effect the TTL-cancel test pins directly on the
    `Sweeper`, proving genuine delegation rather than a stub that ignores
    the call.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    coid, order_id = _place_resting_order(
        gateway,
        market_ticker=DEFAULT_MARKET_TICKER,
        price=_TTL_PRICE,
        idempotency_key="idem-sweep-delegate",
    )
    sweeper = _build_sweeper(
        gateway,
        paper_exchange,
        store,
        clock=_MutableClock(DEFAULT_NOW_EPOCH_S + 900),
    )
    gateway.attach_sweeper(sweeper)

    outcome = gateway.sweep()

    assert outcome == SweepOutcome(
        cancelled=1, filled_during_cancel=0, skipped_unresolved=0, frozen_tickers=()
    )
    assert fold_ledger_states(store.read_all())[coid] is OrderState.CANCELLED
    assert paper_exchange.get_open_orders() == ()
    assert gateway.tracked_orders() == ()
    assert gateway.resting_meta(order_id) is None
    store.close()
