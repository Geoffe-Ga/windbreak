"""Failing-first tests for hedgekit.riskkernel.demotion (issue #33, RED).

Issue #33 gives the Risk Kernel its demotion-trigger machinery: 16 named
`DemotionTrigger`s, each mapped to exactly one of four `DemotionAction`s
(`PAUSE`/`DEMOTE_ONE_MODE`/`HALT`/`KILL`), resolved against the current `Mode`
by the pure `resolve_demotion` function, plus the kernel-level
`fire_demotion_trigger` entrypoint that ledgers exactly one
`DemotionTriggerFired` event per firing (including no-op firings) and performs
the transition when one exists.

Neither `hedgekit/riskkernel/demotion.py` nor the new `hedgekit.ledger.events`
class it needs (`DemotionTriggerFired`) exist yet, so this file fails
collection in two stages depending on which is fixed first: today, the
`from hedgekit.ledger.events import ... DemotionTriggerFired ...` line raises
`ImportError: cannot import name 'DemotionTriggerFired' from
'hedgekit.ledger.events'` (that module exists but does not yet define the
class); once `events.py` is extended, the next import,
`from hedgekit.riskkernel.demotion import ...`, would raise
`ModuleNotFoundError: No module named 'hedgekit.riskkernel.demotion'`. Either
way this is the expected Gate 1 RED state for issue #33.

ASSUMPTION this file pins (the architecture plan describes the resolution
*rules* in prose, not a literal 112-cell table): the full (mode, trigger)
matrix is derived here from two small, independently-verifiable building
blocks rather than by re-deriving the production algorithm -- (1)
`TRIGGER_ACTIONS`, the exact 16-entry table given verbatim in the plan, and
(2) `_ACTION_MODE_EXPECTATION`, a 4-action x 7-mode table restating the plan's
plain-English resolution rules (PAUSE/HALT/KILL target their safety mode
unless already there -- an idempotent no-op; DEMOTE_ONE_MODE steps down one
ladder rung, floors at PAUSED from RESEARCH, and is a no-op off-ladder; KILLED
is a dead end for every action). If `resolve_demotion` disagrees with this
table, either the implementation or this pinned table is wrong and the two
must be reconciled with the architect -- not silently patched to match
whichever came first.
"""

from __future__ import annotations

import itertools
import json

import pytest

from hedgekit.ledger.events import EVENT_TYPES, DemotionTriggerFired, canonical_json
from hedgekit.riskkernel.demotion import (
    TRIGGER_ACTIONS,
    DemotionAction,
    DemotionTrigger,
    resolve_demotion,
)
from hedgekit.riskkernel.modes import Mode, ModeStateMachine
from hedgekit.riskkernel.process import InMemoryKernelLedgerWriter, RiskKernel

#: The exact 16 `DemotionTrigger` members, verbatim from the architecture plan.
_EXPECTED_TRIGGER_NAMES = frozenset(
    {
        "DAILY_LOSS_BREACH",
        "DRAWDOWN_BREACH",
        "BALANCE_POSITION_MISMATCH",
        "FLOOR_CHECK_FAILURE",
        "SCHEMA_ANOMALY",
        "JURISDICTION_UNKNOWN",
        "ROLLING_BRIER_DEGRADATION",
        "LIVE_PAPER_SLIPPAGE_DIVERGENCE",
        "CLOCK_SKEW",
        "STALE_HEARTBEAT",
        "FEE_MODEL_UNAVAILABLE",
        "CANARY_DRIFT_UNACKNOWLEDGED",
        "TOKEN_REPLAY_ATTEMPT",
        "BACKUP_FAILURES_BEYOND_LIMIT",
        "DISK_BELOW_THRESHOLD",
        "MANUAL_KILL",
    }
)

#: The pinned trigger -> action table, verbatim from the architecture plan.
_EXPECTED_TRIGGER_ACTIONS: dict[str, DemotionAction] = {
    "DAILY_LOSS_BREACH": DemotionAction.PAUSE,
    "DRAWDOWN_BREACH": DemotionAction.DEMOTE_ONE_MODE,
    "ROLLING_BRIER_DEGRADATION": DemotionAction.DEMOTE_ONE_MODE,
    "LIVE_PAPER_SLIPPAGE_DIVERGENCE": DemotionAction.DEMOTE_ONE_MODE,
    "CANARY_DRIFT_UNACKNOWLEDGED": DemotionAction.DEMOTE_ONE_MODE,
    "BALANCE_POSITION_MISMATCH": DemotionAction.HALT,
    "FLOOR_CHECK_FAILURE": DemotionAction.HALT,
    "SCHEMA_ANOMALY": DemotionAction.HALT,
    "JURISDICTION_UNKNOWN": DemotionAction.HALT,
    "CLOCK_SKEW": DemotionAction.HALT,
    "STALE_HEARTBEAT": DemotionAction.HALT,
    "FEE_MODEL_UNAVAILABLE": DemotionAction.HALT,
    "TOKEN_REPLAY_ATTEMPT": DemotionAction.HALT,
    "BACKUP_FAILURES_BEYOND_LIMIT": DemotionAction.HALT,
    "DISK_BELOW_THRESHOLD": DemotionAction.HALT,
    "MANUAL_KILL": DemotionAction.KILL,
}

#: `_ACTION_MODE_EXPECTATION[(action, mode)]` -> the resolved destination
#: `Mode`, or `None` for a no-op. This is the plan's plain-English resolution
#: rule set, restated as data (see the module docstring's ASSUMPTION).
_ACTION_MODE_EXPECTATION: dict[tuple[DemotionAction, Mode], Mode | None] = {
    # PAUSE always targets PAUSED, except the idempotent same-mode no-op, and
    # KILLED (a dead end for every action).
    (DemotionAction.PAUSE, Mode.RESEARCH): Mode.PAUSED,
    (DemotionAction.PAUSE, Mode.PAPER): Mode.PAUSED,
    (DemotionAction.PAUSE, Mode.LIVE_MICRO): Mode.PAUSED,
    (DemotionAction.PAUSE, Mode.LIVE): Mode.PAUSED,
    (DemotionAction.PAUSE, Mode.PAUSED): None,
    (DemotionAction.PAUSE, Mode.HALT): Mode.PAUSED,
    (DemotionAction.PAUSE, Mode.KILLED): None,
    # HALT always targets HALT, except the idempotent same-mode no-op, and
    # KILLED.
    (DemotionAction.HALT, Mode.RESEARCH): Mode.HALT,
    (DemotionAction.HALT, Mode.PAPER): Mode.HALT,
    (DemotionAction.HALT, Mode.LIVE_MICRO): Mode.HALT,
    (DemotionAction.HALT, Mode.LIVE): Mode.HALT,
    (DemotionAction.HALT, Mode.PAUSED): Mode.HALT,
    (DemotionAction.HALT, Mode.HALT): None,
    (DemotionAction.HALT, Mode.KILLED): None,
    # KILL always targets KILLED, except the idempotent no-op from KILLED
    # itself (dead end).
    (DemotionAction.KILL, Mode.RESEARCH): Mode.KILLED,
    (DemotionAction.KILL, Mode.PAPER): Mode.KILLED,
    (DemotionAction.KILL, Mode.LIVE_MICRO): Mode.KILLED,
    (DemotionAction.KILL, Mode.LIVE): Mode.KILLED,
    (DemotionAction.KILL, Mode.PAUSED): Mode.KILLED,
    (DemotionAction.KILL, Mode.HALT): Mode.KILLED,
    (DemotionAction.KILL, Mode.KILLED): None,
    # DEMOTE_ONE_MODE steps down one ladder rung; RESEARCH floors at PAUSED
    # (fail-safe, nothing below the bottom rung); off-ladder (PAUSED/HALT) and
    # KILLED are no-ops.
    (DemotionAction.DEMOTE_ONE_MODE, Mode.LIVE): Mode.LIVE_MICRO,
    (DemotionAction.DEMOTE_ONE_MODE, Mode.LIVE_MICRO): Mode.PAPER,
    (DemotionAction.DEMOTE_ONE_MODE, Mode.PAPER): Mode.RESEARCH,
    (DemotionAction.DEMOTE_ONE_MODE, Mode.RESEARCH): Mode.PAUSED,
    (DemotionAction.DEMOTE_ONE_MODE, Mode.PAUSED): None,
    (DemotionAction.DEMOTE_ONE_MODE, Mode.HALT): None,
    (DemotionAction.DEMOTE_ONE_MODE, Mode.KILLED): None,
}


def _expected_resolution(mode: Mode, trigger: DemotionTrigger) -> Mode | None:
    """Look up the pinned expected `resolve_demotion(mode, trigger)` result.

    Args:
        mode: The current operating mode.
        trigger: The firing trigger.

    Returns:
        The expected destination mode, or `None` for a no-op.
    """
    action = _EXPECTED_TRIGGER_ACTIONS[trigger.name]
    return _ACTION_MODE_EXPECTATION[(action, mode)]


# --- Registry exhaustiveness -----------------------------------------------------


def test_demotion_trigger_has_exactly_the_sixteen_spec_members() -> None:
    """`DemotionTrigger` has exactly the 16 named members, no more, no fewer."""
    assert {member.name for member in DemotionTrigger} == _EXPECTED_TRIGGER_NAMES
    assert len(DemotionTrigger) == 16


def test_demotion_action_has_exactly_the_four_spec_members() -> None:
    """`DemotionAction` has exactly PAUSE/DEMOTE_ONE_MODE/HALT/KILL."""
    assert {member.name for member in DemotionAction} == {
        "PAUSE",
        "DEMOTE_ONE_MODE",
        "HALT",
        "KILL",
    }
    assert len(DemotionAction) == 4


def test_trigger_actions_registry_is_exhaustive_over_every_trigger() -> None:
    """`TRIGGER_ACTIONS` maps every one of the 16 triggers, none omitted."""
    assert set(TRIGGER_ACTIONS) == set(DemotionTrigger)
    assert len(TRIGGER_ACTIONS) == 16


def test_trigger_actions_registry_matches_the_pinned_table_verbatim() -> None:
    """`TRIGGER_ACTIONS` matches the architecture plan's table exactly, one
    entry at a time -- a single misassigned trigger fails this test.
    """
    for trigger in DemotionTrigger:
        expected = _EXPECTED_TRIGGER_ACTIONS[trigger.name]
        assert TRIGGER_ACTIONS[trigger] is expected, trigger.name


# --- Full 7x16 = 112 (mode, trigger) resolution matrix ---------------------------

_ALL_MODES: tuple[Mode, ...] = tuple(Mode)
_ALL_MODE_TRIGGER_PAIRS: tuple[tuple[Mode, DemotionTrigger], ...] = tuple(
    itertools.product(_ALL_MODES, DemotionTrigger)
)


def test_full_matrix_fixture_has_112_pairs() -> None:
    """Sanity check on the fixture itself: 7 modes x 16 triggers = 112."""
    assert len(_ALL_MODE_TRIGGER_PAIRS) == 112


@pytest.mark.parametrize(
    ("mode", "trigger"),
    _ALL_MODE_TRIGGER_PAIRS,
    ids=[f"{mode.name}+{trigger.name}" for mode, trigger in _ALL_MODE_TRIGGER_PAIRS],
)
def test_resolve_demotion_matches_pinned_expectation_for_every_pair(
    mode: Mode, trigger: DemotionTrigger
) -> None:
    """Every one of the 112 (mode, trigger) pairs resolves exactly as the
    pinned `_ACTION_MODE_EXPECTATION` table predicts.
    """
    assert resolve_demotion(mode, trigger) == _expected_resolution(mode, trigger)


def test_every_trigger_from_killed_resolves_to_none() -> None:
    """KILLED is a dead end: every one of the 16 triggers is a no-op from it."""
    for trigger in DemotionTrigger:
        assert resolve_demotion(Mode.KILLED, trigger) is None


def test_demote_one_mode_from_research_floors_at_paused_not_none() -> None:
    """The RESEARCH floor case is fail-safe (-> PAUSED), not a silent no-op,
    distinguishing it from the off-ladder/KILLED no-op cases.
    """
    trigger = next(
        trigger
        for trigger in DemotionTrigger
        if TRIGGER_ACTIONS[trigger] is DemotionAction.DEMOTE_ONE_MODE
    )

    assert resolve_demotion(Mode.RESEARCH, trigger) is Mode.PAUSED


@pytest.mark.parametrize("off_ladder_mode", [Mode.PAUSED, Mode.HALT])
def test_demote_one_mode_off_ladder_is_a_no_op(off_ladder_mode: Mode) -> None:
    """DEMOTE_ONE_MODE from PAUSED or HALT (off the promotion ladder) is a
    no-op (`None`), never a fabricated ladder position.
    """
    trigger = next(
        trigger
        for trigger in DemotionTrigger
        if TRIGGER_ACTIONS[trigger] is DemotionAction.DEMOTE_ONE_MODE
    )

    assert resolve_demotion(off_ladder_mode, trigger) is None


# --- Kernel integration: fire_demotion_trigger -----------------------------------


def _kernel_at(mode: Mode, *, ceiling: Mode = Mode.LIVE) -> RiskKernel:
    """Build a `RiskKernel` parked at `mode`, ceilinged at `ceiling`.

    Args:
        mode: The starting operating mode.
        ceiling: The configured `mode_ceiling`.

    Returns:
        A `RiskKernel` wired to a fresh `InMemoryKernelLedgerWriter`.
    """
    machine = ModeStateMachine(mode_ceiling=ceiling, mode=mode)
    return RiskKernel(InMemoryKernelLedgerWriter(), mode_machine=machine)


def test_fire_demotion_trigger_ledgers_one_event_and_transitions() -> None:
    """A firing that resolves to a real destination ledgers exactly one
    `DemotionTriggerFired` (transitioned=True) and moves the kernel there.
    """
    kernel = _kernel_at(Mode.LIVE)

    destination = kernel.fire_demotion_trigger(DemotionTrigger.DAILY_LOSS_BREACH)

    assert destination is Mode.PAUSED
    assert kernel.mode is Mode.PAUSED
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "DemotionTriggerFired"
    ]
    assert len(events) == 1
    event = events[0]
    assert event.component == "riskkernel"
    assert event.payload["trigger"] == "DAILY_LOSS_BREACH"
    assert event.payload["action"] == "PAUSE"
    assert event.payload["from_mode"] == "LIVE"
    assert event.payload["to_mode"] == "PAUSED"
    assert event.payload["transitioned"] is True


def test_fire_demotion_trigger_no_op_firing_ledgers_transitioned_false() -> None:
    """A no-op firing (e.g. HALT while already HALT) still ledgers exactly
    one `DemotionTriggerFired`, but with `transitioned=False`, and the mode
    never moves.
    """
    kernel = _kernel_at(Mode.HALT)

    destination = kernel.fire_demotion_trigger(DemotionTrigger.SCHEMA_ANOMALY)

    assert destination is None
    assert kernel.mode is Mode.HALT
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "DemotionTriggerFired"
    ]
    assert len(events) == 1
    assert events[0].payload["transitioned"] is False
    assert events[0].payload["from_mode"] == "HALT"
    assert events[0].payload["to_mode"] == "HALT"


def test_fire_demotion_trigger_never_raises_from_a_safety_mode() -> None:
    """Firing any trigger from PAUSED, HALT, or KILLED never raises -- every
    one of the 16 triggers resolves cleanly (possibly to a no-op) from a
    safety mode.
    """
    for mode in (Mode.PAUSED, Mode.HALT, Mode.KILLED):
        kernel = _kernel_at(mode)
        for trigger in DemotionTrigger:
            kernel.fire_demotion_trigger(trigger)


def test_fire_demotion_trigger_keeps_killed_kernel_killed() -> None:
    """MANUAL_KILL (or any trigger) fired at a KILLED kernel leaves it KILLED."""
    kernel = _kernel_at(Mode.KILLED)

    kernel.fire_demotion_trigger(DemotionTrigger.MANUAL_KILL)

    assert kernel.mode is Mode.KILLED


def test_two_drawdown_breaches_from_live_step_down_twice() -> None:
    """Two successive `DRAWDOWN_BREACH` firings from LIVE demote one rung
    each time (LIVE -> LIVE_MICRO -> PAPER), recording two distinct
    `DemotionTriggerFired` events.
    """
    kernel = _kernel_at(Mode.LIVE)

    first = kernel.fire_demotion_trigger(DemotionTrigger.DRAWDOWN_BREACH)
    second = kernel.fire_demotion_trigger(DemotionTrigger.DRAWDOWN_BREACH)

    assert first is Mode.LIVE_MICRO
    assert second is Mode.PAPER
    assert kernel.mode is Mode.PAPER
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "DemotionTriggerFired"
    ]
    assert len(events) == 2
    assert events[0].payload["from_mode"] == "LIVE"
    assert events[0].payload["to_mode"] == "LIVE_MICRO"
    assert events[1].payload["from_mode"] == "LIVE_MICRO"
    assert events[1].payload["to_mode"] == "PAPER"
    assert all(event.payload["transitioned"] is True for event in events)


# --- DemotionTriggerFired event: registry round trip -----------------------------


def test_demotion_trigger_fired_event_reconstructs_and_round_trips() -> None:
    """`DemotionTriggerFired` derives the full `Event` contract and its
    payload round-trips through `EVENT_TYPES` + `canonical_json`.
    """
    event = DemotionTriggerFired(
        component="riskkernel",
        trigger="DAILY_LOSS_BREACH",
        action="PAUSE",
        from_mode="LIVE",
        to_mode="PAUSED",
        transitioned=True,
    )

    assert event.event_type == "DemotionTriggerFired"
    envelope = json.loads(event.envelope_json)
    rebuilt_cls = EVENT_TYPES[event.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == event
    assert json.loads(canonical_json(event.payload)) == event.payload
