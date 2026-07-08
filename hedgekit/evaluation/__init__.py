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

Two metric slots remain deliberate stubs returning :data:`NOT_IMPLEMENTED`,
pending their own issues: the selection-track ``traded_vs_skipped_brier_delta``
and the execution-track ``fill_vs_model_slippage`` (#52).

Symbols are re-exported explicitly via ``__all__`` so ``mypy --strict``'s
no-implicit-reexport rule is satisfied.
"""

from __future__ import annotations

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
    registered_metrics,
)
from hedgekit.evaluation.report import (
    NO_EDGE_BANNER,
    POWER_ANALYSIS_SEED,
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

__all__ = [
    "BOOTSTRAP_REPLICATES",
    "ECE_BIN_COUNT",
    "EDGE_BUCKET_EDGES_PPM",
    "HEADLINE_SKILL_METRIC",
    "NOT_IMPLEMENTED",
    "NO_EDGE_BANNER",
    "POWER_ANALYSIS_SEED",
    "POWER_TARGET_N",
    "POWER_TARGET_PPM",
    "PRICE_BUCKET_COUNT",
    "PRICE_BUCKET_EDGES_PIPS",
    "UNIFORM_BASELINE_PPM",
    "Z_80_PPM",
    "Z_975_PPM",
    "BaselineForecast",
    "BaselineInputs",
    "BaselineSet",
    "BootstrapSample",
    "ClusteredCiResult",
    "EdgeBucket",
    "EvaluationInputs",
    "EvaluationReport",
    "FixtureForecast",
    "ForecastTerms",
    "MarketResolution",
    "MetricResult",
    "MetricSpec",
    "MetricValue",
    "NotImplementedSentinel",
    "ObservationWindow",
    "PowerAnalysis",
    "PriceBucket",
    "QuoteSnapshot",
    "ReliabilityBin",
    "ResolutionOutcome",
    "ResolutionStatus",
    "ResolutionTracker",
    "SettlementEvent",
    "SettlementEventType",
    "SplitMix64",
    "Track",
    "TrackReport",
    "baseline_inputs_from_fixture",
    "brier_skill",
    "brier_skill_ci",
    "calibration_intercept",
    "calibration_slope",
    "compute_baselines",
    "edge_bucket_report",
    "expected_calibration_error",
    "mean_brier",
    "mean_log_score",
    "power_analysis",
    "price_bucket_report",
    "registered_metrics",
    "reliability_diagram",
    "resolutions_from_fixture",
    "resolved_forecast_terms",
    "run_clustered_bootstrap",
    "run_evaluation",
    "settlement_events_from_fixture",
    "sharpness",
    "validate_confidence_ppm",
]
