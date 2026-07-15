"""Failing-first tests for the mode-ceiling override (issue #33, RED).

Issue #33 gives the Risk Kernel a single, permanent, ledgered escape hatch
from the mandatory Brier-skill-significance gate: an operator who types the
exact `SIGNIFICANCE_OVERRIDE_ACK_PHRASE` may promote past that one gate, but
only ever up to `OVERRIDE_CEILING` (`Mode.LIVE_MICRO`) -- never all the way to
`Mode.LIVE` -- and the override survives a process restart because it is
itself a ledgered event (`SignificanceOverrideApplied`) replayed by
`RiskKernel.from_events`, not in-memory state.

Neither `windbreak/riskkernel/promotion.py` nor the new `windbreak.ledger.events`
class it needs (`SignificanceOverrideApplied`) exist yet, so this file fails
collection in two stages depending on which is fixed first: today, the
`from windbreak.ledger.events import ... SignificanceOverrideApplied ...` line
raises `ImportError: cannot import name 'SignificanceOverrideApplied' from
'windbreak.ledger.events'` (that module exists but does not yet define the
class); once `events.py` is extended, the next import,
`from windbreak.riskkernel.promotion import ...`, would raise
`ModuleNotFoundError: No module named 'windbreak.riskkernel.promotion'`. Either
way this is the expected Gate 1 RED state for issue #33.

This file duplicates the small `_kernel_at` builder from `test_promotion.py`
and `test_demotion.py` rather than centralizing it in `conftest.py`:
`conftest.py` is shared by every test in `tests/riskkernel/`, including
already-passing suites, and importing a not-yet-existing module there would
turn this file's expected RED (a `ModuleNotFoundError` scoped to this file)
into a collection-wide failure across the whole directory. Once
`windbreak/riskkernel/promotion.py` exists, this ~10-line duplication is a
reasonable, deliberate trade against that collection-wide blast radius.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest

from windbreak.config import EvaluationConfig
from windbreak.evaluation.preregistration import build_gate_plan, register_gate_plan
from windbreak.ledger.events import (
    EVENT_TYPES,
    Event,
    ModeHeartbeat,
    SignificanceOverrideApplied,
    canonical_json,
)
from windbreak.ledger.store import SqliteLedgerStore
from windbreak.riskkernel.modes import Mode, ModeCeilingExceededError, ModeStateMachine
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter, RiskKernel
from windbreak.riskkernel.promotion import (
    OVERRIDE_CEILING,
    SIGNIFICANCE_OVERRIDE_ACK_PHRASE,
    GateEvidence,
    OverrideAcknowledgementError,
    effective_mode_ceiling,
    override_applied_in,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from windbreak.ledger.store import LedgerStore

#: A `GateEvidence` snapshot that satisfies every LIVE_MICRO->LIVE criterion
#: (the other 14 fields are irrelevant to that gate and left at their
#: failing-closed defaults).
_LIVE_MICRO_TO_LIVE_ALL_PASSING = GateEvidence(
    live_micro_days=60,
    live_slippage_vs_paper_ppm=0,
    live_brier_degradation_ppm=0,
    reconciliation_halt_count=0,
    invariant_violation_count=0,
    operator_confirmation=True,
)

#: A `GateEvidence` snapshot that satisfies every PAPER->LIVE_MICRO
#: criterion *except* the mandatory significance criterion:
#: `brier_skill_ci_lower_ppm=0` fails the `GT 0` comparison while
#: `brier_skill_ppm` still clears its own, separate floor. This is the one
#: evidence shape the significance override exists to rescue.
_PAPER_PASSING_EXCEPT_SIGNIFICANCE = GateEvidence(
    resolved_realtime_forecast_count=300,
    independent_event_group_count=100,
    brier_skill_ppm=10_000,
    brier_skill_ci_lower_ppm=0,
    paper_pnl_net_micro_usd=1,
    paper_window_days=90,
    paper_max_drawdown_ppm=0,
    calibration_slope_ppm=1_000_000,
    kernel_invariant_failure_count=0,
)

#: The same snapshot with the significance criterion also passing -- a
#: fully-approving PAPER->LIVE_MICRO evidence set that needs no bypass.
_PAPER_ALL_PASSING = dataclasses.replace(
    _PAPER_PASSING_EXCEPT_SIGNIFICANCE, brier_skill_ci_lower_ppm=1
)

#: Wrong/empty/case-folded/whitespace-padded near-misses of the real phrase,
#: derived from the production constant itself so this file never hardcodes
#: (and thus never accidentally drifts from) the real phrase text.
_BAD_ACK_PHRASES: tuple[str, ...] = (
    "",
    "definitely not the phrase",
    SIGNIFICANCE_OVERRIDE_ACK_PHRASE.casefold(),
    f" {SIGNIFICANCE_OVERRIDE_ACK_PHRASE} ",
    SIGNIFICANCE_OVERRIDE_ACK_PHRASE.swapcase(),
)


def _kernel_at(
    mode: Mode,
    *,
    ceiling: Mode = Mode.LIVE,
    gate_plan_store: LedgerStore | None = None,
) -> RiskKernel:
    """Build a `RiskKernel` parked at `mode`, ceilinged at `ceiling`.

    Args:
        mode: The starting operating mode.
        ceiling: The configured `mode_ceiling`.
        gate_plan_store: The ledger the kernel reads its PAPER gate plan from
            (issue #185); pass the `paper_gate_plan_store` fixture for any
            test that promotes PAPER->LIVE_MICRO.

    Returns:
        A `RiskKernel` wired to a fresh `InMemoryKernelLedgerWriter`.
    """
    machine = ModeStateMachine(mode_ceiling=ceiling, mode=mode)
    return RiskKernel(
        InMemoryKernelLedgerWriter(),
        mode_machine=machine,
        gate_plan_store=gate_plan_store,
    )


@pytest.fixture
def paper_gate_plan_store(tmp_path: Path) -> Iterator[LedgerStore]:
    """Provide a `SqliteLedgerStore` with a default-config PAPER gate plan.

    Registers a plan built from the default `EvaluationConfig()` -- whose
    three plan-sourced thresholds (300/100/10000) match exactly the values
    `_PAPER_PASSING_EXCEPT_SIGNIFICANCE`/`_PAPER_ALL_PASSING` are tuned
    against -- so wiring this store into `_kernel_at` (issue #185) changes no
    existing test's verdict; it only satisfies the new fail-closed
    precondition that a PAPER promotion attempt must find a registered plan.

    Args:
        tmp_path: The pytest tmp-path directory the store is rooted in.

    Yields:
        A `LedgerStore` carrying exactly one `GatePlanRegistered` record.
    """
    store = SqliteLedgerStore(tmp_path / "override_paper_gate_plan.db")
    plan = build_gate_plan(
        EvaluationConfig(), paper_fill_model_version="override-test-v1"
    )
    register_gate_plan(plan, store)
    yield store
    store.close()


# --- Override constants: sanity ---------------------------------------------------


def test_override_ceiling_constant_is_live_micro() -> None:
    """`OVERRIDE_CEILING` is exactly `Mode.LIVE_MICRO` -- the override can
    never reach `Mode.LIVE`.
    """
    assert OVERRIDE_CEILING is Mode.LIVE_MICRO


def test_significance_override_ack_phrase_contains_a_cased_character() -> None:
    """The phrase contains a cased character, so the case-folded near-miss
    tested below is a meaningful negative case (not accidentally identical).
    """
    phrase = SIGNIFICANCE_OVERRIDE_ACK_PHRASE
    assert phrase != phrase.casefold()


# --- effective_mode_ceiling: pure ladder-rank MIN ---------------------------------


@pytest.mark.parametrize(
    "configured",
    [Mode.RESEARCH, Mode.PAPER, Mode.LIVE_MICRO, Mode.LIVE],
    ids=lambda m: m.name,
)
def test_effective_mode_ceiling_is_configured_when_no_override(
    configured: Mode,
) -> None:
    """Without an override, the effective ceiling is simply the configured one."""
    assert effective_mode_ceiling(configured, override_applied=False) is configured


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (Mode.RESEARCH, Mode.RESEARCH),
        (Mode.PAPER, Mode.PAPER),
        (Mode.LIVE_MICRO, Mode.LIVE_MICRO),
        (Mode.LIVE, Mode.LIVE_MICRO),
    ],
    ids=["research", "paper", "live_micro", "live"],
)
def test_effective_mode_ceiling_caps_at_live_micro_when_override_applied(
    configured: Mode, expected: Mode
) -> None:
    """With an override applied, the effective ceiling is the ladder-rank MIN
    of the configured ceiling and `Mode.LIVE_MICRO` -- a low configured
    ceiling (RESEARCH/PAPER/LIVE_MICRO) is unaffected; only LIVE is capped
    down.
    """
    assert effective_mode_ceiling(configured, override_applied=True) is expected


# --- Phrase rigor: verbatim, case-sensitive, no ledger write on mismatch --------


def test_apply_ledgered_override_with_the_exact_phrase_applies() -> None:
    """The exact phrase applies the cap and ledgers `SignificanceOverrideApplied`."""
    kernel = _kernel_at(Mode.LIVE_MICRO, ceiling=Mode.LIVE)

    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)

    assert kernel.mode_ceiling_effective is Mode.LIVE_MICRO
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "SignificanceOverrideApplied"
    ]
    assert len(events) == 1
    assert events[0].payload["operator_ack"] == SIGNIFICANCE_OVERRIDE_ACK_PHRASE
    assert events[0].payload["ceiling"] == "LIVE_MICRO"


@pytest.mark.parametrize("bad_ack", _BAD_ACK_PHRASES)
def test_apply_ledgered_override_rejects_any_near_miss_phrase(bad_ack: str) -> None:
    """Any wrong, empty, case-folded, or whitespace-padded near-miss raises
    `OverrideAcknowledgementError`, ledgers nothing, and leaves the effective
    ceiling at the configured value.
    """
    kernel = _kernel_at(Mode.LIVE_MICRO, ceiling=Mode.LIVE)

    with pytest.raises(OverrideAcknowledgementError):
        kernel.apply_ledgered_override(bad_ack)

    assert kernel.mode_ceiling_effective is Mode.LIVE
    assert kernel.ledger_writer.events == []


# --- Cap semantics: LIVE + override -> effective LIVE_MICRO ----------------------


def test_configured_live_plus_override_caps_effective_ceiling_at_live_micro() -> None:
    """Configured ceiling LIVE, once overridden, has an effective ceiling of
    LIVE_MICRO -- the full ladder is never unlocked by the override.
    """
    kernel = _kernel_at(Mode.LIVE_MICRO, ceiling=Mode.LIVE)

    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)

    assert kernel.mode_ceiling_effective is Mode.LIVE_MICRO


def test_override_cap_blocks_live_promotion_but_still_ledgers() -> None:
    """Even with all-passing LIVE_MICRO->LIVE evidence, an active override
    caps the kernel at LIVE_MICRO: `request_promotion` still ledgers an
    approving `PromotionEvaluated` event (the evidence really did pass), then
    raises `ModeCeilingExceededError` on the ceiling check, leaving the mode
    unchanged.
    """
    kernel = _kernel_at(Mode.LIVE_MICRO, ceiling=Mode.LIVE)
    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)

    with pytest.raises(ModeCeilingExceededError):
        kernel.request_promotion(_LIVE_MICRO_TO_LIVE_ALL_PASSING)

    assert kernel.mode is Mode.LIVE_MICRO
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(events) == 1
    assert events[0].payload["approved"] is True
    assert events[0].payload["override_bypassed"] is False


# --- Significance bypass: the sole override-rescuable criterion ----------------


def test_significance_bypass_promotes_paper_to_live_micro(
    paper_gate_plan_store: LedgerStore,
) -> None:
    """Without the override, PAPER evidence failing only the significance
    criterion neither promotes nor bypasses. With the ledgered override
    applied, the identical evidence promotes PAPER -> LIVE_MICRO via the
    bypass -- even though the raw, ledgered decision is still `approved is
    False` (the override changes the kernel's mode, never the evaluation).
    """
    kernel = _kernel_at(
        Mode.PAPER, ceiling=Mode.LIVE, gate_plan_store=paper_gate_plan_store
    )

    without_override = kernel.request_promotion(_PAPER_PASSING_EXCEPT_SIGNIFICANCE)

    assert kernel.mode is Mode.PAPER
    assert without_override.approved is False
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(events) == 1
    assert events[0].payload["approved"] is False
    assert events[0].payload["override_bypassed"] is False

    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)
    with_override = kernel.request_promotion(_PAPER_PASSING_EXCEPT_SIGNIFICANCE)

    assert kernel.mode is Mode.LIVE_MICRO
    assert with_override.approved is False
    promotion_events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(promotion_events) == 2
    assert promotion_events[-1].payload["approved"] is False
    assert promotion_events[-1].payload["override_bypassed"] is True


def test_override_does_not_bypass_a_non_significance_failure(
    paper_gate_plan_store: LedgerStore,
) -> None:
    """A second, non-overridable failure alongside the significance failure
    blocks the bypass entirely: too few resolved forecasts
    (`resolved_realtime_forecast_count=299`, failing the non-overridable
    `paper_resolved_forecasts` criterion) means the override rescues
    nothing, even though it is active.
    """
    kernel = _kernel_at(
        Mode.PAPER, ceiling=Mode.LIVE, gate_plan_store=paper_gate_plan_store
    )
    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)
    evidence = dataclasses.replace(
        _PAPER_PASSING_EXCEPT_SIGNIFICANCE, resolved_realtime_forecast_count=299
    )

    kernel.request_promotion(evidence)

    assert kernel.mode is Mode.PAPER
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(events) == 1
    assert events[0].payload["approved"] is False
    assert events[0].payload["override_bypassed"] is False


def test_override_unused_when_significance_passes(
    paper_gate_plan_store: LedgerStore,
) -> None:
    """When the significance criterion itself passes, an active override
    changes nothing: the promotion succeeds on its own merits, and
    `override_bypassed` is `False` because no bypass was needed.
    """
    kernel = _kernel_at(
        Mode.PAPER, ceiling=Mode.LIVE, gate_plan_store=paper_gate_plan_store
    )
    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)

    decision = kernel.request_promotion(_PAPER_ALL_PASSING)

    assert kernel.mode is Mode.LIVE_MICRO
    assert decision.approved is True
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(events) == 1
    assert events[0].payload["approved"] is True
    assert events[0].payload["override_bypassed"] is False


def test_cap_still_blocks_live_after_override_bypassed_promotion(
    paper_gate_plan_store: LedgerStore,
) -> None:
    """Having reached LIVE_MICRO via the override bypass, the permanent cap
    still blocks LIVE_MICRO -> LIVE: `request_promotion` still ledgers the
    approving LIVE_MICRO->LIVE evaluation, then raises
    `ModeCeilingExceededError`, leaving the mode at LIVE_MICRO.
    """
    kernel = _kernel_at(
        Mode.PAPER, ceiling=Mode.LIVE, gate_plan_store=paper_gate_plan_store
    )
    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)
    kernel.request_promotion(_PAPER_PASSING_EXCEPT_SIGNIFICANCE)
    assert kernel.mode is Mode.LIVE_MICRO

    with pytest.raises(ModeCeilingExceededError):
        kernel.request_promotion(_LIVE_MICRO_TO_LIVE_ALL_PASSING)

    assert kernel.mode is Mode.LIVE_MICRO
    live_micro_events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
        and event.payload["source_mode"] == "LIVE_MICRO"
    ]
    assert len(live_micro_events) == 1
    assert live_micro_events[0].payload["approved"] is True


def test_override_never_bypasses_research_to_paper_gate() -> None:
    """The RESEARCH->PAPER gate has no overridable criterion, so an active
    override never promotes past a failing RESEARCH criterion.
    """
    kernel = _kernel_at(Mode.RESEARCH, ceiling=Mode.LIVE)
    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)

    kernel.request_promotion(GateEvidence())

    assert kernel.mode is Mode.RESEARCH
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(events) == 1
    assert events[0].payload["override_bypassed"] is False


# --- MIN-not-set: a low configured ceiling is unaffected by the override --------


@pytest.mark.parametrize(
    ("configured_ceiling", "expected_effective"),
    [(Mode.RESEARCH, Mode.RESEARCH), (Mode.PAPER, Mode.PAPER)],
    ids=["research", "paper"],
)
def test_override_never_raises_a_low_configured_ceiling(
    configured_ceiling: Mode, expected_effective: Mode
) -> None:
    """An override applied under a configured ceiling already below
    LIVE_MICRO (RESEARCH or PAPER) leaves the effective ceiling exactly at
    the configured value -- the override only ever *lowers or matches*,
    never raises, the effective ceiling.
    """
    kernel = _kernel_at(Mode.RESEARCH, ceiling=configured_ceiling)

    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)

    assert kernel.mode_ceiling_effective is expected_effective


# --- Restart survival: the cap is ledgered state, not in-memory state ----------


def test_restart_survival_rebuilds_effective_live_micro_from_the_ledgered_event() -> (
    None
):
    """After a restart -- rebuilding a fresh `RiskKernel` via `from_events`
    over the exact same event history -- the effective ceiling is still
    LIVE_MICRO: the cap is durable, ledgered state, not process memory.
    """
    writer = InMemoryKernelLedgerWriter()
    original = RiskKernel(
        writer,
        mode_machine=ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE_MICRO),
    )
    original.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)

    rebuilt = RiskKernel.from_events(
        writer.events,
        InMemoryKernelLedgerWriter(),
        mode_machine=ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE_MICRO),
    )

    assert rebuilt.mode_ceiling_effective is Mode.LIVE_MICRO


def test_restart_without_a_ledgered_override_event_keeps_the_configured_ceiling() -> (
    None
):
    """Rebuilding from an event history that never recorded
    `SignificanceOverrideApplied` leaves the effective ceiling at whatever
    was configured -- the override is opt-in, never assumed.
    """
    rebuilt = RiskKernel.from_events(
        [],
        InMemoryKernelLedgerWriter(),
        mode_machine=ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE_MICRO),
    )

    assert rebuilt.mode_ceiling_effective is Mode.LIVE


def test_override_applied_in_empty_events_is_false() -> None:
    """`override_applied_in([])` is `False` -- no events, no override."""
    assert override_applied_in([]) is False


def test_override_applied_in_ignores_unrelated_events() -> None:
    """A history of unrelated events (heartbeats, a generic base `Event`)
    never trips `override_applied_in`.
    """
    events: list[Event] = [
        ModeHeartbeat(component="riskkernel", mode="RESEARCH", beat=1),
        Event(
            event_type="Something",
            component="riskkernel",
            payload_schema_version=1,
            payload={},
        ),
    ]

    assert override_applied_in(events) is False


def test_override_applied_in_detects_the_significance_override_event() -> None:
    """A history containing `SignificanceOverrideApplied` is detected, even
    alongside unrelated events.
    """
    events: list[Event] = [
        ModeHeartbeat(component="riskkernel", mode="RESEARCH", beat=1),
        SignificanceOverrideApplied(
            component="riskkernel",
            operator_ack=SIGNIFICANCE_OVERRIDE_ACK_PHRASE,
            ceiling="LIVE_MICRO",
        ),
    ]

    assert override_applied_in(events) is True


# --- Permanence: re-applying is allowed and re-ledgers; nothing unsets it ------


def test_reapplying_the_override_is_allowed_and_re_ledgers() -> None:
    """Applying the override twice succeeds both times, each recording its
    own `SignificanceOverrideApplied` event (idempotent effect, non-idempotent
    ledger).
    """
    kernel = _kernel_at(Mode.LIVE_MICRO, ceiling=Mode.LIVE)

    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)
    kernel.apply_ledgered_override(SIGNIFICANCE_OVERRIDE_ACK_PHRASE)

    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "SignificanceOverrideApplied"
    ]
    assert len(events) == 2
    assert kernel.mode_ceiling_effective is Mode.LIVE_MICRO


def test_risk_kernel_exposes_no_public_api_to_unset_the_override_cap() -> None:
    """No public `RiskKernel` method name suggests an "undo" path for the
    cap -- the override is a one-way, permanent ledgered decision.
    """
    suspicious_substrings = (
        "unset",
        "clear_override",
        "remove_override",
        "reset_ceiling",
        "disable_override",
        "revoke_override",
    )
    public_methods = [name for name in dir(RiskKernel) if not name.startswith("_")]

    for name in public_methods:
        lowered = name.lower()
        is_suspicious = any(sub in lowered for sub in suspicious_substrings)
        assert not is_suspicious, name


# --- SignificanceOverrideApplied event: shape and registry round trip ----------


def test_significance_override_applied_event_reconstructs_and_round_trips() -> None:
    """`SignificanceOverrideApplied` derives the full `Event` contract, its
    `ceiling` is always the string `"LIVE_MICRO"`, and its payload round-trips
    through `EVENT_TYPES` + `canonical_json`.
    """
    event = SignificanceOverrideApplied(
        component="riskkernel",
        operator_ack=SIGNIFICANCE_OVERRIDE_ACK_PHRASE,
        ceiling="LIVE_MICRO",
    )

    assert event.event_type == "SignificanceOverrideApplied"
    assert event.payload["ceiling"] == "LIVE_MICRO"
    assert event.payload["operator_ack"] == SIGNIFICANCE_OVERRIDE_ACK_PHRASE

    envelope = json.loads(event.envelope_json)
    rebuilt_cls = EVENT_TYPES[event.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == event
    assert json.loads(canonical_json(event.payload)) == event.payload
