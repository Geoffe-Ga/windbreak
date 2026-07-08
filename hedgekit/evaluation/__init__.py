"""Three-track evaluation harness (SPEC-EPIC_07).

This package pins the full public shape of the evaluation harness -- the
:class:`Track` / :class:`ObservationWindow` taxonomies, the typed
:class:`FixtureForecast` / :class:`EvaluationInputs` carriers, the
:class:`MetricSpec` registry, and the :class:`EvaluationReport` renderer -- and
carries the forecast-track statistical machinery to completion.

The forecast track is fully measured: the reference baselines (#50) and, in
#51, the real SPEC §13.5 statistics -- Brier, log score, Brier skill score,
expected calibration error, calibration slope/intercept, reliability-diagram
data, sharpness, and per-price-bucket / edge-bucket calibration and PnL -- plus
a cluster bootstrap over event/correlation groups (:mod:`.bootstrap`) and a
power analysis at ``N=300`` (:mod:`.power`) rendered into the report. All of it
is exact scaled-integer / :class:`fractions.Fraction` arithmetic with a seeded,
byte-identical PRNG (SPEC §3.5) -- no float anywhere on a value path.

The selection track is measured in #53: forecasts are partitioned into
selection-bias :class:`Cohort`s with per-cohort Brier scores, the headline
traded-vs-skipped Brier delta, and counterfactual abstention-wisdom scoring, all
rendered into the report. One metric slot remains a deliberate stub returning
:data:`NOT_IMPLEMENTED`, pending its own issue: the execution-track
``fill_vs_model_slippage``.

Symbols are re-exported explicitly via ``__all__`` so ``mypy --strict``'s
no-implicit-reexport rule is satisfied.
"""

from __future__ import annotations

from hedgekit.evaluation.abstention import (
    AbstentionScore,
    AbstentionSummary,
    AbstentionVerdict,
    score_abstentions,
    summarize_abstentions,
)
from hedgekit.evaluation.baselines import (
    UNIFORM_BASELINE_PPM,
    BaselineForecast,
    BaselineInputs,
    BaselineSet,
    QuoteSnapshot,
    baseline_inputs_from_fixture,
    compute_baselines,
)
from hedgekit.evaluation.bootstrap import (
    BOOTSTRAP_REPLICATES,
    BootstrapSample,
    ClusteredCiResult,
    SplitMix64,
    brier_skill_ci,
    run_clustered_bootstrap,
    validate_confidence_ppm,
)
from hedgekit.evaluation.cohorts import (
    ABOVE_THRESHOLD_MIN_EDGE_PPM,
    CATEGORY_EXCLUSION_REASONS,
    LIQUIDITY_EXCLUSION_REASONS,
    UNDEFINED,
    Cohort,
    CohortBrier,
    assign_cohorts,
    cohort_brier_table,
    mean_brier_over,
    traded_vs_skipped_brier_delta,
)
from hedgekit.evaluation.metrics import (
    ECE_BIN_COUNT,
    EDGE_BUCKET_EDGES_PPM,
    PRICE_BUCKET_COUNT,
    PRICE_BUCKET_EDGES_PIPS,
    EdgeBucket,
    ForecastTerms,
    PriceBucket,
    ReliabilityBin,
    brier_skill,
    calibration_intercept,
    calibration_slope,
    edge_bucket_report,
    expected_calibration_error,
    mean_brier,
    mean_log_score,
    price_bucket_report,
    reliability_diagram,
    resolved_forecast_terms,
    sharpness,
)
from hedgekit.evaluation.power import (
    POWER_TARGET_N,
    POWER_TARGET_PPM,
    Z_80_PPM,
    Z_975_PPM,
    PowerAnalysis,
    power_analysis,
)
from hedgekit.evaluation.registry import (
    HEADLINE_SKILL_METRIC,
    NOT_IMPLEMENTED,
    EvaluationInputs,
    FixtureForecast,
    MetricSpec,
    MetricValue,
    NotImplementedSentinel,
    ObservationWindow,
    Track,
    gate_evaluation_inputs,
    registered_metrics,
)
from hedgekit.evaluation.report import (
    NO_EDGE_BANNER,
    POWER_ANALYSIS_SEED,
    SKIPPED_OUTPERFORMED_BANNER,
    EvaluationReport,
    MetricResult,
    TrackReport,
    run_evaluation,
)
from hedgekit.evaluation.resolution import (
    MarketResolution,
    ResolutionOutcome,
    ResolutionStatus,
    ResolutionTracker,
    SettlementEvent,
    SettlementEventType,
    resolutions_from_fixture,
    settlement_events_from_fixture,
)
from hedgekit.evaluation.temporal import (
    EVALUATION_RECORD_REJECTED,
    RejectionEvent,
    RejectionReason,
    TemporalContext,
    TemporalGateResult,
    deployment_sequence_from_fixture,
    enforce_temporal_integrity,
    resolution_sequences_from_events,
)
from hedgekit.evaluation.windows import (
    MixedObservationWindowError,
    WindowedForecasts,
    combine,
    resolve_window,
)

__all__ = [
    "ABOVE_THRESHOLD_MIN_EDGE_PPM",
    "BOOTSTRAP_REPLICATES",
    "CATEGORY_EXCLUSION_REASONS",
    "ECE_BIN_COUNT",
    "EDGE_BUCKET_EDGES_PPM",
    "EVALUATION_RECORD_REJECTED",
    "HEADLINE_SKILL_METRIC",
    "LIQUIDITY_EXCLUSION_REASONS",
    "NOT_IMPLEMENTED",
    "NO_EDGE_BANNER",
    "POWER_ANALYSIS_SEED",
    "POWER_TARGET_N",
    "POWER_TARGET_PPM",
    "PRICE_BUCKET_COUNT",
    "PRICE_BUCKET_EDGES_PIPS",
    "SKIPPED_OUTPERFORMED_BANNER",
    "UNDEFINED",
    "UNIFORM_BASELINE_PPM",
    "Z_80_PPM",
    "Z_975_PPM",
    "AbstentionScore",
    "AbstentionSummary",
    "AbstentionVerdict",
    "BaselineForecast",
    "BaselineInputs",
    "BaselineSet",
    "BootstrapSample",
    "ClusteredCiResult",
    "Cohort",
    "CohortBrier",
    "EdgeBucket",
    "EvaluationInputs",
    "EvaluationReport",
    "FixtureForecast",
    "ForecastTerms",
    "MarketResolution",
    "MetricResult",
    "MetricSpec",
    "MetricValue",
    "MixedObservationWindowError",
    "NotImplementedSentinel",
    "ObservationWindow",
    "PowerAnalysis",
    "PriceBucket",
    "QuoteSnapshot",
    "RejectionEvent",
    "RejectionReason",
    "ReliabilityBin",
    "ResolutionOutcome",
    "ResolutionStatus",
    "ResolutionTracker",
    "SettlementEvent",
    "SettlementEventType",
    "SplitMix64",
    "TemporalContext",
    "TemporalGateResult",
    "Track",
    "TrackReport",
    "WindowedForecasts",
    "assign_cohorts",
    "baseline_inputs_from_fixture",
    "brier_skill",
    "brier_skill_ci",
    "calibration_intercept",
    "calibration_slope",
    "cohort_brier_table",
    "combine",
    "compute_baselines",
    "deployment_sequence_from_fixture",
    "edge_bucket_report",
    "enforce_temporal_integrity",
    "expected_calibration_error",
    "gate_evaluation_inputs",
    "mean_brier",
    "mean_brier_over",
    "mean_log_score",
    "power_analysis",
    "price_bucket_report",
    "registered_metrics",
    "reliability_diagram",
    "resolution_sequences_from_events",
    "resolutions_from_fixture",
    "resolve_window",
    "resolved_forecast_terms",
    "run_clustered_bootstrap",
    "run_evaluation",
    "score_abstentions",
    "settlement_events_from_fixture",
    "sharpness",
    "summarize_abstentions",
    "traded_vs_skipped_brier_delta",
    "validate_confidence_ppm",
]
