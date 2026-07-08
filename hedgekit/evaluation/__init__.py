"""Three-track evaluation harness -- tracer-code skeleton (SPEC-EPIC_07, #49).

This package is the RED-first skeleton of the evaluation harness: it pins the
full public shape -- the :class:`Track` / :class:`ObservationWindow` taxonomies,
the typed :class:`FixtureForecast` / :class:`EvaluationInputs` carriers, the
:class:`MetricSpec` registry, and the :class:`EvaluationReport` renderer -- while
every metric's real arithmetic is still a stub returning either the
:data:`NOT_IMPLEMENTED` sentinel or a single constant "no edge" ``0``.

The measurement work is filled in by the successor issues: the forecast-track
Brier metrics (#50), the selection-track traded-vs-skipped delta (#51), and the
execution-track fill-vs-model slippage (#52).

Symbols are re-exported explicitly via ``__all__`` so ``mypy --strict``'s
no-implicit-reexport rule is satisfied.
"""

from __future__ import annotations

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
    EvaluationReport,
    MetricResult,
    TrackReport,
    run_evaluation,
)
from hedgekit.evaluation.resolution import ResolutionOutcome, resolutions_from_fixture

__all__ = [
    "HEADLINE_SKILL_METRIC",
    "NOT_IMPLEMENTED",
    "NO_EDGE_BANNER",
    "EvaluationInputs",
    "EvaluationReport",
    "FixtureForecast",
    "MetricResult",
    "MetricSpec",
    "MetricValue",
    "NotImplementedSentinel",
    "ObservationWindow",
    "ResolutionOutcome",
    "Track",
    "TrackReport",
    "registered_metrics",
    "resolutions_from_fixture",
    "run_evaluation",
]
