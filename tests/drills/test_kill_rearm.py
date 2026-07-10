"""Failing-first tests for the `kill-rearm` drill (issue #59, RED).

`windbreak.drills.kill_rearm` does not exist yet, so the import below fails
collection with `ModuleNotFoundError` -- the expected Gate 1 RED state for
issue #59.

This file pins two things:

    * The assembled invariant every trigger path (CLI, KILL-file, dashboard,
      auto-reconciliation) must satisfy when composed against a
      `HeldPositionsExchange`: positions held before == after, exactly one
      `CancelAllDirective(scope="all_open_orders")` fans out to the exchange
      (canceling every resting order and touching no position), a wrong
      re-arm phrase leaves the switch `KILLED`, and the correct phrase moves
      it to `PAUSED` -- with `KillEngaged`/`KillReArmed` ledgered exactly once
      each, and the `KILL`/`REARM` files lifecycle-managed on disk.
    * `KillRearmDrill` itself: a `Drill` that runs this composition
      end-to-end against an injected `DrillContext` and reports
      `passed=True` on a clean kill-then-rearm cycle.

Design assumption (flagged for the implementer): the drill fans a
`CancelAllDirective` out to `ctx.exchange` via a small adapter
(`_ExchangeDirectiveSink` below, mirrored inside the real drill module)
whose `.submit(directive)` cancels every currently open order on the
exchange -- `KillSwitch` itself only ever emits the one directive, per SPEC
S10.12; a downstream consumer (the real Order Gateway in production, this
adapter in the drill) is what actually cancels resting orders order-by-order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.drills.conftest import FIXED_EPOCH_S, InMemoryDrillLedgerWriter
from windbreak.connector.models import OpenOrder, Position
from windbreak.drills.context import DrillContext
from windbreak.drills.exchanges import HeldPositionsExchange
from windbreak.drills.framework import DrillFailedError, DrillPreconditionError
from windbreak.drills.kill_rearm import KillRearmDrill
from windbreak.numeric.types import ContractCentis, PricePips
from windbreak.riskkernel.kill import KillFileWatcher, KillSwitch, KillTrigger
from windbreak.riskkernel.modes import KillReArmError, Mode, ModeStateMachine
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter

if TYPE_CHECKING:
    from pathlib import Path


class _NoopAlertSink:
    """A minimal `KillSwitch` alert-dispatcher double that records nothing."""

    def dispatch(self, alert_type: object, message: str) -> None:
        """Accept and discard every dispatched alert."""


class _ExchangeDirectiveSink:
    """Fans a `CancelAllDirective` out to every open order on an exchange.

    Mirrors the adapter the real `kill_rearm` drill module composes:
    `KillSwitch` itself only ever emits the one directive (SPEC S10.12); this
    is what actually cancels resting orders, order by order, leaving
    positions untouched.
    """

    def __init__(self, exchange: HeldPositionsExchange) -> None:
        """Wire the sink to the exchange it cancels orders against."""
        self._exchange = exchange
        self.received: list[object] = []

    def submit(self, directive: object) -> None:
        """Record the directive, then cancel every currently open order."""
        self.received.append(directive)
        for order in self._exchange.get_open_orders():
            self._exchange.cancel_order(order.id)


def _seeded_exchange() -> HeldPositionsExchange:
    """Build a `HeldPositionsExchange` with two open orders and one position."""
    orders = (
        OpenOrder(
            id="order-1",
            ticker="PRES-2028-DEM",
            side="yes",
            price=PricePips(5000),
            quantity=ContractCentis(100),
        ),
        OpenOrder(
            id="order-2",
            ticker="PRES-2028-DEM",
            side="no",
            price=PricePips(4800),
            quantity=ContractCentis(50),
        ),
    )
    positions = (
        Position(
            ticker="PRES-2028-DEM",
            quantity=ContractCentis(500),
            average_price=PricePips(5000),
        ),
    )
    return HeldPositionsExchange(open_orders=orders, positions=positions)


def _build_switch(
    exchange: HeldPositionsExchange, tmp_path: Path
) -> tuple[
    KillSwitch, InMemoryKernelLedgerWriter, ModeStateMachine, _ExchangeDirectiveSink
]:
    """Build a `KillSwitch` whose directive sink cancels `exchange`'s orders."""
    writer = InMemoryKernelLedgerWriter()
    directive_sink = _ExchangeDirectiveSink(exchange)
    mode_machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE)
    switch = KillSwitch(
        mode_machine,
        writer,
        _NoopAlertSink(),
        directive_sink=directive_sink,
        state_dir=tmp_path,
        clock=lambda: FIXED_EPOCH_S,
    )
    return switch, writer, mode_machine, directive_sink


# --- Every trigger holds positions and cancels every open order ----------------


@pytest.mark.parametrize("trigger", list(KillTrigger))
def test_every_trigger_holds_positions_and_cancels_every_open_order(
    trigger: KillTrigger, tmp_path: Path
) -> None:
    """Regardless of trigger, killing against a `HeldPositionsExchange`
    cancels every open order and leaves positions exactly as they were.
    """
    exchange = _seeded_exchange()
    positions_before = exchange.get_positions()
    switch, writer, _machine, directive_sink = _build_switch(exchange, tmp_path)

    switch.kill(trigger)

    assert exchange.get_open_orders() == ()
    assert exchange.get_positions() == positions_before
    assert len(directive_sink.received) == 1
    assert directive_sink.received[0].payload["scope"] == "all_open_orders"
    kill_events = [e for e in writer.events if e.event_type == "KillEngaged"]
    assert len(kill_events) == 1
    assert kill_events[0].payload["trigger"] == trigger.name


def test_kill_file_trigger_via_a_real_kill_file_holds_positions_too(
    tmp_path: Path,
) -> None:
    """The `KILL`-file trigger path, driven through a real `KillFileWatcher`
    poll (not `switch.kill()` called directly), holds positions the same way
    every other trigger does.
    """
    exchange = _seeded_exchange()
    positions_before = exchange.get_positions()
    switch, _writer, machine, _sink = _build_switch(exchange, tmp_path)
    watcher = KillFileWatcher(switch, tmp_path)
    (tmp_path / "KILL").write_text("", encoding="utf-8")

    watcher.poll_once(now_epoch_s=FIXED_EPOCH_S)

    assert machine.mode == Mode.KILLED
    assert exchange.get_open_orders() == ()
    assert exchange.get_positions() == positions_before


# --- Re-arm: wrong phrase stays KILLED, correct phrase moves to PAUSED --------


def test_wrong_rearm_phrase_leaves_the_switch_killed(tmp_path: Path) -> None:
    """A wrong re-arm phrase after a kill leaves the switch `KILLED`."""
    exchange = _seeded_exchange()
    switch, _writer, machine, _sink = _build_switch(exchange, tmp_path)
    switch.kill(KillTrigger.CLI)

    with pytest.raises(KillReArmError):
        switch.rearm("not the correct phrase")

    assert machine.mode == Mode.KILLED


def test_correct_rearm_phrase_moves_to_paused_and_ledgers_both_events(
    tmp_path: Path,
) -> None:
    """The correct re-arm phrase moves `KILLED` -> `PAUSED`, with
    `KillEngaged` and `KillReArmed` each ledgered exactly once.
    """
    exchange = _seeded_exchange()
    switch, writer, machine, _sink = _build_switch(exchange, tmp_path)
    switch.kill(KillTrigger.CLI)
    phrase = switch.expected_rearm_phrase(switch.active_kill_sequence)

    switch.rearm(phrase)

    assert machine.mode == Mode.PAUSED
    assert len([e for e in writer.events if e.event_type == "KillEngaged"]) == 1
    assert len([e for e in writer.events if e.event_type == "KillReArmed"]) == 1


def test_kill_file_lifecycle_writes_then_removes_the_kill_file_on_rearm(
    tmp_path: Path,
) -> None:
    """A `KILL` file is written on kill and removed (alongside a consumed
    `REARM` file) once the correct phrase re-arms the switch.
    """
    exchange = _seeded_exchange()
    switch, _writer, machine, _sink = _build_switch(exchange, tmp_path)
    watcher = KillFileWatcher(switch, tmp_path)
    switch.kill(KillTrigger.CLI)
    assert (tmp_path / "KILL").exists()
    phrase = switch.expected_rearm_phrase(switch.active_kill_sequence)
    (tmp_path / "REARM").write_text(phrase, encoding="utf-8")

    watcher.poll_once(now_epoch_s=FIXED_EPOCH_S + 1)

    assert machine.mode == Mode.PAUSED
    assert not (tmp_path / "KILL").exists()
    assert not (tmp_path / "REARM").exists()


# --- KillRearmDrill: end-to-end via the assembled Drill -------------------------


def _build_kill_rearm_ctx(tmp_path: Path) -> DrillContext:
    """Build a `DrillContext` whose `.exchange` is a seeded
    `HeldPositionsExchange`."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=_seeded_exchange(),
        state_dir=state_dir,
        fixture_dir=tmp_path / "fixture",
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: state_dir,
    )


def test_kill_rearm_drill_passes_on_a_clean_kill_then_rearm_cycle(
    tmp_path: Path,
) -> None:
    """`KillRearmDrill().run(ctx)` passes when the kill-then-rearm cycle
    completes cleanly: positions held, one cancel-all directive, and a
    successful re-arm.
    """
    ctx = _build_kill_rearm_ctx(tmp_path)
    drill = KillRearmDrill()

    result = drill.run(ctx)

    assert result.passed is True
    assert result.drill == "kill-rearm"
    assert result.evidence == {
        "positions_held": True,
        "open_orders_cancelled": 2,  # the two seeded open orders
        "cancel_all_directives": 1,
        "rearmed": True,
        "final_mode": "PAUSED",
    }


def test_kill_rearm_drill_writes_its_kill_file_under_the_drill_owned_temp_dir(
    tmp_path: Path,
) -> None:
    """The drill's `KillSwitch` is rooted at `ctx.tmp_dir_factory()`, never at
    `ctx.state_dir`, so a `KILL` file lands in a drill-owned scratch directory
    and the operator-supplied `state_dir` is left untouched.

    This is the structural guarantee that `windbreak drill kill-rearm
    --state-dir <live-ops-dir>` cannot write (and instant-rearm) a genuine
    `KILL` protocol file against a running system: the drill is incapable of
    touching `state_dir` by construction, not by convention.
    """
    state_dir = tmp_path / "live-ops-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    scratch = tmp_path / "drill-owned-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    ctx = DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=_seeded_exchange(),
        state_dir=state_dir,
        fixture_dir=tmp_path / "fixture",
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: scratch,
    )

    result = KillRearmDrill().run(ctx)

    assert result.passed is True
    assert not (state_dir / "KILL").exists()
    assert (scratch / "KILL").exists()


# --- Negative / fault-injection: FAILURE branches (issue #59 Gate 1 coverage) --


def test_kill_rearm_precondition_raises_without_a_held_positions_exchange(
    tmp_path: Path,
) -> None:
    """`check_preconditions` raises `DrillPreconditionError` when
    `ctx.exchange` is not a `HeldPositionsExchange` -- e.g. `None`.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ctx = DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=None,
        state_dir=state_dir,
        fixture_dir=tmp_path / "fixture",
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: state_dir,
    )
    drill = KillRearmDrill()

    with pytest.raises(DrillPreconditionError):
        drill.check_preconditions(ctx)


class _NoCancelExchange(HeldPositionsExchange):
    """A `HeldPositionsExchange` double whose `cancel_order` never cancels.

    Models a broken exchange adapter: the kill phase's cancel-all sink still
    calls `cancel_order` once per resting order, but this double silently
    ignores it, so an order remains resting after a kill.
    """

    def cancel_order(self, order_id: str) -> None:
        """Accept the id and do nothing (the bug under test)."""
        del order_id


class _PositionMutatingExchange(HeldPositionsExchange):
    """A `HeldPositionsExchange` double whose `cancel_order` also mutates
    held positions.

    Models a broken exchange adapter that violates the position-hold
    invariant a kill must never break.
    """

    def cancel_order(self, order_id: str) -> None:
        """Cancel normally, then (the bug under test) clear held positions."""
        super().cancel_order(order_id)
        self._positions = ()


def _build_ctx_with_exchange(tmp_path: Path, exchange: object) -> DrillContext:
    """Build a `DrillContext` carrying `exchange` as its exchange adapter."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=exchange,
        state_dir=state_dir,
        fixture_dir=tmp_path / "fixture",
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: state_dir,
    )


def test_kill_rearm_drill_fails_when_cancel_order_leaves_an_order_resting(
    tmp_path: Path,
) -> None:
    """A broken exchange whose `cancel_order` never actually cancels leaves
    an order resting after the kill; the drill grades `passed=False` rather
    than silently reporting every order cancelled.
    """
    order = OpenOrder(
        id="order-1",
        ticker="PRES-2028-DEM",
        side="yes",
        price=PricePips(5000),
        quantity=ContractCentis(100),
    )
    exchange = _NoCancelExchange(open_orders=(order,), positions=())
    ctx = _build_ctx_with_exchange(tmp_path, exchange)
    drill = KillRearmDrill()

    result = drill.run(ctx)

    assert result.passed is False
    assert result.evidence == {"open_orders_after_kill": "not_empty"}


def test_kill_rearm_drill_fails_when_cancel_order_mutates_positions(
    tmp_path: Path,
) -> None:
    """A broken exchange whose `cancel_order` also mutates held positions
    breaks the position-hold invariant; the drill grades `passed=False`
    rather than silently reporting positions held.
    """
    order = OpenOrder(
        id="order-1",
        ticker="PRES-2028-DEM",
        side="yes",
        price=PricePips(5000),
        quantity=ContractCentis(100),
    )
    position = Position(
        ticker="PRES-2028-DEM",
        quantity=ContractCentis(500),
        average_price=PricePips(5000),
    )
    exchange = _PositionMutatingExchange(open_orders=(order,), positions=(position,))
    ctx = _build_ctx_with_exchange(tmp_path, exchange)
    drill = KillRearmDrill()

    result = drill.run(ctx)

    assert result.passed is False
    assert result.evidence == {"positions_changed": True}


class _DroppingDirectiveSink:
    """A directive-sink double that accepts submissions but never records any.

    Trips `KillRearmDrill`'s own cancel-all-directive-count self-check: the
    real `KillSwitch.kill()` still calls `.submit()` exactly once, but this
    broken sink drops it.
    """

    def __init__(self) -> None:
        """Initialize with an (always-empty) received-directives log."""
        self.received: list[object] = []

    def submit(self, directive: object) -> None:
        """Accept and discard the directive (the bug under test)."""
        del directive


class _CountingDirectiveSink:
    """A directive-sink double recording every submission, correctly."""

    def __init__(self) -> None:
        """Initialize with an empty received-directives log."""
        self.received: list[object] = []

    def submit(self, directive: object) -> None:
        """Record the submitted directive."""
        self.received.append(directive)


class _DroppingLedgerWriter:
    """A ledger-writer double that accepts records but never retains any."""

    def __init__(self) -> None:
        """Initialize with an (always-empty) event log."""
        self.events: list[object] = []

    def record(self, event: object) -> None:
        """Accept and discard the event (the bug under test)."""
        del event


class _NoopAlertSinkDouble:
    """A minimal `KillSwitch` alert-dispatcher double that records nothing."""

    def dispatch(self, alert_type: object, message: str) -> None:
        """Accept and discard every dispatched alert."""
        del alert_type, message


def test_run_kill_phase_fails_when_the_directive_sink_drops_the_directive() -> None:
    """`_run_kill_phase` raises `DrillFailedError` when the wired directive sink
    does not end up holding exactly one received directive -- here, a broken
    sink that drops it, simulating a downstream consumer that never actually
    receives the cancel-all fan-out. `KillSwitch`/exchange are otherwise
    healthy, so only this self-check line is exercised.
    """
    exchange = HeldPositionsExchange(open_orders=(), positions=())
    writer = InMemoryKernelLedgerWriter()
    sink = _DroppingDirectiveSink()
    switch = KillSwitch(
        ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE),
        writer,
        _NoopAlertSinkDouble(),
        directive_sink=sink,
        clock=lambda: FIXED_EPOCH_S,
    )
    drill = KillRearmDrill()

    with pytest.raises(DrillFailedError) as excinfo:
        drill._run_kill_phase(switch, exchange, exchange.get_positions(), writer, sink)

    assert excinfo.value.evidence == {"cancel_all_directives": 0}


def test_run_kill_phase_fails_when_the_ledger_writer_drops_kill_engaged() -> None:
    """`_run_kill_phase` raises `DrillFailedError` when the wired ledger writer
    does not end up holding exactly one `KillEngaged` event -- here, a broken
    writer that drops every record, even though the directive sink itself
    behaves correctly.
    """
    exchange = HeldPositionsExchange(open_orders=(), positions=())
    writer = _DroppingLedgerWriter()
    sink = _CountingDirectiveSink()
    switch = KillSwitch(
        ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE),
        writer,
        _NoopAlertSinkDouble(),
        directive_sink=sink,
        clock=lambda: FIXED_EPOCH_S,
    )
    drill = KillRearmDrill()

    with pytest.raises(DrillFailedError) as excinfo:
        drill._run_kill_phase(switch, exchange, exchange.get_positions(), writer, sink)

    assert excinfo.value.evidence == {"kill_engaged_events": 0}


class _StuckModeSwitch:
    """A `KillSwitch` double whose `rearm` accepts the phrase but never moves
    the mode off `KILLED`.

    Models a regression in the re-arm implementation: the real `KillSwitch`
    always transitions `KILLED` -> `PAUSED` on the correct phrase, but this
    double simulates that transition silently failing to take effect.
    """

    def __init__(self) -> None:
        """Initialize stuck in `KILLED` with kill sequence 1."""
        self.active_kill_sequence = 1
        self.mode = Mode.KILLED

    def expected_rearm_phrase(self, kill_sequence: int) -> str:
        """Return a placeholder phrase (never actually checked here)."""
        return f"phrase-{kill_sequence}"

    def rearm(self, confirmation: str) -> None:
        """Accept the confirmation but (the bug under test) never re-arm."""
        del confirmation


def test_run_rearm_phase_fails_when_the_mode_never_leaves_killed() -> None:
    """`_run_rearm_phase` raises `DrillFailedError` when `switch.mode` is not
    `PAUSED` after a `rearm` call -- here, a broken switch double that
    accepts the confirmation but never actually re-arms.
    """
    switch = _StuckModeSwitch()
    writer = InMemoryKernelLedgerWriter()
    drill = KillRearmDrill()

    with pytest.raises(DrillFailedError) as excinfo:
        drill._run_rearm_phase(switch, writer)

    assert excinfo.value.evidence == {"mode_after_rearm": "KILLED"}


def test_run_rearm_phase_fails_when_the_ledger_writer_drops_kill_rearmed() -> None:
    """`_run_rearm_phase` raises `DrillFailedError` when the wired ledger writer
    does not end up holding exactly one `KillReArmed` event -- here, a broken
    writer that drops every record, even though the real switch itself
    re-arms correctly (mode does reach `PAUSED`).
    """
    writer = _DroppingLedgerWriter()
    switch = KillSwitch(
        ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE),
        writer,
        _NoopAlertSinkDouble(),
        clock=lambda: FIXED_EPOCH_S,
    )
    switch.kill(KillTrigger.CLI)
    drill = KillRearmDrill()

    with pytest.raises(DrillFailedError) as excinfo:
        drill._run_rearm_phase(switch, writer)

    assert excinfo.value.evidence == {"kill_rearmed_events": 0}
