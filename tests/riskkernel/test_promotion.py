"""Failing-first tests for windbreak.riskkernel.promotion (issue #33, RED).

Issue #33 gives the Risk Kernel its promotion-gate machinery: a pinned set of
`PromotionGate`s (RESEARCH->PAPER, PAPER->LIVE_MICRO, LIVE_MICRO->LIVE), each a
tuple of `GateCriterion`s evaluated against a `GateEvidence` snapshot by the
pure `evaluate_promotion` function, plus the kernel-level `request_promotion`
entrypoint that ledgers exactly one `PromotionEvaluated` event per attempt and
only mutates the mode machine on approval (honoring the ceiling).

Neither `windbreak/riskkernel/promotion.py` nor the three new
`windbreak.ledger.events` classes it needs (`PromotionEvaluated`) exist yet, so
this file fails collection in two stages depending on which is fixed first:
today, the `from windbreak.ledger.events import ... PromotionEvaluated ...`
line raises `ImportError: cannot import name 'PromotionEvaluated' from
'windbreak.ledger.events'` (that module exists but does not yet define the
class); once `events.py` is extended, the next import,
`from windbreak.riskkernel.promotion import ...`, would raise
`ModuleNotFoundError: No module named 'windbreak.riskkernel.promotion'`. Either
way this is the expected Gate 1 RED state for issue #33.

ASSUMPTIONS the implementer must honor or explicitly renegotiate with the
architect (the approved plan text left these underspecified):

1. Four numeric thresholds are not sourced from `EvaluationConfig` and have no
   concrete figure in the architecture plan text (`paper_max_drawdown_ppm`'s
   `LT threshold`, the `calibration_slope_ppm` band's low/high edges, and the
   two LIVE_MICRO->LIVE `LE` thresholds for slippage/Brier degradation). This
   file pins them to the module-level constants below
   (`_PAPER_MAX_DRAWDOWN_THRESHOLD_PPM`, `_CALIBRATION_SLOPE_LOW_PPM`,
   `_CALIBRATION_SLOPE_HIGH_PPM`, `_LIVE_SLIPPAGE_MAX_PPM`,
   `_LIVE_BRIER_DEGRADATION_MAX_PPM`). `windbreak/riskkernel/promotion.py` must
   define equal-valued module constants, or this file and the architect must
   agree on different figures together.
2. `GateEvidence.to_payload()` / `GateCriterion.to_payload()` /
   `PromotionGate.to_payload()` are assumed to key their output by the
   dataclass's own field names verbatim (mirroring
   `windbreak.connector.models.market_to_payload`'s convention), with
   `Comparison`/`Mode` values rendered as their `.name` string and
   `PromotionGate.criteria` rendered as a list of nested criterion payloads.
3. `PromotionDecision.results` is assumed ordered identically to
   `PromotionGate.criteria` (same length, same position), so a `CriterionResult`
   can be located either by index-correlation with its source `GateCriterion`
   or by matching `(evidence_field, comparison)` -- see `_result_for` below.
4. `evaluate_intent`-style event payload keys on `PromotionEvaluated` are
   assumed to be `source_mode`, `target_mode`, `approved`, `evidence`,
   `results` per the architecture plan's literal field list.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from windbreak.config import EvaluationConfig
from windbreak.ledger.events import EVENT_TYPES, PromotionEvaluated, canonical_json
from windbreak.riskkernel.modes import (
    IllegalModeTransitionError,
    Mode,
    ModeCeilingExceededError,
    ModeStateMachine,
)
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter, RiskKernel
from windbreak.riskkernel.promotion import (
    Comparison,
    CriterionResult,
    GateCriterion,
    GateEvidence,
    PromotionDecision,
    PromotionGate,
    build_promotion_gates,
    evaluate_promotion,
)

#: ASSUMPTION 1 (see module docstring): `paper_max_drawdown_ppm` must be
#: strictly below this many ppm (30%) to pass its `LT` criterion.
_PAPER_MAX_DRAWDOWN_THRESHOLD_PPM = 300_000

#: ASSUMPTION 1: the acceptable `calibration_slope_ppm` band is [0.8, 1.2],
#: expressed as two criteria (`GE` the low edge, `LE` the high edge).
_CALIBRATION_SLOPE_LOW_PPM = 800_000
_CALIBRATION_SLOPE_HIGH_PPM = 1_200_000

#: ASSUMPTION 1: LIVE_MICRO->LIVE `LE` ceilings on live-vs-paper divergence.
_LIVE_SLIPPAGE_MAX_PPM = 50_000
_LIVE_BRIER_DEGRADATION_MAX_PPM = 20_000

#: The default (SPEC S16) `EvaluationConfig`, used whenever a test does not
#: deliberately probe config-sourcing.
_DEFAULT_CONFIG = EvaluationConfig()

#: Every `GateEvidence` field name the architecture plan specifies, reflected
#: against the real dataclass below so a missing/renamed field fails loudly
#: rather than being silently skipped by every other test in this file.
_EXPECTED_EVIDENCE_FIELDS = frozenset(
    {
        "forecast_count",
        "adversarial_suite_green",
        "days_without_unhandled_errors",
        "ledger_rebuild_verified",
        "resolved_realtime_forecast_count",
        "independent_event_group_count",
        "brier_skill_ppm",
        "brier_skill_ci_lower_ppm",
        "brier_skill_ci_upper_ppm",
        "paper_pnl_net_micro_usd",
        "paper_window_days",
        "paper_max_drawdown_ppm",
        "calibration_slope_ppm",
        "kernel_invariant_failure_count",
        "live_micro_days",
        "live_slippage_vs_paper_ppm",
        "live_brier_degradation_ppm",
        "reconciliation_halt_count",
        "invariant_violation_count",
        "operator_confirmation",
    }
)

#: A single `GateEvidence` snapshot that satisfies every criterion of all
#: three gates simultaneously, under `_DEFAULT_CONFIG`'s thresholds. Every
#: boundary/isolation test below starts from this baseline and degrades
#: exactly one field, so a resulting failure can only be attributed to that
#: field.
_ALL_PASSING_EVIDENCE = GateEvidence(
    forecast_count=50,
    adversarial_suite_green=True,
    days_without_unhandled_errors=14,
    ledger_rebuild_verified=True,
    resolved_realtime_forecast_count=300,
    independent_event_group_count=100,
    brier_skill_ppm=10_000,
    brier_skill_ci_lower_ppm=1,
    brier_skill_ci_upper_ppm=20_000,
    paper_pnl_net_micro_usd=1,
    paper_window_days=90,
    paper_max_drawdown_ppm=0,
    calibration_slope_ppm=1_000_000,
    kernel_invariant_failure_count=0,
    live_micro_days=60,
    live_slippage_vs_paper_ppm=0,
    live_brier_degradation_ppm=0,
    reconciliation_halt_count=0,
    invariant_violation_count=0,
    operator_confirmation=True,
)


def _criterion_for(
    gate: PromotionGate, evidence_field: str, comparison: Comparison
) -> GateCriterion:
    """Return the single criterion matching (`evidence_field`, `comparison`).

    Args:
        gate: The gate to search.
        evidence_field: The `GateEvidence` field the criterion reads.
        comparison: The comparison the criterion applies.

    Returns:
        The one matching `GateCriterion`.
    """
    matches = [
        criterion
        for criterion in gate.criteria
        if criterion.evidence_field == evidence_field
        and criterion.comparison is comparison
    ]
    assert len(matches) == 1, (evidence_field, comparison, gate.criteria)
    return matches[0]


def _result_for(
    gate: PromotionGate,
    decision: PromotionDecision,
    evidence_field: str,
    comparison: Comparison,
) -> CriterionResult:
    """Return the `CriterionResult` correlated with a gate's criterion.

    Assumes (ASSUMPTION 3) `decision.results` is ordered identically to
    `gate.criteria`, and additionally verifies that assumption by asserting
    `criterion_id` agreement at the matched index.

    Args:
        gate: The gate `decision` was evaluated against.
        decision: The decision to search.
        evidence_field: The `GateEvidence` field the criterion reads.
        comparison: The comparison the criterion applies.

    Returns:
        The one matching `CriterionResult`.
    """
    assert len(decision.results) == len(gate.criteria)
    criteria = list(gate.criteria)
    indices = [
        index
        for index, criterion in enumerate(criteria)
        if criterion.evidence_field == evidence_field
        and criterion.comparison is comparison
    ]
    assert len(indices) == 1, (evidence_field, comparison, criteria)
    index = indices[0]
    result = decision.results[index]
    assert result.criterion_id == criteria[index].criterion_id
    return result


# --- GateEvidence: failing-closed defaults, frozen/slotted, kw-only --------------


def test_gate_evidence_has_exactly_the_spec_fields() -> None:
    """`GateEvidence` declares exactly the 20 spec-named fields, no more."""
    fields = {field.name for field in dataclasses.fields(GateEvidence)}

    assert fields == _EXPECTED_EVIDENCE_FIELDS
    assert len(_EXPECTED_EVIDENCE_FIELDS) == 20


def test_gate_evidence_default_construction_is_failing_closed() -> None:
    """Every int field defaults to 0 and every bool field defaults to False --
    `GateEvidence()` alone never satisfies any real promotion criterion.
    """
    evidence = GateEvidence()

    for field in dataclasses.fields(evidence):
        value = getattr(evidence, field.name)
        if isinstance(value, bool):
            assert value is False, field.name
        else:
            assert value == 0, field.name


def test_gate_evidence_rejects_positional_construction() -> None:
    """`GateEvidence` accepts only keyword arguments (kw-only dataclass)."""
    with pytest.raises(TypeError):
        GateEvidence(50)  # type: ignore[call-arg]


def test_gate_evidence_is_frozen_and_slotted() -> None:
    """`GateEvidence` instances are immutable and carry no `__dict__`."""
    evidence = GateEvidence()

    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.forecast_count = 1  # type: ignore[misc]
    assert not hasattr(evidence, "__dict__")


def test_gate_evidence_to_payload_round_trips_through_canonical_json() -> None:
    """`to_payload()` is a JSON-safe, lossless projection of every field."""
    payload = _ALL_PASSING_EVIDENCE.to_payload()

    assert json.loads(canonical_json(payload)) == payload
    assert payload["forecast_count"] == 50
    assert payload["adversarial_suite_green"] is True
    assert payload["operator_confirmation"] is True
    assert set(payload) == _EXPECTED_EVIDENCE_FIELDS


# --- Comparison / GateCriterion / PromotionGate: structure and to_payload -------


def test_comparison_has_exactly_the_six_spec_members() -> None:
    """`Comparison` has exactly GE/GT/LE/LT/EQ/IS_TRUE."""
    assert {member.name for member in Comparison} == {
        "GE",
        "GT",
        "LE",
        "LT",
        "EQ",
        "IS_TRUE",
    }
    assert len(Comparison) == 6


def test_gate_criterion_to_payload_uses_field_names_and_named_comparison() -> None:
    """`GateCriterion.to_payload()` keys by field name; `comparison` renders
    as its `.name` string, not the raw enum member.
    """
    criterion = GateCriterion(
        criterion_id="research_forecast_count",
        evidence_field="forecast_count",
        comparison=Comparison.GE,
        threshold=50,
        description="at least 50 resolved forecasts",
    )

    payload = criterion.to_payload()

    assert payload == {
        "criterion_id": "research_forecast_count",
        "evidence_field": "forecast_count",
        "comparison": "GE",
        "threshold": 50,
        "description": "at least 50 resolved forecasts",
        "overridable": False,
    }
    assert json.loads(canonical_json(payload)) == payload


@pytest.mark.parametrize(
    "overridable", [False, True], ids=["not_overridable", "overridable"]
)
def test_gate_criterion_to_payload_includes_overridable_bool(
    overridable: bool,
) -> None:
    """`to_payload()` carries an `"overridable"` key equal to the
    criterion's own `overridable` bool, in both directions.
    """
    criterion = GateCriterion(
        criterion_id="x",
        evidence_field="forecast_count",
        comparison=Comparison.GE,
        threshold=50,
        description="d",
        overridable=overridable,
    )

    assert criterion.to_payload()["overridable"] is overridable


def test_gate_criterion_is_frozen() -> None:
    """`GateCriterion` instances are immutable after construction."""
    criterion = GateCriterion(
        criterion_id="x",
        evidence_field="forecast_count",
        comparison=Comparison.GE,
        threshold=50,
        description="d",
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        criterion.threshold = 1  # type: ignore[misc]


def test_promotion_gate_to_payload_nests_criteria_payloads_and_named_modes() -> None:
    """`PromotionGate.to_payload()` renders `source`/`target` as mode names
    and `criteria` as a list of each criterion's own `to_payload()`.
    """
    criterion = GateCriterion(
        criterion_id="c1",
        evidence_field="forecast_count",
        comparison=Comparison.GE,
        threshold=50,
        description="d",
    )
    gate = PromotionGate(source=Mode.RESEARCH, target=Mode.PAPER, criteria=(criterion,))

    payload = gate.to_payload()

    assert payload == {
        "source": "RESEARCH",
        "target": "PAPER",
        "criteria": [criterion.to_payload()],
    }
    assert json.loads(canonical_json(payload)) == payload


def test_promotion_gate_is_frozen() -> None:
    """`PromotionGate` instances are immutable after construction."""
    gate = PromotionGate(source=Mode.RESEARCH, target=Mode.PAPER, criteria=())

    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.target = Mode.LIVE  # type: ignore[misc]


# --- build_promotion_gates: structure, keying, and config-sourcing -------------


def test_build_promotion_gates_is_keyed_by_source_mode() -> None:
    """`build_promotion_gates` returns exactly the three promotable-source
    gates, each keyed by its own `source` mode.
    """
    gates = build_promotion_gates(_DEFAULT_CONFIG)

    assert set(gates) == {Mode.RESEARCH, Mode.PAPER, Mode.LIVE_MICRO}
    assert gates[Mode.RESEARCH].source is Mode.RESEARCH
    assert gates[Mode.RESEARCH].target is Mode.PAPER
    assert gates[Mode.PAPER].source is Mode.PAPER
    assert gates[Mode.PAPER].target is Mode.LIVE_MICRO
    assert gates[Mode.LIVE_MICRO].source is Mode.LIVE_MICRO
    assert gates[Mode.LIVE_MICRO].target is Mode.LIVE


def test_research_to_paper_gate_has_exactly_four_criteria() -> None:
    """RESEARCH->PAPER pins forecasts/error-free-days/adversarial/ledger-rebuild."""
    gate = build_promotion_gates(_DEFAULT_CONFIG)[Mode.RESEARCH]

    assert len(gate.criteria) == 4
    assert {criterion.evidence_field for criterion in gate.criteria} == {
        "forecast_count",
        "days_without_unhandled_errors",
        "adversarial_suite_green",
        "ledger_rebuild_verified",
    }


def test_paper_to_live_micro_gate_has_exactly_ten_criteria() -> None:
    """PAPER->LIVE_MICRO has 10 criteria, 2 of which share the calibration
    field (the low/high band edges).
    """
    gate = build_promotion_gates(_DEFAULT_CONFIG)[Mode.PAPER]

    assert len(gate.criteria) == 10
    evidence_fields = [criterion.evidence_field for criterion in gate.criteria]
    assert evidence_fields.count("calibration_slope_ppm") == 2
    assert set(evidence_fields) == {
        "resolved_realtime_forecast_count",
        "independent_event_group_count",
        "brier_skill_ppm",
        "brier_skill_ci_lower_ppm",
        "paper_pnl_net_micro_usd",
        "paper_window_days",
        "paper_max_drawdown_ppm",
        "calibration_slope_ppm",
        "kernel_invariant_failure_count",
    }


def test_live_micro_to_live_gate_has_exactly_six_criteria() -> None:
    """LIVE_MICRO->LIVE pins the 6 post-live-micro-graduation criteria."""
    gate = build_promotion_gates(_DEFAULT_CONFIG)[Mode.LIVE_MICRO]

    assert len(gate.criteria) == 6
    assert {criterion.evidence_field for criterion in gate.criteria} == {
        "live_micro_days",
        "live_slippage_vs_paper_ppm",
        "live_brier_degradation_ppm",
        "reconciliation_halt_count",
        "invariant_violation_count",
        "operator_confirmation",
    }


def test_build_promotion_gates_sources_resolved_threshold_from_config() -> None:
    """`promotion_min_resolved` from `EvaluationConfig` drives the resolved-
    count criterion's threshold, not a hardcoded 300.
    """
    config = dataclasses.replace(_DEFAULT_CONFIG, promotion_min_resolved=301)
    gate = build_promotion_gates(config)[Mode.PAPER]

    criterion = _criterion_for(gate, "resolved_realtime_forecast_count", Comparison.GE)
    assert criterion.threshold == 301


def test_build_promotion_gates_sources_independent_groups_threshold_from_config() -> (
    None
):
    """`promotion_min_independent_event_groups` drives the independent-event-
    group-count criterion's threshold.
    """
    config = dataclasses.replace(
        _DEFAULT_CONFIG, promotion_min_independent_event_groups=101
    )
    gate = build_promotion_gates(config)[Mode.PAPER]

    criterion = _criterion_for(gate, "independent_event_group_count", Comparison.GE)
    assert criterion.threshold == 101


def test_build_promotion_gates_sources_brier_skill_threshold_from_config() -> None:
    """`brier_skill_required_ppm` drives the Brier-skill criterion's threshold."""
    config = dataclasses.replace(_DEFAULT_CONFIG, brier_skill_required_ppm=10_001)
    gate = build_promotion_gates(config)[Mode.PAPER]

    criterion = _criterion_for(gate, "brier_skill_ppm", Comparison.GE)
    assert criterion.threshold == 10_001


def test_build_gates_default_thresholds_match_config_defaults() -> None:
    """With the default `EvaluationConfig()`, the three config-sourced
    thresholds equal the config's own documented defaults (300/100/10000).
    """
    gate = build_promotion_gates(_DEFAULT_CONFIG)[Mode.PAPER]

    assert (
        _criterion_for(
            gate, "resolved_realtime_forecast_count", Comparison.GE
        ).threshold
        == 300
    )
    assert (
        _criterion_for(gate, "independent_event_group_count", Comparison.GE).threshold
        == 100
    )
    assert _criterion_for(gate, "brier_skill_ppm", Comparison.GE).threshold == 10_000


def test_mandatory_significance_criterion_threshold_is_hardcoded_not_config() -> None:
    """The CI-lower significance criterion's threshold is always 0 (`GT`),
    even when unrelated config thresholds are changed drastically.
    """
    config = dataclasses.replace(_DEFAULT_CONFIG, promotion_min_resolved=999_999)
    gate = build_promotion_gates(config)[Mode.PAPER]

    criterion = _criterion_for(gate, "brier_skill_ci_lower_ppm", Comparison.GT)
    assert criterion.threshold == 0


def test_research_to_paper_thresholds_are_module_constants_unaffected_by_config() -> (
    None
):
    """The RESEARCH->PAPER thresholds (50 forecasts, 14 error-free days) are
    module constants: unrelated `EvaluationConfig` changes never move them.
    """
    config = dataclasses.replace(
        _DEFAULT_CONFIG,
        promotion_min_resolved=999_999,
        promotion_min_independent_event_groups=999_999,
        brier_skill_required_ppm=999_999,
    )
    gate = build_promotion_gates(config)[Mode.RESEARCH]

    assert _criterion_for(gate, "forecast_count", Comparison.GE).threshold == 50
    assert (
        _criterion_for(gate, "days_without_unhandled_errors", Comparison.GE).threshold
        == 14
    )


# --- overridable: the sole significance-bypass flag, pinned per criterion -----

#: (criterion_id, source_mode, expected `overridable`) for every one of the
#: 20 criteria across all three gates -- pins that `paper_brier_skill_
#: significance` is the *only* overridable criterion. One row per criterion
#: (rather than one blanket assertion) so a flag flipped on any single
#: criterion pinpoints exactly which one broke.
_OVERRIDABLE_EXPECTATIONS: tuple[tuple[str, Mode, bool], ...] = (
    ("research_min_forecasts", Mode.RESEARCH, False),
    ("research_error_free_days", Mode.RESEARCH, False),
    ("research_adversarial_suite_green", Mode.RESEARCH, False),
    ("research_ledger_rebuild_verified", Mode.RESEARCH, False),
    ("paper_resolved_forecasts", Mode.PAPER, False),
    ("paper_independent_event_groups", Mode.PAPER, False),
    ("paper_brier_skill", Mode.PAPER, False),
    ("paper_brier_skill_significance", Mode.PAPER, True),
    ("paper_pnl_positive", Mode.PAPER, False),
    ("paper_window_days", Mode.PAPER, False),
    ("paper_max_drawdown", Mode.PAPER, False),
    ("paper_calibration_slope_low", Mode.PAPER, False),
    ("paper_calibration_slope_high", Mode.PAPER, False),
    ("paper_kernel_invariant_failures", Mode.PAPER, False),
    ("live_micro_days", Mode.LIVE_MICRO, False),
    ("live_micro_slippage", Mode.LIVE_MICRO, False),
    ("live_micro_brier_degradation", Mode.LIVE_MICRO, False),
    ("live_micro_reconciliation_halts", Mode.LIVE_MICRO, False),
    ("live_micro_invariant_violations", Mode.LIVE_MICRO, False),
    ("live_micro_operator_confirmation", Mode.LIVE_MICRO, False),
)


@pytest.mark.parametrize(
    ("criterion_id", "source_mode", "expected_overridable"),
    _OVERRIDABLE_EXPECTATIONS,
    ids=[row[0] for row in _OVERRIDABLE_EXPECTATIONS],
)
def test_criterion_overridable_flag_is_pinned(
    criterion_id: str, source_mode: Mode, expected_overridable: bool
) -> None:
    """Every criterion's `overridable` flag matches the pinned table above:
    `paper_brier_skill_significance` is `True`, every other criterion (all
    three gates) is `False`.
    """
    gate = build_promotion_gates(_DEFAULT_CONFIG)[source_mode]
    matches = [c for c in gate.criteria if c.criterion_id == criterion_id]
    assert len(matches) == 1, criterion_id

    assert matches[0].overridable is expected_overridable


def test_exactly_one_criterion_is_overridable_across_all_gates() -> None:
    """Exactly one criterion, across all three gates combined, is
    overridable -- guards against a second criterion being accidentally
    marked overridable alongside the pinned significance criterion.
    """
    gates = build_promotion_gates(_DEFAULT_CONFIG)
    overridable_ids = [
        criterion.criterion_id
        for gate in gates.values()
        for criterion in gate.criteria
        if criterion.overridable
    ]

    assert overridable_ids == ["paper_brier_skill_significance"]


# --- evaluate_promotion: sanity, no-short-circuit, both-sided boundaries -------


def test_default_gate_evidence_never_approves_any_gate() -> None:
    """`GateEvidence()` (all-zero/False) fails every criterion of all three
    gates, so none is ever approved by default.
    """
    gates = build_promotion_gates(_DEFAULT_CONFIG)

    for gate in gates.values():
        decision = evaluate_promotion(gate, GateEvidence())
        assert decision.approved is False, gate.source


@pytest.mark.parametrize(
    "source_mode", [Mode.RESEARCH, Mode.PAPER, Mode.LIVE_MICRO], ids=lambda m: m.name
)
def test_all_passing_evidence_approves_every_gate(source_mode: Mode) -> None:
    """The shared `_ALL_PASSING_EVIDENCE` snapshot approves each gate, with
    every one of its criteria individually passing.
    """
    gate = build_promotion_gates(_DEFAULT_CONFIG)[source_mode]

    decision = evaluate_promotion(gate, _ALL_PASSING_EVIDENCE)

    assert decision.approved is True
    assert decision.source is gate.source
    assert decision.target is gate.target
    assert len(decision.results) == len(gate.criteria)
    assert all(result.passed for result in decision.results)


def test_evaluate_promotion_yields_a_result_per_criterion_when_failing() -> None:
    """Failing evidence still yields one `CriterionResult` per criterion --
    `evaluate_promotion` never short-circuits on the first failure.
    """
    gate = build_promotion_gates(_DEFAULT_CONFIG)[Mode.PAPER]

    decision = evaluate_promotion(gate, GateEvidence())

    assert len(decision.results) == len(gate.criteria) == 10
    assert decision.approved is False


def test_ci_straddling_zero_fails_significance_despite_good_point() -> None:
    """A CI of (-2000, 11000) -- straddling zero -- fails the mandatory
    significance criterion even though the point estimate and every other
    field is otherwise passing; a positive point estimate is not enough.
    """
    gate = build_promotion_gates(_DEFAULT_CONFIG)[Mode.PAPER]
    evidence = dataclasses.replace(
        _ALL_PASSING_EVIDENCE,
        brier_skill_ci_lower_ppm=-2000,
        brier_skill_ci_upper_ppm=11_000,
    )

    decision = evaluate_promotion(gate, evidence)

    result = _result_for(gate, decision, "brier_skill_ci_lower_ppm", Comparison.GT)
    assert result.passed is False
    assert result.observed == -2000
    assert decision.approved is False


#: (label, source_mode, evidence_field, comparison, threshold, fail_value, pass_value)
#: Both-sided boundary table for every numeric criterion across all three
#: gates: `fail_value` must fail the criterion, `pass_value` must pass it, and
#: (mutation-resistance) no *other* criterion may flip in either evidence.
_NUMERIC_BOUNDARIES: tuple[tuple[str, Mode, str, Comparison, int, int, int], ...] = (
    (
        "research_forecast_count",
        Mode.RESEARCH,
        "forecast_count",
        Comparison.GE,
        50,
        49,
        50,
    ),
    (
        "research_error_free_days",
        Mode.RESEARCH,
        "days_without_unhandled_errors",
        Comparison.GE,
        14,
        13,
        14,
    ),
    (
        "paper_resolved_count",
        Mode.PAPER,
        "resolved_realtime_forecast_count",
        Comparison.GE,
        300,
        299,
        300,
    ),
    (
        "paper_independent_groups",
        Mode.PAPER,
        "independent_event_group_count",
        Comparison.GE,
        100,
        99,
        100,
    ),
    (
        "paper_brier_skill",
        Mode.PAPER,
        "brier_skill_ppm",
        Comparison.GE,
        10_000,
        9_999,
        10_000,
    ),
    (
        "paper_ci_lower_significance",
        Mode.PAPER,
        "brier_skill_ci_lower_ppm",
        Comparison.GT,
        0,
        0,
        1,
    ),
    (
        "paper_pnl_positive",
        Mode.PAPER,
        "paper_pnl_net_micro_usd",
        Comparison.GT,
        0,
        0,
        1,
    ),
    (
        "paper_window_days",
        Mode.PAPER,
        "paper_window_days",
        Comparison.GE,
        90,
        89,
        90,
    ),
    (
        "paper_max_drawdown",
        Mode.PAPER,
        "paper_max_drawdown_ppm",
        Comparison.LT,
        _PAPER_MAX_DRAWDOWN_THRESHOLD_PPM,
        _PAPER_MAX_DRAWDOWN_THRESHOLD_PPM,
        _PAPER_MAX_DRAWDOWN_THRESHOLD_PPM - 1,
    ),
    (
        "paper_calibration_low_band_edge",
        Mode.PAPER,
        "calibration_slope_ppm",
        Comparison.GE,
        _CALIBRATION_SLOPE_LOW_PPM,
        _CALIBRATION_SLOPE_LOW_PPM - 1,
        _CALIBRATION_SLOPE_LOW_PPM,
    ),
    (
        "paper_calibration_high_band_edge",
        Mode.PAPER,
        "calibration_slope_ppm",
        Comparison.LE,
        _CALIBRATION_SLOPE_HIGH_PPM,
        _CALIBRATION_SLOPE_HIGH_PPM + 1,
        _CALIBRATION_SLOPE_HIGH_PPM,
    ),
    (
        "paper_kernel_invariant_failures",
        Mode.PAPER,
        "kernel_invariant_failure_count",
        Comparison.EQ,
        0,
        1,
        0,
    ),
    (
        "live_micro_days",
        Mode.LIVE_MICRO,
        "live_micro_days",
        Comparison.GE,
        60,
        59,
        60,
    ),
    (
        "live_slippage_vs_paper",
        Mode.LIVE_MICRO,
        "live_slippage_vs_paper_ppm",
        Comparison.LE,
        _LIVE_SLIPPAGE_MAX_PPM,
        _LIVE_SLIPPAGE_MAX_PPM + 1,
        _LIVE_SLIPPAGE_MAX_PPM,
    ),
    (
        "live_brier_degradation",
        Mode.LIVE_MICRO,
        "live_brier_degradation_ppm",
        Comparison.LE,
        _LIVE_BRIER_DEGRADATION_MAX_PPM,
        _LIVE_BRIER_DEGRADATION_MAX_PPM + 1,
        _LIVE_BRIER_DEGRADATION_MAX_PPM,
    ),
    (
        "live_reconciliation_halts",
        Mode.LIVE_MICRO,
        "reconciliation_halt_count",
        Comparison.EQ,
        0,
        1,
        0,
    ),
    (
        "live_invariant_violations",
        Mode.LIVE_MICRO,
        "invariant_violation_count",
        Comparison.EQ,
        0,
        1,
        0,
    ),
)


@pytest.mark.parametrize(
    (
        "source_mode",
        "evidence_field",
        "comparison",
        "threshold",
        "fail_value",
        "pass_value",
    ),
    [row[1:] for row in _NUMERIC_BOUNDARIES],
    ids=[row[0] for row in _NUMERIC_BOUNDARIES],
)
def test_numeric_criterion_boundary_is_both_sided_and_isolated(
    source_mode: Mode,
    evidence_field: str,
    comparison: Comparison,
    threshold: int,
    fail_value: int,
    pass_value: int,
) -> None:
    """For every numeric criterion: `fail_value` fails it (and only it) while
    every other criterion still passes, and `pass_value` passes it (and only
    it flips) -- proving `evaluate_promotion` evaluates every criterion
    independently (no short-circuit) and the comparison/threshold are exact
    (kills off-by-one and comparison-operator mutants).
    """
    gate = build_promotion_gates(_DEFAULT_CONFIG)[source_mode]
    criterion = _criterion_for(gate, evidence_field, comparison)
    assert criterion.threshold == threshold

    failing_evidence = dataclasses.replace(
        _ALL_PASSING_EVIDENCE, **{evidence_field: fail_value}
    )
    passing_evidence = dataclasses.replace(
        _ALL_PASSING_EVIDENCE, **{evidence_field: pass_value}
    )

    fail_decision = evaluate_promotion(gate, failing_evidence)
    pass_decision = evaluate_promotion(gate, passing_evidence)

    fail_result = _result_for(gate, fail_decision, evidence_field, comparison)
    pass_result = _result_for(gate, pass_decision, evidence_field, comparison)

    assert fail_result.passed is False
    assert fail_result.observed == fail_value
    assert pass_result.passed is True
    assert pass_result.observed == pass_value

    assert fail_decision.approved is False
    assert pass_decision.approved is True

    for result in fail_decision.results:
        if result.criterion_id != fail_result.criterion_id:
            assert result.passed is True, f"unexpected extra failure: {result}"


#: (label, source_mode, evidence_field) for the three `IS_TRUE` criteria.
_BOOLEAN_CRITERIA: tuple[tuple[str, Mode, str], ...] = (
    (
        "research_adversarial_suite_green",
        Mode.RESEARCH,
        "adversarial_suite_green",
    ),
    ("research_ledger_rebuild_verified", Mode.RESEARCH, "ledger_rebuild_verified"),
    (
        "live_micro_operator_confirmation",
        Mode.LIVE_MICRO,
        "operator_confirmation",
    ),
)


@pytest.mark.parametrize(
    ("source_mode", "evidence_field"),
    [row[1:] for row in _BOOLEAN_CRITERIA],
    ids=[row[0] for row in _BOOLEAN_CRITERIA],
)
def test_is_true_criterion_boundary_is_both_sided_and_isolated(
    source_mode: Mode, evidence_field: str
) -> None:
    """Each `IS_TRUE` criterion fails on `False` (isolated) and passes on
    `True` (the shared baseline), proving both directions.
    """
    gate = build_promotion_gates(_DEFAULT_CONFIG)[source_mode]
    criterion = _criterion_for(gate, evidence_field, Comparison.IS_TRUE)
    assert criterion.comparison is Comparison.IS_TRUE

    failing_evidence = dataclasses.replace(
        _ALL_PASSING_EVIDENCE, **{evidence_field: False}
    )

    fail_decision = evaluate_promotion(gate, failing_evidence)
    pass_decision = evaluate_promotion(gate, _ALL_PASSING_EVIDENCE)

    fail_result = _result_for(gate, fail_decision, evidence_field, Comparison.IS_TRUE)
    pass_result = _result_for(gate, pass_decision, evidence_field, Comparison.IS_TRUE)

    assert fail_result.passed is False
    assert pass_result.passed is True
    assert fail_decision.approved is False
    assert pass_decision.approved is True

    for result in fail_decision.results:
        if result.criterion_id != fail_result.criterion_id:
            assert result.passed is True, f"unexpected extra failure: {result}"


def test_criterion_result_and_promotion_decision_are_frozen() -> None:
    """`CriterionResult` and `PromotionDecision` are immutable value objects."""
    result = CriterionResult(
        criterion_id="x", observed=1, threshold=1, comparison=Comparison.GE, passed=True
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.passed = False  # type: ignore[misc]

    decision = PromotionDecision(
        source=Mode.RESEARCH, target=Mode.PAPER, approved=True, results=(result,)
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.approved = False  # type: ignore[misc]


# --- Kernel integration: request_promotion --------------------------------------


def _kernel_at(
    mode: Mode,
    *,
    ceiling: Mode = Mode.LIVE,
    evaluation_config: EvaluationConfig | None = None,
) -> RiskKernel:
    """Build a `RiskKernel` parked at `mode`, ceilinged at `ceiling`.

    Args:
        mode: The starting operating mode.
        ceiling: The configured `mode_ceiling`.
        evaluation_config: Optional `EvaluationConfig` override.

    Returns:
        A `RiskKernel` wired to a fresh `InMemoryKernelLedgerWriter`.
    """
    machine = ModeStateMachine(mode_ceiling=ceiling, mode=mode)
    return RiskKernel(
        InMemoryKernelLedgerWriter(),
        mode_machine=machine,
        evaluation_config=evaluation_config,
    )


def test_request_promotion_ledgers_promotion_evaluated_on_approval_and_promotes() -> (
    None
):
    """An approved `request_promotion` ledgers exactly one `PromotionEvaluated`
    (approved=True) event and advances the mode one rung.
    """
    kernel = _kernel_at(Mode.RESEARCH, ceiling=Mode.LIVE)

    decision = kernel.request_promotion(_ALL_PASSING_EVIDENCE)

    assert decision.approved is True
    assert kernel.mode is Mode.PAPER
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(events) == 1
    event = events[0]
    assert event.component == "riskkernel"
    assert event.payload["source_mode"] == "RESEARCH"
    assert event.payload["target_mode"] == "PAPER"
    assert event.payload["approved"] is True
    assert event.payload["override_bypassed"] is False
    assert isinstance(event.payload["evidence"], dict)
    assert isinstance(event.payload["results"], list)
    assert len(event.payload["results"]) == 4


def test_request_promotion_ledgers_on_failure_and_leaves_mode_unchanged() -> None:
    """A rejected `request_promotion` still ledgers exactly one
    `PromotionEvaluated` (approved=False) event, and the mode never moves.
    """
    kernel = _kernel_at(Mode.RESEARCH, ceiling=Mode.LIVE)

    decision = kernel.request_promotion(GateEvidence())

    assert decision.approved is False
    assert kernel.mode is Mode.RESEARCH
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(events) == 1
    assert events[0].payload["approved"] is False
    assert events[0].payload["override_bypassed"] is False


@pytest.mark.parametrize(
    "blocked_mode",
    [Mode.PAUSED, Mode.HALT, Mode.KILLED, Mode.LIVE],
    ids=lambda m: m.name,
)
def test_request_promotion_from_off_ladder_or_live_raises_unledgered(
    blocked_mode: Mode,
) -> None:
    """From any safety mode or from LIVE (the top rung, off-ladder for
    *further* promotion), `request_promotion` raises `IllegalModeTransitionError`
    and never touches the ledger.
    """
    kernel = _kernel_at(blocked_mode, ceiling=Mode.LIVE)

    with pytest.raises(IllegalModeTransitionError):
        kernel.request_promotion(_ALL_PASSING_EVIDENCE)

    assert kernel.ledger_writer.events == []


def test_request_promotion_beyond_configured_ceiling_still_ledgers_before_raising() -> (
    None
):
    """When evidence approves the promotion but the configured ceiling blocks
    it, `PromotionEvaluated(approved=True)` is still ledgered *before*
    `ModeCeilingExceededError` propagates, and the mode is left unchanged.
    """
    kernel = _kernel_at(Mode.RESEARCH, ceiling=Mode.RESEARCH)

    with pytest.raises(ModeCeilingExceededError):
        kernel.request_promotion(_ALL_PASSING_EVIDENCE)

    assert kernel.mode is Mode.RESEARCH
    events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
    ]
    assert len(events) == 1
    assert events[0].payload["approved"] is True
    assert events[0].payload["override_bypassed"] is False


def test_tracer_code_all_passing_research_to_paper_promotes_via_for_testing() -> None:
    """End-to-end tracer: `RiskKernel.for_testing()` (default ceiling PAPER)
    plus all-passing synthetic evidence promotes RESEARCH -> PAPER and ledgers
    one approving `PromotionEvaluated`.
    """
    kernel = RiskKernel.for_testing()

    decision = kernel.request_promotion(_ALL_PASSING_EVIDENCE)

    assert decision.approved is True
    assert kernel.mode is Mode.PAPER
    approved_events = [
        event
        for event in kernel.ledger_writer.events
        if event.event_type == "PromotionEvaluated"
        and event.payload["approved"] is True
    ]
    assert len(approved_events) == 1


# --- PromotionEvaluated event: registry round trip ------------------------------


def test_promotion_evaluated_event_reconstructs_via_event_types_and_round_trips() -> (
    None
):
    """`PromotionEvaluated` derives the full `Event` contract and its payload
    round-trips through `EVENT_TYPES` + `canonical_json`, matching the
    `ConfigLoaded` pattern exactly.
    """
    event = PromotionEvaluated(
        component="riskkernel",
        source_mode="RESEARCH",
        target_mode="PAPER",
        approved=True,
        evidence={"forecast_count": 50},
        results=[{"criterion_id": "x", "passed": True}],
        override_bypassed=False,
    )

    assert event.event_type == "PromotionEvaluated"
    assert event.payload["override_bypassed"] is False
    envelope = json.loads(event.envelope_json)
    rebuilt_cls = EVENT_TYPES[event.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == event
    assert json.loads(canonical_json(event.payload)) == event.payload
