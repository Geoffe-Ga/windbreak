"""The Order Gateway chaos suite: SPEC S11.5 acceptance criteria (issue #42).

Structured in TDD order:

    1. `TestInvariantSelfChecks` -- RED-first: each of the four
       `tests/chaos/invariants.py` checkers must DETECT synthetic broken
       state before it is trusted to guard any real scenario below. This is
       what keeps the suite from being vacuous (a checker that never fires
       would let every scenario below pass for the wrong reason).
    2. The six SPEC S11.5 scenario families, individually: kill-at-every-edge
       (reusing `tests/order_gateway/test_recovery.py`'s own `_KILL_MATRIX`
       taxonomy), network-cut-mid-submit, duplicate-ACK, out-of-order-fills,
       missed-fill, and cancel/fill-race.
    3. A deterministic, fixed-seed storm (`CHAOS_SEEDS`) and a Hypothesis
       storm, each composing a small, seeded combination of the six families
       against a stream of random intents.

Every scenario drives the system to quiescence (`recover()` ->
`Reconciler.run_once()`/`run()` to fixpoint -> `Sweeper.sweep_once()`/`run()`
to fixpoint) and then asserts all four SPEC S11.5 invariants: zero duplicate
live orders, zero orders without valid tokens, zero net-short positions, and
correct reservation release. A fail-closed HALT counts as convergence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.chaos.conftest import (
    ALL_FAULT_KINDS,
    ChaosHarness,
    FaultSpec,
    NetworkCutError,
    SimulatedCrashError,
    _FixedFillsReconciliationSource,
    random_faults,
    random_intent_stream,
    resting_full_consume_exchange,
)
from tests.chaos.invariants import (
    GatewaySnapshot,
    assert_all_invariants,
    assert_no_duplicate_live_orders,
    assert_no_net_short_positions,
    assert_no_tokenless_orders,
    assert_reservations_balanced,
)
from tests.order_gateway.conftest import (
    DEFAULT_MARKET_TICKER,
    DEFAULT_NOW_EPOCH_S,
    make_intent,
)
from tests.order_gateway.test_recovery import _KILL_MATRIX
from windbreak.connector.models import Fill, OpenOrder, Position
from windbreak.ledger.events import canonical_json
from windbreak.ledger.store import LedgerRecord
from windbreak.numeric.types import ContractCentis, PricePips
from windbreak.order_gateway.wal import WalRecord

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from windbreak.connector.paper import PaperExchange
    from windbreak.order_gateway.gateway import GatewayResult

#: The `resting_full_consume` fixture's sole ticker (see
#: `tests/order_gateway/test_reconciler.py`'s own `_FULLCONSUME_TICKER`): a
#: single yes-bid resting order that fully fills out-of-band on `advance()`
#: via a trade-through print recorded strictly below its limit.
_FULLCONSUME_TICKER = "MKT-FULLCONSUME"

#: The resting order's limit price in the `resting_full_consume` fixture:
#: below the fixture's 4400-pip ask (never crosses at placement) and above
#: the fixture's 4150-pip trade print (a strict trade-through on `advance()`).
_FULLCONSUME_PRICE = PricePips(4200)

#: A committed, fixed tuple of chaos seeds for the deterministic storm; the
#: seed appears in the pytest test id, so a failure names exactly which storm
#: reproduces it (`pytest tests/chaos -k <seed>`).
CHAOS_SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)


def _ledger_record(
    event_type: str, data: dict[str, object], *, sequence_number: int = 1
) -> LedgerRecord:
    """Build a synthetic `LedgerRecord` carrying `data` under `event_type`.

    Args:
        event_type: The record's event-type discriminator.
        data: The record's decoded payload data.
        sequence_number: The record's position (irrelevant to the pure
            invariant checkers, which never verify the hash chain).

    Returns:
        A `LedgerRecord` whose `payload_json` round-trips `data` exactly as
        `tests/chaos/invariants.py`'s `_payload_data` helper decodes it.
    """
    payload_json = canonical_json(
        {"component": "order_gateway", "data": data, "schema_version": 1}
    )
    return LedgerRecord(
        sequence_number=sequence_number,
        event_type=event_type,
        created_at="2024-01-01T00:00:00.000000+00:00",
        component="order_gateway",
        payload_json=payload_json,
        payload_schema_version=1,
        prev_hash="0" * 64,
        event_hash="f" * 64,
    )


def _transition_record(
    coid: str, from_state: str, event: str, to_state: str, *, sequence_number: int
) -> LedgerRecord:
    """Build a synthetic `OrderTransitionLedgered`-shaped `LedgerRecord`.

    Args:
        coid: The client-order-id the transition belongs to.
        from_state: The `OrderState.name` the transition moved from.
        event: The `OrderEvent.name` driving the transition.
        to_state: The `OrderState.name` the transition moved to.
        sequence_number: The record's position.

    Returns:
        The synthetic ledger record.
    """
    return _ledger_record(
        "OrderTransitionLedgered",
        {
            "client_order_id": coid,
            "from_state": from_state,
            "event": event,
            "to_state": to_state,
        },
        sequence_number=sequence_number,
    )


def _ack_record(coid: str, order_id: str | None) -> WalRecord:
    """Build a synthetic WAL ack record correlating `order_id` to `coid`.

    Args:
        coid: The intent's content-addressed client-order-id.
        order_id: The venue's resting-order id, or `None`.

    Returns:
        The synthetic `WalRecord`.
    """
    return WalRecord(
        kind="ack",
        client_order_id=coid,
        intent=None,
        order_id=order_id,
        filled=ContractCentis(0),
    )


def _transition_chain(
    ledger_records: Sequence[LedgerRecord],
) -> tuple[tuple[object, object, object], ...]:
    """Extract every ledgered `(from_state, event, to_state)` triple, in order.

    Args:
        ledger_records: The durable ledger records to scan.

    Returns:
        One triple per `OrderTransitionLedgered` record, in ledger (append)
        order.
    """
    chain: list[tuple[object, object, object]] = []
    for record in ledger_records:
        if record.event_type != "OrderTransitionLedgered":
            continue
        data = json.loads(record.payload_json)["data"]
        chain.append((data["from_state"], data["event"], data["to_state"]))
    return tuple(chain)


def _open_order(order_id: str, *, price_pips: int = 4600) -> OpenOrder:
    """Build a synthetic `OpenOrder` resting on `DEFAULT_MARKET_TICKER`.

    Args:
        order_id: The venue order id.
        price_pips: The order's limit price, in pips.

    Returns:
        The synthetic `OpenOrder`.
    """
    return OpenOrder(
        id=order_id,
        ticker=DEFAULT_MARKET_TICKER,
        side="yes",
        price=PricePips(price_pips),
        quantity=ContractCentis(10),
    )


# =============================================================================
# 1. TestInvariantSelfChecks -- RED-first: each checker must detect a break.
# =============================================================================


@pytest.mark.chaos
class TestInvariantSelfChecks:
    """Each invariant checker must DETECT synthetic broken state, and must
    NOT fire on the corresponding healthy state -- proving the four checkers
    guard the real scenario families below for the right reason, not
    vacuously.

    Marked `@pytest.mark.chaos` (class-level, applying to every method) so
    the standalone, merge-gating `pytest -m chaos` CI job -- which never runs
    the full `quality` job's unmarked tests -- self-validates its own
    invariant checkers rather than trusting them un-exercised.
    """

    # --- Invariant 1: zero duplicate live orders ------------------------------

    def test_detects_two_live_orders_sharing_one_client_order_id(self) -> None:
        """Two currently-open venue orders both tracing to the same coid."""
        snapshot = GatewaySnapshot(
            ledger_records=(),
            wal_records=(
                _ack_record("coid-shared", "order-1"),
                _ack_record("coid-shared", "order-2"),
            ),
            open_orders=(_open_order("order-1"), _open_order("order-2")),
        )

        with pytest.raises(AssertionError, match="duplicate live venue orders"):
            assert_no_duplicate_live_orders(snapshot)

    def test_accepts_two_live_orders_with_distinct_client_order_ids(self) -> None:
        """Two currently-open venue orders tracing to distinct coids is fine."""
        snapshot = GatewaySnapshot(
            ledger_records=(),
            wal_records=(
                _ack_record("coid-a", "order-1"),
                _ack_record("coid-b", "order-2"),
            ),
            open_orders=(_open_order("order-1"), _open_order("order-2")),
        )

        assert_no_duplicate_live_orders(snapshot)

    # --- Invariant 2: zero orders without valid tokens ------------------------

    def test_detects_an_exchange_order_with_no_ledger_origin_chain(self) -> None:
        """A resting order with zero WAL/ledger trace and no halt flagging it."""
        snapshot = GatewaySnapshot(
            ledger_records=(),
            wal_records=(),
            open_orders=(_open_order("mystery-order"),),
        )

        with pytest.raises(AssertionError, match="no Gateway-verified token trace"):
            assert_no_tokenless_orders(snapshot)

    def test_accepts_a_live_order_with_a_valid_wal_ack_trace(self) -> None:
        """A normal resting order with a genuine WAL ack trace is fine."""
        snapshot = GatewaySnapshot(
            ledger_records=(),
            wal_records=(_ack_record("coid-normal", "order-normal"),),
            open_orders=(_open_order("order-normal"),),
        )

        assert_no_tokenless_orders(snapshot)

    def test_excuses_an_unaccounted_order_already_flagged_by_a_halt(self) -> None:
        """The identical unaccounted order is fine once a halt names it."""
        halt = _ledger_record(
            "ReconciliationHalted",
            {
                "reason": "foreign_open_order",
                "ticker": DEFAULT_MARKET_TICKER,
                "venue_order_id": "mystery-order",
                "client_order_id": "",
                "detail": "resting order on the venue has no durable trace",
            },
        )
        snapshot = GatewaySnapshot(
            ledger_records=(halt,),
            wal_records=(),
            open_orders=(_open_order("mystery-order"),),
        )

        assert_no_tokenless_orders(snapshot)

    # --- Invariant 3: zero net-short positions --------------------------------

    def test_detects_a_standing_net_short_with_no_halt(self) -> None:
        """A negative held position with no `ReduceOnlyViolation` latched."""
        snapshot = GatewaySnapshot(
            ledger_records=(),
            positions=(
                Position(
                    ticker=DEFAULT_MARKET_TICKER,
                    quantity=ContractCentis(-50),
                    average_price=PricePips(4600),
                ),
            ),
        )

        with pytest.raises(AssertionError, match="net-short position"):
            assert_no_net_short_positions(snapshot)

    def test_excuses_a_net_short_position_once_a_violation_halt_is_latched(
        self,
    ) -> None:
        """The identical negative position is fine once the halt is latched."""
        violation = _ledger_record(
            "ReduceOnlyViolation",
            {
                "client_order_id": "coid",
                "ticker": DEFAULT_MARKET_TICKER,
                "held_centis": 50,
                "filled_centis": 100,
                "net_centis": -50,
            },
        )
        snapshot = GatewaySnapshot(
            ledger_records=(violation,),
            positions=(
                Position(
                    ticker=DEFAULT_MARKET_TICKER,
                    quantity=ContractCentis(-50),
                    average_price=PricePips(4600),
                ),
            ),
        )

        assert_no_net_short_positions(snapshot)

    def test_does_not_excuse_a_net_short_on_an_unrelated_ticker(self) -> None:
        """A `ReduceOnlyViolation` naming one ticker must not excuse a
        standing net-short on a *different*, unnamed ticker (the halt is
        process-wide, but it says nothing about any other ticker's position).
        """
        violation = _ledger_record(
            "ReduceOnlyViolation",
            {
                "client_order_id": "coid",
                "ticker": DEFAULT_MARKET_TICKER,
                "held_centis": 50,
                "filled_centis": 100,
                "net_centis": -50,
            },
        )
        snapshot = GatewaySnapshot(
            ledger_records=(violation,),
            positions=(
                Position(
                    ticker=_FULLCONSUME_TICKER,
                    quantity=ContractCentis(-25),
                    average_price=PricePips(4200),
                ),
            ),
        )

        with pytest.raises(AssertionError, match="net-short position"):
            assert_no_net_short_positions(snapshot)

    # --- Invariant 4: correct reservation release -----------------------------

    def test_detects_a_reservation_transition_re_recorded_out_of_sequence(self) -> None:
        """A coid re-ledgers SUBMIT/ACK from SUBMISSION_REQUESTED after it is
        already ACKED -- the double-open/double-release symptom a corrupted
        recovery rehydration would leave (see `invariants.py`'s docstring).
        """
        coid = "coid-double-release"
        records = (
            _transition_record(
                coid, "INTENT_CREATED", "APPROVE", "APPROVED", sequence_number=1
            ),
            _transition_record(
                coid,
                "APPROVED",
                "REQUEST_SUBMISSION",
                "SUBMISSION_REQUESTED",
                sequence_number=2,
            ),
            _transition_record(
                coid, "SUBMISSION_REQUESTED", "SUBMIT", "SUBMITTED", sequence_number=3
            ),
            _transition_record(coid, "SUBMITTED", "ACK", "ACKED", sequence_number=4),
            _transition_record(
                coid, "SUBMISSION_REQUESTED", "SUBMIT", "SUBMITTED", sequence_number=5
            ),
        )
        snapshot = GatewaySnapshot(ledger_records=records)

        with pytest.raises(AssertionError, match="disagrees with the replayed state"):
            assert_reservations_balanced(snapshot)

    def test_detects_an_illegal_replayed_transition(self) -> None:
        """A coid ledgers an event with no legal edge from its replayed state."""
        coid = "coid-illegal"
        records = (
            _transition_record(
                coid, "INTENT_CREATED", "APPROVE", "APPROVED", sequence_number=1
            ),
            _transition_record(coid, "APPROVED", "FILL", "FILLED", sequence_number=2),
        )
        snapshot = GatewaySnapshot(ledger_records=records)

        with pytest.raises(AssertionError, match="illegal replayed transition"):
            assert_reservations_balanced(snapshot)

    def test_accepts_a_single_legal_chain_to_a_terminal_state(self) -> None:
        """A coid's ledgered history is a single legal chain to FILLED."""
        coid = "coid-legal"
        records = (
            _transition_record(
                coid, "INTENT_CREATED", "APPROVE", "APPROVED", sequence_number=1
            ),
            _transition_record(
                coid,
                "APPROVED",
                "REQUEST_SUBMISSION",
                "SUBMISSION_REQUESTED",
                sequence_number=2,
            ),
            _transition_record(
                coid, "SUBMISSION_REQUESTED", "SUBMIT", "SUBMITTED", sequence_number=3
            ),
            _transition_record(coid, "SUBMITTED", "ACK", "ACKED", sequence_number=4),
            _transition_record(coid, "ACKED", "FILL", "FILLED", sequence_number=5),
        )
        snapshot = GatewaySnapshot(ledger_records=records)

        assert_reservations_balanced(snapshot)


# =============================================================================
# 2. Family 1: kill-at-every-edge (reuses test_recovery.py's _KILL_MATRIX).
# =============================================================================


@pytest.mark.chaos
@pytest.mark.parametrize(
    "kill_after,expected_halted",
    [(point[0], point[2]) for point in _KILL_MATRIX],
    ids=[point[1] for point in _KILL_MATRIX],
)
def test_kill_at_every_state_edge_converges(
    chaos_harness: ChaosHarness, kill_after: int, expected_halted: bool
) -> None:
    """A crash at any of the seven durable write points converges safely.

    A fresh intent is driven through a Gateway killed after the `kill_after`th
    durable write; the restarted Gateway either resumes cleanly or halts
    `"ambiguous_match"` -- both are convergence -- and every invariant holds
    either way (SPEC S11.5, mirroring `test_recovery.py`'s own kill matrix).
    """
    intent = make_intent(idempotency_key=f"chaos-kill-{kill_after}")

    run = chaos_harness.run(
        intents=(intent,),
        faults=(
            FaultSpec(
                name=f"kill-after-{kill_after}",
                seam="wal",
                kind="kill_after",
                kill_after=kill_after,
            ),
        ),
    )

    assert run.halted is expected_halted
    assert len(run.snapshot.open_orders) <= 1
    assert_all_invariants(run.snapshot)


# =============================================================================
# 3. Family 2: network-cut-mid-submit.
# =============================================================================


@pytest.mark.chaos
def test_network_cut_mid_submit_converges_to_an_ambiguous_halt(
    chaos_harness: ChaosHarness,
) -> None:
    """A network cut right after the venue accepts an order halts safely.

    The venue-side placement is real, but the ack never reaches the Gateway;
    the restart halts `"ambiguous_match"` (the same resolution as kill point
    4) rather than guessing, and every invariant holds in that halted state.
    """
    intent = make_intent(idempotency_key="chaos-network-cut")

    run = chaos_harness.run(
        intents=(intent,),
        faults=(FaultSpec(name="network-cut", seam="submitter", kind="network_cut"),),
    )

    assert run.halted is True
    assert len(run.raised) == 1
    assert isinstance(run.raised[0], NetworkCutError)
    halts = [
        record
        for record in run.snapshot.ledger_records
        if record.event_type == "ReconciliationHalted"
    ]
    assert len(halts) == 1
    data = json.loads(halts[0].payload_json)["data"]
    assert data["reason"] == "ambiguous_match"
    assert_all_invariants(run.snapshot)


# =============================================================================
# 4. Family 3: duplicate-ACK.
# =============================================================================


@pytest.mark.chaos
def test_duplicate_ack_on_a_clean_submission_is_harmless(
    chaos_harness: ChaosHarness,
) -> None:
    """A redelivered (duplicate) WAL ack on an otherwise-clean submission
    changes nothing observable: recovery's rehydration sees the coid already
    complete (fully ledgered) and is a no-op on the second, duplicate record.
    """
    intent = make_intent(idempotency_key="chaos-dup-ack-clean")

    run = chaos_harness.run(
        intents=(intent,),
        faults=(FaultSpec(name="dup-ack", seam="wal", kind="duplicate_ack"),),
    )

    assert run.halted is False
    assert not run.raised
    assert_all_invariants(run.snapshot)


@pytest.mark.chaos
def test_duplicate_ack_during_a_mid_submission_crash_keeps_reservations_balanced(
    chaos_harness: ChaosHarness,
) -> None:
    """A duplicate WAL ack landing exactly at the "after-wal-ack" crash point
    (kill point 5, `_KILL_MATRIX`) must not corrupt recovery's rehydration:
    the duplicate record's second rehydration must not re-ledger a second
    SUBMIT/ACK transition pair for a coid recovery has already completed on
    its first pass over the (identical) first ack record.
    """
    intent = make_intent(idempotency_key="chaos-dup-ack-crash")

    run = chaos_harness.run(
        intents=(intent,),
        faults=(
            FaultSpec(name="dup-ack", seam="wal", kind="duplicate_ack"),
            FaultSpec(
                name="kill-after-wal-ack", seam="wal", kind="kill_after", kill_after=5
            ),
        ),
    )

    assert len(run.raised) == 1
    assert isinstance(run.raised[0], SimulatedCrashError)
    assert_all_invariants(run.snapshot)


# =============================================================================
# 5. Family 4: out-of-order-fills.
# =============================================================================


@pytest.mark.chaos
def test_out_of_order_fills_heal_identically_regardless_of_feed_order(
    tmp_path: Path,
) -> None:
    """A multi-fill heal is insensitive to the fill feed's presentation order.

    A resting order is bypass-cancelled (an out-of-band consumption), and a
    fixed pair of fills summing to its full size is reported through
    `get_fills()` -- once in natural order, once reversed. Both runs heal
    identically (`matched_fill_centis` sums regardless of order): not merely
    "each heals exactly once", but the *same* client_order_id is healed with
    the *same* `ReconciliationHealed` content, driven through the *same*
    ledgered `OrderTransitionLedgered` chain (there is no raw fill-quantity
    field ledgered anywhere -- the transition chain reaching `FILLED` is the
    durable, external proxy for "the full 100-centis size was matched" in
    both orderings). Every invariant holds either way.
    """
    intent = make_intent(
        market_ticker=_FULLCONSUME_TICKER,
        outcome="yes",
        action="buy",
        price=_FULLCONSUME_PRICE,
        size=ContractCentis(100),
        idempotency_key="chaos-out-of-order-fills",
    )
    fill_a = Fill(
        id="synthetic-fill-a",
        ticker=_FULLCONSUME_TICKER,
        side="yes",
        price=_FULLCONSUME_PRICE,
        quantity=ContractCentis(60),
        ts=datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
    )
    fill_b = Fill(
        id="synthetic-fill-b",
        ticker=_FULLCONSUME_TICKER,
        side="yes",
        price=_FULLCONSUME_PRICE,
        quantity=ContractCentis(40),
        ts=datetime(2025, 1, 1, 0, 1, tzinfo=UTC),
    )

    def _cancel_the_resting_order(
        exchange: PaperExchange, results: list[GatewayResult]
    ) -> None:
        ack = results[0].ack
        assert ack is not None
        assert ack.order_id is not None
        exchange.cancel_order(ack.order_id)

    healed_counts: list[int] = []
    healed_payloads: list[dict[str, object]] = []
    transition_chains: list[tuple[tuple[object, object, object], ...]] = []
    for ordering, fills in (
        ("forward", (fill_a, fill_b)),
        ("reversed", (fill_b, fill_a)),
    ):
        harness_dir = tmp_path / ordering
        harness_dir.mkdir()
        harness = ChaosHarness(harness_dir)

        run = harness.run(
            intents=(intent,),
            exchange_factory=resting_full_consume_exchange,
            before_reconcile=_cancel_the_resting_order,
            reconciliation_source_factory=lambda source, f=fills: (
                _FixedFillsReconciliationSource(source, f)
            ),
        )

        assert run.halted is False, ordering
        healed = [
            record
            for record in run.snapshot.ledger_records
            if record.event_type == "ReconciliationHealed"
        ]
        assert len(healed) == 1, ordering
        healed_counts.append(len(healed))
        healed_payloads.append(json.loads(healed[0].payload_json)["data"])
        # The sole intent's own transition chain: with only one intent driven
        # through this harness run, every ledgered `OrderTransitionLedgered`
        # belongs to its one client_order_id, so `_transition_chain`'s result
        # *is* this coid's full chain.
        transition_chains.append(_transition_chain(run.snapshot.ledger_records))
        assert_all_invariants(run.snapshot)

    assert healed_counts == [1, 1]
    assert healed_payloads[0] == healed_payloads[1], (
        "forward and reversed feed orderings healed with different "
        f"ReconciliationHealed content: {healed_payloads!r}"
    )
    assert transition_chains[0] == transition_chains[1], (
        "forward and reversed feed orderings drove different ledgered "
        f"transition chains -- not a truly identical heal: {transition_chains!r}"
    )
    assert transition_chains[0][-1][-1] == "FILLED", (
        f"the heal did not converge to FILLED: {transition_chains[0]!r}"
    )


# =============================================================================
# 6. Family 5: missed-fill.
# =============================================================================


@pytest.mark.chaos
def test_missed_fill_notification_converges_to_a_safe_halt(
    chaos_harness: ChaosHarness,
) -> None:
    """A dropped fill notification on a vanished tracked order halts safely.

    The order genuinely, fully fills out-of-band (`advance()`), but the fill
    feed drops every fill (`drop_ppm=1_000_000`): the Reconciler cannot tell
    this apart from a genuine anomaly and halts `"vanished_order_no_fill"`
    rather than silently miscounting -- the safe, convergent outcome.
    """
    intent = make_intent(
        market_ticker=_FULLCONSUME_TICKER,
        outcome="yes",
        action="buy",
        price=_FULLCONSUME_PRICE,
        size=ContractCentis(100),
        idempotency_key="chaos-missed-fill",
    )

    run = chaos_harness.run(
        intents=(intent,),
        exchange_factory=resting_full_consume_exchange,
        advance_cycles=1,
        faults=(
            FaultSpec(
                name="drop-all-fills",
                seam="reconciliation_source",
                kind="drop_fills",
                drop_ppm=1_000_000,
            ),
        ),
    )

    assert run.halted is True
    halts = [
        record
        for record in run.snapshot.ledger_records
        if record.event_type == "ReconciliationHalted"
    ]
    assert len(halts) == 1
    data = json.loads(halts[0].payload_json)["data"]
    assert data["reason"] == "vanished_order_no_fill"
    assert_all_invariants(run.snapshot)


# =============================================================================
# 7. Family 6: cancel/fill-race.
# =============================================================================


@pytest.mark.chaos
def test_cancel_fill_race_resolves_filled_never_double_cancelled(
    chaos_harness: ChaosHarness,
) -> None:
    """A TTL-stale order fully consumed out-of-band resolves FILLED.

    The Sweeper (not the Reconciler, which is skipped here via
    `reconcile_cycles=0` so the race is genuinely the Sweeper's to resolve)
    finds the order already gone from the venue *with* a corroborating fill
    on its very first cycle and resolves it via the
    `(CANCEL_REQUESTED, FILL) -> FILLED` edge -- never ledgering `CANCEL` --
    mirroring `test_sweeper.py`'s own cancel/fill-race test.
    """
    intent = make_intent(
        market_ticker=_FULLCONSUME_TICKER,
        outcome="yes",
        action="buy",
        price=_FULLCONSUME_PRICE,
        size=ContractCentis(100),
        idempotency_key="chaos-cancel-fill-race",
    )

    run = chaos_harness.run(
        intents=(intent,),
        exchange_factory=resting_full_consume_exchange,
        advance_cycles=1,
        reconcile_cycles=0,
        sweeper_now=DEFAULT_NOW_EPOCH_S + 900,
    )

    assert run.halted is False
    assert run.snapshot.open_orders == ()
    fill_events = [
        record
        for record in run.snapshot.ledger_records
        if record.event_type == "OrderTransitionLedgered"
        and json.loads(record.payload_json)["data"]["event"] == "FILL"
    ]
    cancel_events = [
        record
        for record in run.snapshot.ledger_records
        if record.event_type == "OrderTransitionLedgered"
        and json.loads(record.payload_json)["data"]["event"] == "CANCEL"
    ]
    assert fill_events
    assert not cancel_events
    assert_all_invariants(run.snapshot)


# =============================================================================
# 8. Deterministic and Hypothesis-driven combination storms.
# =============================================================================


@pytest.mark.chaos
@pytest.mark.parametrize("seed", CHAOS_SEEDS)
def test_deterministic_fault_storm_preserves_invariants(
    seed: int, tmp_path: Path
) -> None:
    """A fixed-seed combination of faults over a short intent stream always
    converges with every SPEC S11.5 invariant holding. Reproduce a failure
    locally with `pytest tests/chaos -m chaos -k test_deterministic_fault_storm
    and <seed>`.
    """
    harness = ChaosHarness(tmp_path)
    intents = random_intent_stream(seed, n=15)
    faults = random_faults(seed, kinds=ALL_FAULT_KINDS, max_faults=2)

    run = harness.run(intents=intents, faults=faults)

    assert_all_invariants(run.snapshot)


@pytest.mark.chaos
@settings(max_examples=25, deadline=None, print_blob=True)
@given(seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_hypothesis_fault_storm_preserves_invariants(
    seed: int, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A Hypothesis-drawn seed derives a short, fully deterministic intent
    stream and fault combination (see `random_intent_stream`/`random_faults`)
    that must always converge with every invariant holding.

    Uses `tmp_path_factory` (session-scoped) rather than the function-scoped
    `tmp_path` fixture: `@given` re-invokes this function body for every
    drawn example within a single pytest test-function call, so a
    function-scoped fixture would be created once and reused (and collide)
    across examples, while `tmp_path_factory.mktemp` gives each drawn `seed`
    its own fresh scratch directory.

    A failure prints the drawn `seed` (`print_blob=True`); reproduce it with
    `pytest --hypothesis-seed=<n>` or the printed `@reproduce_failure` blob.
    """
    harness_dir = tmp_path_factory.mktemp(f"chaos-hypothesis-{seed}")
    harness = ChaosHarness(harness_dir)
    intents = random_intent_stream(seed, n=12)
    faults = random_faults(seed, kinds=ALL_FAULT_KINDS, max_faults=2)

    run = harness.run(intents=intents, faults=faults)

    assert_all_invariants(run.snapshot)
