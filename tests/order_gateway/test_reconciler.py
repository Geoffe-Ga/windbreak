"""Failing-first tests for the continuous reconciler (issue #40, RED).

`windbreak/order_gateway/reconciler.py` does not exist yet, so the module-level
import below fails collection with `ModuleNotFoundError: No module named
'windbreak.order_gateway.reconciler'` -- the expected Gate 1 RED state for
issue #40.

Design assumption (flagged for the implementer, since the issue text leaves
`Reconciler`'s exact constructor open beyond `..., reconciliation_source,
ledger_writer, interval: int = 60`): these tests construct it as
`Reconciler(gateway, ledger_reader=..., reconciliation_source=...,
ledger_writer=..., interval=...)` -- the Gateway instance is threaded through
so the Reconciler can flip its `.halted` latch and gate `accepting_approvals`
exactly the way `OrderGateway.recover()` does, and `ledger_reader` supplies
the currently-tracked open orders/coids the Reconciler diffs against
`reconciliation_source`. If the real signature differs, only this file's call
sites need updating -- the behavioral contract below (ledgered event types,
`gateway.halted`, `gateway.accepting_approvals`) should still hold.

This module pins:

    * A benign heal: a Gateway-placed resting order that fills *out-of-band*
      (the exchange's own `advance()`, never through the Gateway) is healed,
      not treated as an anomaly: `run_once()` ledgers exactly one
      `ReconciliationHealed` event and the Gateway stays un-halted. A closing
      variant additionally retires that coid's in-flight-closing tally
      (issue #39), returning its full headroom.
    * An unexplained foreign open order (no Gateway trace at all) halts:
      `run_once()` ledgers `ReconciliationHalted`, and the Gateway itself
      becomes `.halted` and stops `accepting_approvals`.
    * A Gateway-tracked order that vanishes from the venue with *no*
      corresponding fill (e.g. an out-of-band cancellation) is a distinct
      anomaly the Reconciler refuses to silently heal: it halts with reason
      `"vanished_order_no_fill"`, never guessing it was a benign fill.
    * `run(max_cycles=N, interval=0, stop_event=...)` terminates
      deterministically -- never an unbounded loop -- mirroring
      `OrderGateway.run`'s own bounded-loop contract, and the default
      `interval` is the whole-second `int` `60` (SPEC S6.1, no floats).
"""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from tests.order_gateway.conftest import (
    DEFAULT_MARKET_TICKER,
    DEFAULT_NOW_EPOCH_S,
    KEY_MATERIAL,
    issue_matching_token,
    make_intent,
)
from tests.order_gateway.test_reduce_only import _position, _StubPositionSource
from windbreak.connector.paper import PaperOrderIntent
from windbreak.ledger.store import SqliteLedgerStore
from windbreak.numeric.types import ContractCentis, PricePips
from windbreak.order_gateway.gateway import OrderGateway, PaperSubmitter, SubmitOutcome
from windbreak.order_gateway.ledger_writer import SqliteGatewayLedgerWriter
from windbreak.order_gateway.reconciler import Reconciler
from windbreak.order_gateway.wal import WriteAheadLog

if TYPE_CHECKING:
    from windbreak.connector.paper import PaperExchange
    from windbreak.order_gateway.gateway import GatewayPositionSource

#: The `resting_full_consume` fixture's sole ticker: a single yes-bid resting
#: order that fully fills out-of-band on `advance()` via a trade-through
#: print recorded strictly below its limit (see
#: `tests/connector/test_paper_exchange.py`'s own use of this fixture).
_FULLCONSUME_TICKER = "MKT-FULLCONSUME"

#: The resting order's limit price in the `resting_full_consume` fixture: below
#: the fixture's 4400-pip ask, so it never crosses and rests in full.
_FULLCONSUME_PRICE = PricePips(4200)


def _resting_full_consume_exchange() -> PaperExchange:
    """Load the `resting_full_consume` books fixture.

    Returns:
        A fresh `PaperExchange` whose sole ticker (`MKT-FULLCONSUME`) rests a
        yes-bid at 4200 pips (below the 4400-pip ask) that fully fills
        out-of-band on `advance()` via a recorded trade-through print.
    """
    from windbreak.connector.paper import PaperExchange

    books_dir = (
        Path(__file__).resolve().parents[1] / "fixtures" / "books"
    ) / "resting_full_consume"
    return PaperExchange.from_fixture_dir(books_dir)


def _build_gateway_over(
    exchange: PaperExchange,
    store: SqliteLedgerStore,
    wal: WriteAheadLog,
    *,
    position_source: GatewayPositionSource | None = None,
) -> OrderGateway:
    """Build a recovery-wired `OrderGateway` over `exchange`/`store`/`wal`.

    Args:
        exchange: The paper exchange the Gateway submits through and the
            Reconciler later reconciles against.
        store: The SQLite-backed ledger, used as both `ledger_writer` (wrapped
            in `SqliteGatewayLedgerWriter`) and `ledger_reader`.
        wal: The write-ahead log the Gateway durably journals through.
        position_source: Optional reduce-only position source (issue #39).

    Returns:
        A fully wired `OrderGateway`, not yet recovered.
    """
    return OrderGateway(
        PaperSubmitter(exchange),
        verification_key=KEY_MATERIAL,
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=SqliteGatewayLedgerWriter(store),
        wal=wal,
        ledger_reader=store,
        reconciliation_source=exchange,
        position_source=position_source,
    )


# --- Benign heal: out-of-band fill is healed, not treated as an anomaly -------


def test_run_once_heals_a_gateway_placed_resting_order_that_fills_out_of_band(
    tmp_path: Path,
) -> None:
    """A BUY_TO_OPEN resting order that fills via `advance()` is healed.

    The fill never went through the Gateway (a real venue fill feed would
    surface the same effect asynchronously); `run_once()` recognizes it as a
    benign heal, ledgering exactly one `ReconciliationHealed` event with no
    halt.
    """
    exchange = _resting_full_consume_exchange()
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(exchange, store, wal)
    assert gateway.recover().halted is False

    intent = make_intent(
        market_ticker=_FULLCONSUME_TICKER,
        outcome="yes",
        action="buy",
        price=_FULLCONSUME_PRICE,
        size=ContractCentis(100),
        idempotency_key="idem-heal-open",
    )
    token = issue_matching_token(intent)
    result = gateway.process_intent(intent, token)
    assert result.outcome is SubmitOutcome.ACKED
    assert result.ack is not None
    assert result.ack.order_id is not None
    assert len(exchange.get_open_orders()) == 1

    exchange.advance()
    assert exchange.get_open_orders() == ()

    reconciler = Reconciler(
        gateway,
        ledger_reader=store,
        reconciliation_source=exchange,
        ledger_writer=SqliteGatewayLedgerWriter(store),
    )

    reconciler.run_once()

    assert gateway.halted is False
    assert gateway.accepting_approvals is True
    healed = [r for r in store.read_all() if r.event_type == "ReconciliationHealed"]
    assert len(healed) == 1
    data = json.loads(healed[0].payload_json)["data"]
    assert data["client_order_id"] == result.client_order_id
    store.close()


def test_run_once_heals_a_settled_close_and_retires_its_inflight_tally(
    tmp_path: Path,
) -> None:
    """A SELL_TO_CLOSE resting order that fully settles out-of-band heals.

    The heal also retires the settled close's in-flight-closing tally
    (issue #39): a subsequent, equal-size close against the *same* held
    position ACKs, which is only possible if the tally was retired rather
    than left standing.
    """
    exchange = _resting_full_consume_exchange()
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    position_source = _StubPositionSource((_position(100, ticker=_FULLCONSUME_TICKER),))
    gateway = _build_gateway_over(exchange, store, wal, position_source=position_source)
    assert gateway.recover().halted is False

    close_intent = make_intent(
        market_ticker=_FULLCONSUME_TICKER,
        outcome="yes",
        action="sell_to_close",
        price=_FULLCONSUME_PRICE,
        size=ContractCentis(100),
        idempotency_key="idem-heal-close",
    )
    close_token = issue_matching_token(close_intent)
    closed = gateway.process_intent(close_intent, close_token)
    assert closed.outcome is SubmitOutcome.ACKED
    assert closed.ack is not None
    assert closed.ack.order_id is not None

    exchange.advance()

    reconciler = Reconciler(
        gateway,
        ledger_reader=store,
        reconciliation_source=exchange,
        ledger_writer=SqliteGatewayLedgerWriter(store),
    )
    reconciler.run_once()

    assert gateway.halted is False

    second_intent = make_intent(
        market_ticker=_FULLCONSUME_TICKER,
        outcome="yes",
        action="sell_to_close",
        price=_FULLCONSUME_PRICE,
        size=ContractCentis(100),
        idempotency_key="idem-after-heal-close",
    )
    second_token = issue_matching_token(second_intent)
    second_result = gateway.process_intent(second_intent, second_token)

    assert second_result.outcome is SubmitOutcome.ACKED
    store.close()


# --- Unexplained mismatch: foreign open order halts ---------------------------


def test_run_once_halts_on_an_untracked_foreign_open_order(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """A resting order the Gateway never placed halts reconciliation.

    Appearing mid-operation (after a clean boot `.recover()`) with zero
    WAL/ledger trace, `run_once()` ledgers `ReconciliationHalted(reason=
    "foreign_open_order")` and the Gateway itself becomes `.halted` and stops
    `accepting_approvals`.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    paper_exchange.place_order(
        PaperOrderIntent(
            ticker=DEFAULT_MARKET_TICKER,
            side="yes",
            price=PricePips(4000),
            quantity=ContractCentis(5),
        ),
        object(),
    )

    reconciler = Reconciler(
        gateway,
        ledger_reader=store,
        reconciliation_source=paper_exchange,
        ledger_writer=SqliteGatewayLedgerWriter(store),
    )

    reconciler.run_once()

    assert gateway.halted is True
    assert gateway.accepting_approvals is False
    halts = [r for r in store.read_all() if r.event_type == "ReconciliationHalted"]
    assert len(halts) == 1
    data = json.loads(halts[0].payload_json)["data"]
    assert data["reason"] == "foreign_open_order"
    store.close()


# --- Closed allowlist: vanished-with-no-fill halts, never silently heals ------


def test_run_once_halts_on_a_vanished_tracked_order_with_no_matching_fill(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """A tracked order vanishing with *no* fill trace halts, never heals.

    An out-of-band cancellation (no Gateway involvement, no `Fill` emitted)
    is a genuine anomaly: `run_once()` halts with `reason ==
    "vanished_order_no_fill"` rather than assuming a benign fill.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    intent = make_intent(action="buy", idempotency_key="idem-vanish-no-fill")
    token = issue_matching_token(intent)
    result = gateway.process_intent(intent, token)
    assert result.outcome is SubmitOutcome.ACKED
    assert result.ack is not None
    assert result.ack.order_id is not None
    assert len(paper_exchange.get_open_orders()) == 1

    paper_exchange.cancel_order(result.ack.order_id)
    assert paper_exchange.get_open_orders() == ()

    reconciler = Reconciler(
        gateway,
        ledger_reader=store,
        reconciliation_source=paper_exchange,
        ledger_writer=SqliteGatewayLedgerWriter(store),
    )

    reconciler.run_once()

    assert gateway.halted is True
    halts = [r for r in store.read_all() if r.event_type == "ReconciliationHalted"]
    assert len(halts) == 1
    data = json.loads(halts[0].payload_json)["data"]
    assert data["reason"] == "vanished_order_no_fill"
    store.close()


# --- Loop discipline: bounded, deterministic, whole-second default ------------


def test_run_terminates_deterministically_after_max_cycles(
    paper_exchange: PaperExchange, tmp_path: Path
) -> None:
    """`run(max_cycles=2, interval=0, stop_event=...)` returns deterministically.

    Mirrors `OrderGateway.run`'s bounded-loop contract: the call must return
    (never hang) once the beat budget is exhausted.
    """
    db_path = tmp_path / "ledger.db"
    wal_path = tmp_path / "wal.jsonl"
    store = SqliteLedgerStore(db_path)
    wal = WriteAheadLog(wal_path)
    gateway = _build_gateway_over(paper_exchange, store, wal)
    assert gateway.recover().halted is False

    reconciler = Reconciler(
        gateway,
        ledger_reader=store,
        reconciliation_source=paper_exchange,
        ledger_writer=SqliteGatewayLedgerWriter(store),
    )

    reconciler.run(max_cycles=2, interval=0, stop_event=threading.Event())

    # Reaching this line (no hang, no exception) is the pass condition; the
    # gateway must still be healthy since nothing needed reconciling.
    assert gateway.halted is False
    store.close()


def test_reconciler_default_interval_is_the_whole_second_int_sixty() -> None:
    """The default `interval` is the int `60` -- never a float (SPEC S6.1)."""
    default = inspect.signature(Reconciler.__init__).parameters["interval"].default

    assert default == 60
    assert isinstance(default, int)
    assert not isinstance(default, bool)
