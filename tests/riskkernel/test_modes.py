"""Failing-first tests for hedgekit.riskkernel.modes (issue #29, RED).

Issue #29 gives the Risk Kernel (Process B, SPEC S5.1-S5.3) its operating-mode
state machine: the seven-state ladder RESEARCH -> PAPER -> LIVE_MICRO -> LIVE,
plus the always-reachable safety states PAUSED, HALT, and KILLED, each guarded
by a `mode_ceiling` the runtime may never exceed and a typed-confirmation
re-arm procedure once killed.

`hedgekit/riskkernel/modes.py` does not exist yet, so the import below fails
the whole module at collection with
`ModuleNotFoundError: No module named 'hedgekit.riskkernel.modes'` -- the
expected Gate 1 RED state for issue #29. Once the module exists, this file
pins the exact transition table (all 49 ordered (from, to) pairs over the
7-member `Mode` enum), the `mode_ceiling` enforcement (`ModeCeilingExceededError`
as a subclass of `IllegalModeTransitionError`), the KILLED/re-arm procedure
(`KillReArmError`, the exact `REARM_CONFIRMATION_PHRASE` constant), and
`Mode.from_config`'s parsing of the SPEC S16 `mode_ceiling` config token.
"""

from __future__ import annotations

import itertools

import pytest

from hedgekit.config import HedgekitConfig
from hedgekit.riskkernel.modes import (
    REARM_CONFIRMATION_PHRASE,
    IllegalModeTransitionError,
    KillReArmError,
    Mode,
    ModeCeilingExceededError,
    ModeStateMachine,
)

#: The exact 7-member ladder, in SPEC promotion order.
_EXPECTED_MODE_NAMES = frozenset(
    {"RESEARCH", "PAPER", "LIVE_MICRO", "LIVE", "PAUSED", "HALT", "KILLED"}
)

#: Every `Mode` member, in enum definition order.
_ALL_MODES: tuple[Mode, ...] = tuple(Mode)

#: Every mode except KILLED -- KILLED is a dead end reachable only via `rearm`.
_NON_KILLED_MODES: tuple[Mode, ...] = tuple(
    mode for mode in Mode if mode is not Mode.KILLED
)

#: The one-step promotion ladder: each pair is legal only moving upward by
#: exactly one rung, and only up to `mode_ceiling`.
_PROMOTION_STEPS: tuple[tuple[Mode, Mode], ...] = (
    (Mode.RESEARCH, Mode.PAPER),
    (Mode.PAPER, Mode.LIVE_MICRO),
    (Mode.LIVE_MICRO, Mode.LIVE),
)

#: The three "safety" targets reachable from any non-KILLED mode.
_TERMINAL_TARGETS: tuple[Mode, ...] = (Mode.PAUSED, Mode.HALT, Mode.KILLED)


def _demotion_and_terminal_pairs() -> frozenset[tuple[Mode, Mode]]:
    """Return every legal (source, target) pair landing on PAUSED/HALT/KILLED.

    Any non-KILLED mode may move to PAUSED, HALT, or KILLED, except a
    same-mode "transition" (e.g. PAUSED -> PAUSED), which is always illegal
    regardless of target.

    Returns:
        The frozenset of legal (source, target) pairs whose target is one of
        the three terminal/safety modes.
    """
    return frozenset(
        (source, target)
        for source in _NON_KILLED_MODES
        for target in _TERMINAL_TARGETS
        if source is not target
    )


#: The complete legal-transition table: 3 promotion steps + 16 demotion/safety
#: moves = 19 legal pairs out of the full 7x7 = 49.
LEGAL_TRANSITIONS: frozenset[tuple[Mode, Mode]] = (
    frozenset(_PROMOTION_STEPS) | _demotion_and_terminal_pairs()
)

#: Every ordered (from, to) pair over the 7-member ladder.
ALL_MODE_PAIRS: tuple[tuple[Mode, Mode], ...] = tuple(
    itertools.product(_ALL_MODES, repeat=2)
)


def test_mode_has_exactly_the_seven_spec_members() -> None:
    """`Mode` has exactly RESEARCH/PAPER/LIVE_MICRO/LIVE/PAUSED/HALT/KILLED."""
    assert {member.name for member in Mode} == _EXPECTED_MODE_NAMES
    assert len(Mode) == 7


def test_legal_transition_table_has_nineteen_pairs() -> None:
    """Sanity check on the fixture itself: 3 promotions + 16 safety moves."""
    assert len(LEGAL_TRANSITIONS) == 19


@pytest.mark.parametrize(
    ("source", "target"),
    ALL_MODE_PAIRS,
    ids=[f"{source.name}->{target.name}" for source, target in ALL_MODE_PAIRS],
)
def test_every_ordered_mode_pair_matches_the_pinned_legal_table(
    source: Mode, target: Mode
) -> None:
    """Each of the 49 ordered pairs is legal (and succeeds) iff it is in
    `LEGAL_TRANSITIONS`; every illegal pair raises `IllegalModeTransitionError` and
    leaves `.mode` unchanged. `mode_ceiling=Mode.LIVE` (the highest rung) so
    no legal promotion is ever blocked by the ceiling in this table-driven
    sweep -- ceiling enforcement itself is pinned separately below.
    """
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=source)

    if (source, target) in LEGAL_TRANSITIONS:
        machine.transition(target)
        assert machine.mode == target
    else:
        with pytest.raises(IllegalModeTransitionError):
            machine.transition(target)
        assert machine.mode == source


def test_multi_step_promotion_reaches_live_under_a_live_ceiling() -> None:
    """RESEARCH -> PAPER -> LIVE_MICRO -> LIVE succeeds one step at a time."""
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE)

    machine.transition(Mode.PAPER)
    assert machine.mode == Mode.PAPER

    machine.transition(Mode.LIVE_MICRO)
    assert machine.mode == Mode.LIVE_MICRO

    machine.transition(Mode.LIVE)
    assert machine.mode == Mode.LIVE


def test_skipping_a_promotion_step_raises_and_leaves_mode_unchanged() -> None:
    """RESEARCH -> LIVE_MICRO (skipping PAPER) is illegal even under a
    permissive ceiling.
    """
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE)

    with pytest.raises(IllegalModeTransitionError):
        machine.transition(Mode.LIVE_MICRO)

    assert machine.mode == Mode.RESEARCH


def test_mode_ceiling_exceeded_is_an_illegal_mode_transition() -> None:
    """`ModeCeilingExceededError` is a (more specific) `IllegalModeTransitionError`."""
    assert issubclass(ModeCeilingExceededError, IllegalModeTransitionError)


def test_promotion_beyond_ceiling_raises_mode_ceiling_exceeded() -> None:
    """PAPER -> LIVE_MICRO under `mode_ceiling=PAPER` raises the specific
    `ModeCeilingExceededError`, not a generic `IllegalModeTransitionError`, and leaves
    `.mode` at PAPER.
    """
    machine = ModeStateMachine(mode_ceiling=Mode.PAPER)
    machine.transition(Mode.PAPER)

    with pytest.raises(ModeCeilingExceededError):
        machine.transition(Mode.LIVE_MICRO)

    assert machine.mode == Mode.PAPER


@pytest.mark.parametrize("safety_target", [Mode.PAUSED, Mode.HALT, Mode.KILLED])
def test_ceiling_never_blocks_a_move_to_a_safety_mode(safety_target: Mode) -> None:
    """A low `mode_ceiling` (PAPER) never blocks PAUSED/HALT/KILLED -- those
    are not "upward" promotions and the ceiling only bounds the ladder.
    """
    machine = ModeStateMachine(mode_ceiling=Mode.PAPER, mode=Mode.PAPER)

    machine.transition(safety_target)

    assert machine.mode == safety_target


@pytest.mark.parametrize("demoted_from", [Mode.LIVE, Mode.LIVE_MICRO, Mode.PAPER])
def test_ceiling_never_blocks_demotion_to_paused(demoted_from: Mode) -> None:
    """A ceiling bounding the ladder does not block demoting *to* PAUSED from
    a mode that itself sits above the ceiling (demotions are not "upward").
    """
    machine = ModeStateMachine(mode_ceiling=Mode.RESEARCH, mode=demoted_from)

    machine.transition(Mode.PAUSED)

    assert machine.mode == Mode.PAUSED


@pytest.mark.parametrize("target", list(Mode))
def test_every_transition_from_killed_raises_illegal_mode_transition(
    target: Mode,
) -> None:
    """From KILLED, every `transition()` call -- including to KILLED itself
    -- raises `IllegalModeTransitionError` and leaves `.mode` at KILLED.
    """
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.KILLED)

    with pytest.raises(IllegalModeTransitionError):
        machine.transition(target)

    assert machine.mode == Mode.KILLED


def test_rearm_with_the_exact_phrase_returns_to_research() -> None:
    """A correctly typed `rearm` confirmation returns KILLED -> RESEARCH."""
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.KILLED)

    machine.rearm(REARM_CONFIRMATION_PHRASE)

    assert machine.mode == Mode.RESEARCH


@pytest.mark.parametrize(
    "bad_phrase", ["", "definitely-wrong-phrase", "another-bad-one"]
)
def test_rearm_with_a_wrong_or_empty_phrase_raises_and_stays_killed(
    bad_phrase: str,
) -> None:
    """A wrong or empty confirmation phrase raises `KillReArmError` and never
    moves the machine out of KILLED.
    """
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.KILLED)

    with pytest.raises(KillReArmError):
        machine.rearm(bad_phrase)

    assert machine.mode == Mode.KILLED


def test_rearm_with_a_case_mismatched_phrase_raises_and_stays_killed() -> None:
    """A case-swapped confirmation phrase (e.g. `"Confirm"` vs `"confirm"`)
    is rejected -- `rearm` must not case-fold the comparison.
    """
    mismatched_phrase = REARM_CONFIRMATION_PHRASE.swapcase()
    assert mismatched_phrase != REARM_CONFIRMATION_PHRASE, (
        "fixture assumption: REARM_CONFIRMATION_PHRASE must contain a cased "
        "character for this test to be meaningful"
    )
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.KILLED)

    with pytest.raises(KillReArmError):
        machine.rearm(mismatched_phrase)

    assert machine.mode == Mode.KILLED


@pytest.mark.parametrize("mode", _NON_KILLED_MODES)
def test_rearm_when_not_killed_raises_kill_rearm_error(mode: Mode) -> None:
    """Calling `rearm` from any mode other than KILLED raises
    `KillReArmError` and leaves the machine in its current mode.
    """
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=mode)

    with pytest.raises(KillReArmError):
        machine.rearm(REARM_CONFIRMATION_PHRASE)

    assert machine.mode == mode


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("research", Mode.RESEARCH),
        ("paper", Mode.PAPER),
        ("live_micro", Mode.LIVE_MICRO),
        ("live", Mode.LIVE),
    ],
)
def test_from_config_parses_each_valid_ceiling_token(
    token: str, expected: Mode
) -> None:
    """`Mode.from_config` parses each of the four SPEC S16 ceiling tokens."""
    assert Mode.from_config(token) == expected


@pytest.mark.parametrize("token", ["paused", "halt", "killed", "LIVE", "", "garbage"])
def test_from_config_rejects_non_ceiling_tokens(token: str) -> None:
    """`Mode.from_config` rejects safety-mode tokens, wrong case, empty, and
    garbage input -- only the four promotable-ceiling tokens ever parse.
    """
    with pytest.raises(ValueError):
        Mode.from_config(token)


def test_from_config_matches_the_default_hedgekit_config_mode_ceiling() -> None:
    """`HedgekitConfig()`'s default `mode_ceiling` ("paper") parses to
    `Mode.PAPER` -- the config schema and the mode machine agree on the
    ceiling token vocabulary end to end.
    """
    assert Mode.from_config(HedgekitConfig().mode_ceiling) == Mode.PAPER
