"""Failing-first tests for the `ratchet-sweep` drill (issue #59, RED).

`windbreak.drills.ratchet_sweep` does not exist yet, so the import below
fails collection with `ModuleNotFoundError` -- the expected Gate 1 RED state
for issue #59.

Composes three already-shipped mechanisms into one "ratchet sweep" scenario:
`windbreak.riskkernel.governance.FloorGovernance.observe_equity`'s exact
integer-ppm ratchet math and its `PROFIT_SWEEP_ADVISORY` alert,
`windbreak.net.allowlist.OutboundAllowlist`'s fail-closed rejection of a
withdrawal-shaped URL, and `HeldPositionsExchange`'s structural absence of
any fund-movement method -- "cannot move funds" is audited from two
independent angles (network egress, exchange surface), never just one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.drills.conftest import FIXED_EPOCH_S, InMemoryDrillLedgerWriter
from windbreak.alerts import AlertDispatcher, AlertType, LoggingLedgerWriter
from windbreak.drills import ratchet_sweep
from windbreak.drills.context import DrillContext
from windbreak.drills.exchanges import HeldPositionsExchange
from windbreak.drills.framework import DrillFailedError, DrillPreconditionError
from windbreak.drills.ratchet_sweep import RatchetSweepDrill
from windbreak.net.allowlist import EgressDeniedError, OutboundAllowlist
from windbreak.numeric.types import MoneyMicros
from windbreak.riskkernel.governance import FloorGovernance
from windbreak.riskkernel.modes import Mode, ModeStateMachine
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter

if TYPE_CHECKING:
    from pathlib import Path


class _RecordingSink:
    """A spy `AlertSink` recording every dispatched alert type."""

    name = "spy"

    def __init__(self) -> None:
        """Initialize with an empty dispatch log."""
        self.calls: list[object] = []

    def send(self, alert_type: object, severity: object, message: str) -> None:
        """Record the dispatched alert type, discarding severity/message."""
        del severity, message
        self.calls.append(alert_type)


def _build_governance(
    *, ratchet_ppm: int, profit_sweep_threshold: MoneyMicros, sink: _RecordingSink
) -> FloorGovernance:
    """Build a `FloorGovernance` wired to `sink`, mirroring
    `tests/riskkernel/test_governance.py`'s own construction recipe.
    """
    dispatcher = AlertDispatcher([sink], ledger_writer=LoggingLedgerWriter())
    mode_machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE)
    writer = InMemoryKernelLedgerWriter()
    return FloorGovernance(
        initial_floor=MoneyMicros(0),
        ratchet_ppm=ratchet_ppm,
        profit_sweep_threshold=profit_sweep_threshold,
        mode_machine=mode_machine,
        dispatcher=dispatcher,
        writer=writer,
        clock=lambda: FIXED_EPOCH_S,
    )


# --- Integer ppm math: the ratchet increment is exact, never float-derived ----


@pytest.mark.parametrize(
    ("gain", "ppm", "expected_increment"),
    [
        (1_000_001, 500_000, 500_000),
        (999_999, 500_000, 499_999),
        (7, 1, 0),
        (1_000_000, 1_000_000, 1_000_000),
    ],
)
def test_ratchet_increment_matches_the_same_integer_floor_division(
    gain: int, ppm: int, expected_increment: int
) -> None:
    """`FloorGovernance.observe_equity`'s floor increment equals
    `gain * ppm // 1_000_000` computed independently here with the same
    integer math -- never a float approximation.
    """
    governance = _build_governance(
        ratchet_ppm=ppm,
        profit_sweep_threshold=MoneyMicros(10**18),
        sink=_RecordingSink(),
    )

    governance.observe_equity(MoneyMicros(gain))

    assert governance.current_floor_micros == MoneyMicros(expected_increment)
    assert expected_increment == gain * ppm // 1_000_000


# --- Strictly-above-HWM edge: an equal observation never re-ratchets ----------


def test_observing_equity_exactly_at_the_high_water_mark_is_a_no_op() -> None:
    """A second observation exactly at (not above) the current high-water
    mark ratchets nothing further and fires no second advisory.
    """
    sink = _RecordingSink()
    governance = _build_governance(
        ratchet_ppm=500_000, profit_sweep_threshold=MoneyMicros(1), sink=sink
    )
    governance.observe_equity(MoneyMicros(2_000_000))
    floor_after_first = governance.current_floor_micros
    advisories_after_first = sink.calls.count(AlertType.PROFIT_SWEEP_ADVISORY)

    governance.observe_equity(MoneyMicros(2_000_000))

    assert governance.current_floor_micros == floor_after_first
    assert sink.calls.count(AlertType.PROFIT_SWEEP_ADVISORY) == advisories_after_first


# --- PROFIT_SWEEP_ADVISORY is captured by the alert sink -----------------------


def test_profit_sweep_advisory_is_captured_when_gain_exceeds_the_threshold() -> None:
    """A gain strictly above the sweep threshold fires exactly one
    `PROFIT_SWEEP_ADVISORY`, captured by the recording sink.
    """
    sink = _RecordingSink()
    governance = _build_governance(
        ratchet_ppm=0, profit_sweep_threshold=MoneyMicros(1_000_000), sink=sink
    )

    governance.observe_equity(MoneyMicros(2_000_000))

    assert sink.calls.count(AlertType.PROFIT_SWEEP_ADVISORY) == 1


# --- "Cannot move funds": allowlist rejection plus a structural audit ---------


def test_outbound_allowlist_rejects_a_withdrawal_url() -> None:
    """`OutboundAllowlist.require` raises fail-closed on a withdrawal-shaped
    URL against a host never on the allowlist.
    """
    allowlist = OutboundAllowlist(frozenset({"api.example.com"}))

    with pytest.raises(EgressDeniedError):
        allowlist.require("https://withdraw.example.com/v1/withdraw")


def test_held_positions_exchange_has_no_fund_movement_method() -> None:
    """The exchange the ratchet-sweep drill audits exposes no
    `withdraw`/`transfer`/`move_funds` method at all.
    """
    exchange = HeldPositionsExchange(open_orders=(), positions=())

    assert not hasattr(exchange, "withdraw")
    assert not hasattr(exchange, "transfer")
    assert not hasattr(exchange, "move_funds")


# --- RatchetSweepDrill: end-to-end via the assembled Drill --------------------


def _build_ctx(tmp_path: Path) -> DrillContext:
    """Build a `DrillContext` for the ratchet-sweep drill."""
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=HeldPositionsExchange(open_orders=(), positions=()),
        state_dir=state_dir,
        fixture_dir=fixture_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: state_dir,
    )


def test_ratchet_sweep_drill_passes_end_to_end(tmp_path: Path) -> None:
    """`RatchetSweepDrill().run(ctx)` passes: the ratchet math is exact, the
    advisory fires, and the no-withdrawal audit holds.
    """
    ctx = _build_ctx(tmp_path)
    drill = RatchetSweepDrill()

    result = drill.run(ctx)

    assert result.passed is True
    assert result.drill == "ratchet-sweep"
    increment = result.evidence["floor_increment_micros"]
    assert increment == result.evidence["expected_increment_micros"]
    assert isinstance(increment, int)
    assert increment > 0
    assert result.evidence["advisory_fired"] is True
    assert result.evidence["withdrawal_denied"] is True
    assert result.evidence["exchange_has_no_fund_movement"] is True


# --- Negative / fault-injection: FAILURE branches (issue #59 Gate 1 coverage) --


def test_ratchet_sweep_precondition_raises_without_a_held_positions_exchange(
    tmp_path: Path,
) -> None:
    """`check_preconditions` raises `DrillPreconditionError` when
    `ctx.exchange` is not a `HeldPositionsExchange` -- e.g. `None`.
    """
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=None,
        state_dir=state_dir,
        fixture_dir=fixture_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: state_dir,
    )
    drill = RatchetSweepDrill()

    with pytest.raises(DrillPreconditionError):
        drill.check_preconditions(ctx)


class _WithdrawableExchange(HeldPositionsExchange):
    """A `HeldPositionsExchange` double that (incorrectly) exposes `withdraw`.

    Models a broken exchange adapter that regresses the "cannot move funds"
    structural invariant `RatchetSweepDrill` audits.
    """

    def withdraw(self, amount: int) -> None:
        """Accept a withdrawal amount (never actually called by the drill;
        its mere presence is what the structural audit catches).
        """
        del amount


def test_ratchet_sweep_drill_fails_when_the_exchange_exposes_a_withdraw_method(
    tmp_path: Path,
) -> None:
    """A broken exchange double exposing `withdraw` fails the structural
    no-fund-movement audit, and the drill grades `passed=False` rather than
    silently passing a regressed exchange surface.
    """
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=_WithdrawableExchange(open_orders=(), positions=()),
        state_dir=state_dir,
        fixture_dir=fixture_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=lambda: state_dir,
    )
    drill = RatchetSweepDrill()

    result = drill.run(ctx)

    assert result.passed is False
    assert result.evidence["exchange_has_no_fund_movement"] is False


def test_withdrawal_denied_returns_false_when_the_allowlist_admits_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the outbound-allowlist collaborator is broken and never denies
    (a fault-injected permissive allowlist), `_withdrawal_denied` reports
    `False` -- the audit's own failure signal, never a silent pass.
    """

    class _PermissiveAllowlist:
        """An `OutboundAllowlist` double that admits every URL (broken)."""

        def __init__(self, hosts: object) -> None:
            """Accept and discard the allowlisted hosts."""
            del hosts

        def require(self, url: str) -> None:
            """Accept every URL: never denies (the bug under test)."""
            del url

    monkeypatch.setattr(ratchet_sweep, "OutboundAllowlist", _PermissiveAllowlist)
    drill = RatchetSweepDrill()

    assert drill._withdrawal_denied() is False


def test_grade_fails_when_the_ratchet_delta_does_not_match_expected() -> None:
    """`_grade` raises `DrillFailedError` carrying both the actual and expected
    increment when the ratchet's floor delta diverges from the independently
    computed integer-ppm expectation.
    """
    drill = RatchetSweepDrill()

    with pytest.raises(DrillFailedError) as excinfo:
        drill._grade(
            floor_after=100,
            expected=200,
            advisory_fired=True,
            withdrawal_denied=True,
            no_fund_movement=True,
        )

    assert excinfo.value.evidence == {
        "floor_increment_micros": 100,
        "expected": 200,
    }
