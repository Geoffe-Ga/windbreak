"""Failing-first tests for the `reconciliation-mismatch` drill (issue #59, RED).

`windbreak.drills.reconciliation_mismatch` does not exist yet, so the import
below fails collection with `ModuleNotFoundError` -- the expected Gate 1 RED
state for issue #59.

This file composes two independent, already-shipped mechanisms into one
"reconciliation mismatch" scenario:

    * `windbreak.order_gateway.reconciler.Reconciler`, driven here by a
      lightweight scripted `reconciliation_source`/gateway double (rather
      than a full `OrderGateway` + `PaperExchange`, since the scenario this
      drill demonstrates only needs the Reconciler's own diff/halt/heal
      contract): a `run_once()` cycle over a tracked order that vanished from
      the venue with no corroborating fill halts the Gateway; a cycle over a
      benign out-of-band fill heals it instead.
    * `windbreak.riskkernel.kill.ReconciliationMismatchMonitor`, the kill
      switch's own alerting leg for repeated verification breaches: at
      `threshold=1` the first `BREACH` observation immediately kills and
      dispatches `HALT_KILL`.

`RecoveryCompleted` (emitted by a real `OrderGateway.recover()` cycle) is
already covered end-to-end by `tests/order_gateway/test_recovery.py` and is
out of this drill's narrower scripted-double scope; "recovery" here means a
fresh reconciliation cycle -- simulating an operator restart after resolving
the mismatch -- that reconciles cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from tests.drills.conftest import (
    FIXED_EPOCH_S,
    InMemoryDrillLedgerWriter,
    RecordingAlertSink,
)
from windbreak.alerts.registry import AlertType
from windbreak.connector.models import Fill, OpenOrder
from windbreak.drills import reconciliation_mismatch as recon_drill_module
from windbreak.drills.context import DrillContext
from windbreak.drills.framework import DrillFailedError
from windbreak.drills.reconciliation_mismatch import ReconciliationMismatchDrill
from windbreak.numeric.types import ContractCentis, PricePips
from windbreak.order_gateway.reconciler import ReconcileOutcome, Reconciler
from windbreak.order_gateway.recovery import TrackedOrder
from windbreak.riskkernel.kill import KillSwitch, ReconciliationMismatchMonitor
from windbreak.riskkernel.modes import Mode, ModeStateMachine
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter
from windbreak.riskkernel.verification import VerificationOutcome

if TYPE_CHECKING:
    from pathlib import Path

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TICKER = "MKT-RECON"


def _tracked_order(order_id: str = "venue-1", filled_centis: int = 0) -> TrackedOrder:
    """Build one representative Gateway-placed tracked order."""
    return TrackedOrder(
        client_order_id="coid-1",
        order_id=order_id,
        ticker=_TICKER,
        side="yes",
        price_pips=5000,
        size_centis=100,
        action="buy",
        filled_centis=filled_centis,
    )


class _FakeGateway:
    """A minimal `Reconciler`-facing gateway double.

    Exposes exactly the surface `Reconciler` reads/mutates:
    `tracked_orders`, `mark_halted`, `retire_tracked_order`, and the
    `.halted`/`.accepting_approvals` flags -- never a real `OrderGateway`,
    since this drill's reconciliation-mismatch scenario is scripted
    end-to-end.
    """

    def __init__(self, tracked: tuple[TrackedOrder, ...]) -> None:
        """Seed the gateway with `tracked`'s resting orders."""
        self._tracked = list(tracked)
        self.halted = False
        self.accepting_approvals = True

    def tracked_orders(self) -> tuple[TrackedOrder, ...]:
        """Return the currently tracked resting orders."""
        return tuple(self._tracked)

    def mark_halted(self) -> None:
        """Latch halted and stop accepting approvals."""
        self.halted = True
        self.accepting_approvals = False

    def retire_tracked_order(self, order: TrackedOrder) -> None:
        """Remove `order` from the tracked set."""
        self._tracked = [o for o in self._tracked if o.order_id != order.order_id]


class _ScriptedSource:
    """A `ReconciliationSourceProtocol` double returning one fixed scenario:
    the same `(open_orders, fills)` pair on every call, so one `run_once()`
    cycle sees a single, internally consistent snapshot of the venue.
    """

    def __init__(
        self, open_orders: tuple[OpenOrder, ...], fills: tuple[Fill, ...]
    ) -> None:
        """Seed the fixed `(open_orders, fills)` this source always returns."""
        self._open_orders = open_orders
        self._fills = fills

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Return the seeded open orders."""
        return self._open_orders

    def get_fills(self, since: datetime) -> tuple[Fill, ...]:
        """Return the seeded fills, ignoring `since`."""
        del since
        return self._fills


class _EmptyLedgerReader:
    """A `LedgerReaderProtocol` double that always reads back no records."""

    def read_all(self) -> list[object]:
        """Return an empty record list."""
        return []


# --- Halt phase: a tracked order vanishes with no corroborating fill -----------


def test_run_once_halts_on_a_vanished_tracked_order_with_no_fill() -> None:
    """A tracked order missing from the venue's open orders, with no matching
    fill, halts reconciliation: `ReconciliationHalted` is ledgered and the
    gateway latches halted/stops accepting approvals.
    """
    tracked = _tracked_order()
    gateway = _FakeGateway((tracked,))
    source = _ScriptedSource((), ())
    writer = InMemoryKernelLedgerWriter()
    reconciler = Reconciler(
        gateway,
        ledger_reader=_EmptyLedgerReader(),
        reconciliation_source=source,
        ledger_writer=writer,
    )

    outcome = reconciler.run_once()

    assert outcome.halted is True
    assert outcome.halt_reason == "vanished_order_no_fill"
    assert gateway.halted is True
    assert gateway.accepting_approvals is False
    halts = [e for e in writer.events if e.event_type == "ReconciliationHalted"]
    assert len(halts) == 1


def test_run_once_halts_on_an_untracked_foreign_open_order() -> None:
    """A resting order the gateway never placed halts reconciliation with
    reason `"foreign_open_order"`.
    """
    gateway = _FakeGateway(())
    foreign = OpenOrder(
        id="foreign-1",
        ticker=_TICKER,
        side="yes",
        price=PricePips(5000),
        quantity=ContractCentis(10),
    )
    source = _ScriptedSource((foreign,), ())
    writer = InMemoryKernelLedgerWriter()
    reconciler = Reconciler(
        gateway,
        ledger_reader=_EmptyLedgerReader(),
        reconciliation_source=source,
        ledger_writer=writer,
    )

    outcome = reconciler.run_once()

    assert outcome.halted is True
    assert outcome.halt_reason == "foreign_open_order"
    assert gateway.halted is True


# --- Recovery phase: a benign heal, and a fresh clean cycle --------------------


def test_a_benign_out_of_band_fill_heals_rather_than_halts() -> None:
    """A tracked order missing from the venue's open orders *with* a fully
    corroborating fill heals (a benign missed fill), never halting: exactly
    one `ReconciliationHealed` is ledgered and the tracked order is retired.
    """
    tracked = _tracked_order(filled_centis=0)
    gateway = _FakeGateway((tracked,))
    fill = Fill(
        id="fill-1",
        ticker=_TICKER,
        side="yes",
        price=PricePips(5000),
        quantity=ContractCentis(100),
        ts=_EPOCH,
    )
    source = _ScriptedSource((), (fill,))
    writer = InMemoryKernelLedgerWriter()
    reconciler = Reconciler(
        gateway,
        ledger_reader=_EmptyLedgerReader(),
        reconciliation_source=source,
        ledger_writer=writer,
    )

    outcome = reconciler.run_once()

    assert outcome.halted is False
    assert outcome.healed == 1
    healed = [e for e in writer.events if e.event_type == "ReconciliationHealed"]
    assert len(healed) == 1
    assert gateway.tracked_orders() == ()


def test_a_fresh_cycle_against_a_clean_venue_reconciles_without_halting() -> None:
    """A subsequent reconciliation cycle -- built fresh, simulating an
    operator restart after resolving the mismatch -- against a venue that now
    matches the Gateway's own (empty) tracked-order set reconciles cleanly.
    """
    gateway = _FakeGateway(())
    source = _ScriptedSource((), ())
    writer = InMemoryKernelLedgerWriter()
    reconciler = Reconciler(
        gateway,
        ledger_reader=_EmptyLedgerReader(),
        reconciliation_source=source,
        ledger_writer=writer,
    )

    outcome = reconciler.run_once()

    assert outcome.halted is False
    assert outcome.healed == 0
    assert gateway.halted is False


# --- Alert leg: a detected breach auto-kills and dispatches HALT_KILL ----------


def test_mismatch_monitor_kills_and_dispatches_halt_kill_at_threshold() -> None:
    """With `threshold=1`, the first `BREACH` outcome the monitor observes
    immediately kills the switch and dispatches exactly one `HALT_KILL`
    alert -- the drill's alerting leg for a detected reconciliation mismatch.
    """
    writer = InMemoryKernelLedgerWriter()
    sink = RecordingAlertSink()
    mode_machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE)
    switch = KillSwitch(mode_machine, writer, sink, clock=lambda: FIXED_EPOCH_S)
    monitor = ReconciliationMismatchMonitor(switch, threshold=1)

    monitor.observe(VerificationOutcome.BREACH)

    assert mode_machine.mode == Mode.KILLED
    assert sink.count(AlertType.HALT_KILL) == 1


# --- ReconciliationMismatchDrill: end-to-end via the assembled Drill -----------


def _build_ctx(tmp_path: Path) -> DrillContext:
    """Build a `DrillContext` for the reconciliation-mismatch drill."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=None,
        state_dir=state_dir,
        fixture_dir=tmp_path / "fixture",
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: state_dir,
    )


def test_reconciliation_mismatch_drill_passes_on_scripted_scenario(
    tmp_path: Path,
) -> None:
    """`ReconciliationMismatchDrill().run(ctx)` passes when its internally
    scripted mismatch-then-clean-cycle scenario behaves exactly per the
    contract pinned above: a halt on the first cycle, no halt on the second,
    and the monitor's `HALT_KILL` alert fires.
    """
    ctx = _build_ctx(tmp_path)
    drill = ReconciliationMismatchDrill()

    result = drill.run(ctx)

    assert result.passed is True
    assert result.drill == "reconciliation-mismatch"
    assert result.evidence["first_cycle_halted"] is True
    assert result.evidence["second_cycle_reconciled"] is True
    assert result.evidence["auto_killed"] is True
    assert result.evidence["halt_reason"]
    assert _as_int(result.evidence["halt_kill_alerts"]) >= 1


def _as_int(value: object) -> int:
    """Return ``value`` as an ``int``, asserting it already is one.

    Args:
        value: The evidence value expected to be an integer.

    Returns:
        The value narrowed to ``int``.
    """
    assert isinstance(value, int)
    return value


# --- Negative / fault-injection: FAILURE branches (issue #59 Gate 1 coverage) --


def test_drills_own_fake_gateway_retires_the_healed_order() -> None:
    """The drill module's *own* scripted `_FakeGateway.retire_tracked_order`
    removes exactly the healed order from the tracked set. Exercised via a
    corroborating-fill scenario that heals rather than halts -- the drill's
    `execute()` never drives this path itself (both its scripted cycles are
    scoped to halt/clean, never heal), so this is the only way to cover it.
    """
    tracked = recon_drill_module._tracked_order()
    gateway = recon_drill_module._FakeGateway((tracked,))
    fill = Fill(
        id="fill-1",
        ticker=_TICKER,
        side="yes",
        price=PricePips(5000),
        quantity=ContractCentis(100),
        ts=_EPOCH,
    )
    source = recon_drill_module._ScriptedSource((), (fill,))

    outcome = recon_drill_module._reconcile_once(gateway, source)

    assert outcome.halted is False
    assert gateway.tracked_orders() == ()


def test_grade_fails_when_the_halt_cycle_does_not_halt_for_the_expected_reason() -> (
    None
):
    """`_grade` raises `DrillFailedError` when the halt cycle either did not
    halt at all, or halted for a reason other than `vanished_order_no_fill`.
    """
    drill = ReconciliationMismatchDrill()
    halt = ReconcileOutcome(halted=False, healed=0, halt_reason=None)
    clean = ReconcileOutcome(halted=False, healed=0, halt_reason=None)

    with pytest.raises(DrillFailedError) as excinfo:
        drill._grade(halt, clean, killed=True, halt_kill_alerts=1)

    assert excinfo.value.evidence == {"halted": False, "halt_reason": None}


def test_grade_fails_when_the_clean_cycle_halts() -> None:
    """`_grade` raises `DrillFailedError` when the second (should-be-clean)
    cycle halts instead of reconciling.
    """
    drill = ReconciliationMismatchDrill()
    halt = ReconcileOutcome(halted=True, healed=0, halt_reason="vanished_order_no_fill")
    clean = ReconcileOutcome(halted=True, healed=0, halt_reason="foreign_open_order")

    with pytest.raises(DrillFailedError) as excinfo:
        drill._grade(halt, clean, killed=True, halt_kill_alerts=1)

    assert excinfo.value.evidence == {"clean_cycle_halted": True}


def test_grade_fails_when_the_alert_leg_does_not_kill_and_fire_exactly_one_alert() -> (
    None
):
    """`_grade` raises `DrillFailedError` when the alert leg either did not kill
    the switch, or did not fire exactly one `HALT_KILL` alert.
    """
    drill = ReconciliationMismatchDrill()
    halt = ReconcileOutcome(halted=True, healed=0, halt_reason="vanished_order_no_fill")
    clean = ReconcileOutcome(halted=False, healed=0, halt_reason=None)

    with pytest.raises(DrillFailedError) as excinfo:
        drill._grade(halt, clean, killed=False, halt_kill_alerts=0)

    assert excinfo.value.evidence == {"auto_killed": False, "halt_kill_alerts": 0}
