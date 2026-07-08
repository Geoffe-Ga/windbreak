"""Three-track evaluation report and its loader (SPEC-EPIC_07, #49, RED).

Assembles the harness's user-facing artefact: an :class:`EvaluationReport` of
exactly three :class:`TrackReport`s (forecast, selection, execution), each
carrying one :class:`MetricResult` per metric registered in that track. The
report is built by :func:`run_evaluation`, which loads and validates a
known-answer JSON fixture, constructs typed :class:`EvaluationInputs`, and runs
every metric in the registry so no registered metric can be silently omitted.

The renderer speaks bluntly: an unimplemented metric prints the literal
``NOT_IMPLEMENTED``, and whenever the headline skill metric resolves to a
non-positive ``int`` the forecast section prints :data:`NO_EDGE_BANNER`
("NO EDGE DEMONSTRATED") rather than any hedging language. A ``NOT_IMPLEMENTED``
headline is "not measured yet", not "no edge", and never triggers the banner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from windbreak.evaluation.abstention import summarize_abstentions
from windbreak.evaluation.cohorts import (
    Cohort,
    UndefinedBrier,
    cohort_brier_table,
)
from windbreak.evaluation.power import power_analysis
from windbreak.evaluation.registry import (
    HEADLINE_SKILL_METRIC,
    EvaluationInputs,
    FixtureForecast,
    NotImplementedSentinel,
    Track,
    gate_evaluation_inputs,
    registered_metrics,
)
from windbreak.evaluation.resolution import (
    resolutions_from_fixture,
    settlement_events_from_fixture,
)
from windbreak.evaluation.temporal import (
    TemporalContext,
    deployment_sequence_from_fixture,
    resolution_sequences_from_events,
)
from windbreak.evaluation.windows import (
    HEADLINE_OBSERVATION_WINDOW,
    ObservationWindow,
)
from windbreak.numeric.types import ProbabilityPpm

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Any, Final

    from windbreak.evaluation.abstention import AbstentionSummary
    from windbreak.evaluation.cohorts import CohortBrier, CohortBrierValue
    from windbreak.evaluation.power import PowerAnalysis
    from windbreak.evaluation.registry import MetricValue
    from windbreak.evaluation.temporal import RejectionEvent

#: The blunt banner printed under the forecast track when the headline skill
#: metric shows no positive demonstrated edge.
NO_EDGE_BANNER: Final[str] = "NO EDGE DEMONSTRATED"

#: The blunt banner printed in the selection section when the traded-vs-skipped
#: Brier delta is negative -- the forecasts the strategy skipped scored better
#: than the ones it traded.
SKIPPED_OUTPERFORMED_BANNER: Final[str] = (
    "SKIPPED FORECASTS OUTPERFORMED TRADED FORECASTS"
)

#: The observation window the selection-bias cohort/abstention detail is
#: computed and labelled under (SPEC-EPIC_07 #53); the single headline window
#: shared with the forecast-track metrics, sourced from its canonical home in
#: :mod:`windbreak.evaluation.windows` so the two call sites cannot drift apart.
_SELECTION_WINDOW = HEADLINE_OBSERVATION_WINDOW

#: The fixed seed the report's power analysis runs under, so a report over a
#: given fixture is byte-identical across repeated runs (SPEC S3.5).
POWER_ANALYSIS_SEED = 20_240_607

#: JSON top-level key holding the list of forecast rows.
_FORECASTS_KEY = "forecasts"
#: JSON top-level key holding the list of resolution entries.
_RESOLUTIONS_KEY = "resolutions"
#: JSON top-level key holding the ordered settlement-event stream (#52 gating).
_SETTLEMENT_EVENTS_KEY = "settlement_events"
#: Render header introducing the rejection-ledger section, emitted only when the
#: report carries at least one temporal-integrity rejection (#52).
_REJECTIONS_HEADER = "== rejections =="
#: JSON field naming a forecast's probability, validated before construction.
_PROBABILITY_FIELD = "probability_ppm"
#: Threshold at or below which the headline skill metric shows no edge.
_NO_EDGE_CEILING = 0


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One metric's computed value for a single evaluation run.

    Attributes:
        name: The metric's registry name.
        window: The :class:`ObservationWindow` the metric observed.
        value: The computed value -- a ppm-scaled ``int`` or the sentinel.
    """

    name: str
    window: ObservationWindow
    value: MetricValue


@dataclass(frozen=True, slots=True)
class TrackReport:
    """All metric results reported under one track.

    Attributes:
        name: The track name, equal to the owning :class:`Track`'s value.
        metrics: The metric results in this track, in registry order.
    """

    name: str
    metrics: tuple[MetricResult, ...]


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """A full evaluation report: exactly one track report per :class:`Track`.

    Attributes:
        tracks: The three track reports, one per :class:`Track`, in order.
        power: The clustered-bootstrap power analysis, or ``None`` when the
            report was constructed without one (e.g. in renderer unit tests).
        rejections: The temporal-integrity rejection ledger (#52), in fixture
            order; empty when every forecast was admitted.
        cohorts: The per-cohort Brier table (#53), one row per
            :class:`~windbreak.evaluation.cohorts.Cohort`; empty when the report
            was constructed without selection-bias detail.
        abstentions: The abstention-wisdom summary (#53), or ``None`` when the
            report carries no abstention detail.
    """

    tracks: tuple[TrackReport, ...]
    power: PowerAnalysis | None = None
    rejections: tuple[RejectionEvent, ...] = ()
    cohorts: tuple[CohortBrier, ...] = ()
    abstentions: AbstentionSummary | None = None

    def __post_init__(self) -> None:
        """Validate that the report carries each track exactly once.

        Raises:
            ValueError: Unless ``tracks`` holds exactly the three
                :class:`Track` value names, each exactly once.
        """
        names = sorted(track.name for track in self.tracks)
        expected = sorted(member.value for member in Track)
        if names != expected:
            raise ValueError(
                f"EvaluationReport requires exactly one track per Track value "
                f"{expected}, got track names {names}"
            )

    def render_text(self) -> str:
        """Render the report as blunt plain text, one section per track.

        Returns:
            The rendered report: a section per track in fixed order, each with
            one line per metric (``name [window] = <int | NOT_IMPLEMENTED>``)
            and, under the forecast track, :data:`NO_EDGE_BANNER` when the
            headline skill metric shows no positive edge; a selection-bias detail
            section (one line per cohort, :data:`SKIPPED_OUTPERFORMED_BANNER` when
            the skipped cohort scored better, and an abstentions line) when the
            report carries cohort/abstention data; a trailing ``== power ==``
            section when a power analysis is present, and a trailing
            ``== rejections ==`` section when the temporal gate ledgered at least
            one rejection.
        """
        sections = [_render_track(track) for track in self.tracks]
        selection = _render_selection_detail(self)
        if selection is not None:
            sections.append(selection)
        if self.power is not None:
            sections.append(self.power.render_text())
        if self.rejections:
            sections.append(_render_rejections(self.rejections))
        return "\n".join(sections)


def _format_value(value: MetricValue) -> str:
    """Render a metric value as text.

    Args:
        value: The computed metric value.

    Returns:
        The literal ``NOT_IMPLEMENTED`` for the not-yet-built stub sentinel, the
        literal ``UNDEFINED`` for a metric that is built but genuinely undefined
        for these inputs (e.g. an empty cohort), else the integer's decimal
        string.
    """
    if isinstance(value, (NotImplementedSentinel, UndefinedBrier)):
        return value.name
    return str(value)


def _render_metric(result: MetricResult) -> str:
    """Render one metric result as a single ``name [window] = value`` line.

    Args:
        result: The metric result to render.

    Returns:
        The rendered line.
    """
    return f"{result.name} [{result.window.value}] = {_format_value(result.value)}"


def _shows_no_edge(value: MetricValue) -> bool:
    """Report whether a headline value demonstrates no positive edge.

    Args:
        value: The headline skill metric's value.

    Returns:
        ``True`` only for a real ``int`` measurement at or below the no-edge
        ceiling; the sentinel is "not measured yet", never "no edge".
    """
    return isinstance(value, int) and value <= _NO_EDGE_CEILING


def _no_edge_banner(track: TrackReport) -> str | None:
    """Return the no-edge banner for the forecast track when it applies.

    Args:
        track: The track being rendered.

    Returns:
        :data:`NO_EDGE_BANNER` if this is the forecast track and its headline
        skill metric shows no positive edge, else ``None``.
    """
    if track.name != Track.FORECAST.value:
        return None
    for metric in track.metrics:
        if metric.name == HEADLINE_SKILL_METRIC and _shows_no_edge(metric.value):
            return NO_EDGE_BANNER
    return None


def _render_track(track: TrackReport) -> str:
    """Render one track section: header, metric lines, and optional banner.

    Args:
        track: The track report to render.

    Returns:
        The rendered multi-line section for the track.
    """
    lines = [f"== {track.name} =="]
    lines.extend(_render_metric(metric) for metric in track.metrics)
    banner = _no_edge_banner(track)
    if banner is not None:
        lines.append(banner)
    return "\n".join(lines)


def _render_rejection(event: RejectionEvent) -> str:
    """Render one rejection as a single auditable ledger line.

    Args:
        event: The rejection event to render.

    Returns:
        A line carrying the immutable :data:`EVALUATION_RECORD_REJECTED` token
        followed by the record's identity, market, and reason.
    """
    return (
        f"{event.event_type} {event.forecast_id} "
        f"{event.market_ticker} {event.reason.value}"
    )


def _render_rejections(rejections: tuple[RejectionEvent, ...]) -> str:
    """Render the rejection-ledger section.

    Args:
        rejections: The non-empty rejection ledger, in fixture order.

    Returns:
        The rendered ``== rejections ==`` section, one line per rejection.
    """
    lines = [_REJECTIONS_HEADER]
    lines.extend(_render_rejection(event) for event in rejections)
    return "\n".join(lines)


def _format_cohort_brier(value: CohortBrierValue) -> str:
    """Render a cohort Brier value as text.

    Args:
        value: The cohort's mean Brier, or the ``UNDEFINED`` sentinel.

    Returns:
        The literal ``UNDEFINED`` for the sentinel, else the integer's decimal
        string.
    """
    if isinstance(value, UndefinedBrier):
        return value.name
    return str(value)


def _render_cohort(row: CohortBrier) -> str:
    """Render one cohort row as a ``cohort <name> [<window>] n=<k> brier=<v>`` line.

    Args:
        row: The cohort Brier row to render.

    Returns:
        The rendered line.
    """
    return (
        f"cohort {row.cohort.value} [{row.window.value}] "
        f"n={row.count} brier={_format_cohort_brier(row.brier_ppm)}"
    )


def _skipped_outperformed(cohorts: tuple[CohortBrier, ...]) -> bool:
    """Report whether the skipped cohort scored a strictly better Brier.

    Args:
        cohorts: The per-cohort Brier table.

    Returns:
        ``True`` iff both the ``TRADED`` and ``SKIPPED`` cohorts carry a real
        ``int`` Brier and ``SKIPPED - TRADED`` is negative (skipped scored
        lower, i.e. better).
    """
    brier_by_cohort = {row.cohort: row.brier_ppm for row in cohorts}
    traded = brier_by_cohort.get(Cohort.TRADED)
    skipped = brier_by_cohort.get(Cohort.SKIPPED)
    if isinstance(traded, int) and isinstance(skipped, int):
        return skipped - traded < 0
    return False


def _render_abstentions(summary: AbstentionSummary, *, window_value: str) -> str:
    """Render the abstention-summary line for the selection section.

    Args:
        summary: The abstention-wisdom summary to render.
        window_value: The observation-window label to tag the line with.

    Returns:
        The rendered ``abstentions [<window>] wise=.. unwise=.. forgone..`` line.
    """
    return (
        f"abstentions [{window_value}] wise={summary.wise_count} "
        f"unwise={summary.unwise_count} forgone_pnl_pips={summary.forgone_pnl_pips}"
    )


def _selection_window_value(report: EvaluationReport) -> str:
    """Return the observation-window label the selection section renders under.

    Args:
        report: The report being rendered.

    Returns:
        The window value of the report's cohort rows when present, else the
        default :data:`_SELECTION_WINDOW` label.
    """
    if report.cohorts:
        return report.cohorts[0].window.value
    return _SELECTION_WINDOW.value


def _render_selection_detail(report: EvaluationReport) -> str | None:
    """Render the selection-bias detail: cohort rows, banner, abstentions.

    Args:
        report: The report being rendered.

    Returns:
        The rendered selection-detail section, or ``None`` when the report
        carries neither cohorts nor an abstention summary.
    """
    summary = report.abstentions
    if not report.cohorts and summary is None:
        return None
    lines = [_render_cohort(row) for row in report.cohorts]
    if _skipped_outperformed(report.cohorts):
        lines.append(SKIPPED_OUTPERFORMED_BANNER)
    if summary is not None:
        lines.append(
            _render_abstentions(summary, window_value=_selection_window_value(report))
        )
    return "\n".join(lines)


def _probability_from_raw(raw: object) -> ProbabilityPpm:
    """Construct a :class:`ProbabilityPpm` from a raw fixture value.

    Args:
        raw: The raw ``probability_ppm`` value decoded from JSON.

    Returns:
        The constructed :class:`ProbabilityPpm`.

    Raises:
        TypeError: If ``raw`` is a ``bool`` (an ``int`` subclass that must not
            masquerade as a probability) or is not an ``int``; the message
            names the ``probability_ppm`` field.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(
            f"probability_ppm requires a non-bool int, got {type(raw).__name__}"
        )
    return ProbabilityPpm(raw)


def _forecast_from_entry(entry: Mapping[str, Any]) -> FixtureForecast:
    """Build a :class:`FixtureForecast` from one raw fixture forecast entry.

    Args:
        entry: The decoded forecast object from the fixture.

    Returns:
        The typed, validated forecast row.

    Raises:
        TypeError: If a numeric field carries a ``bool`` masquerading as an int.
        ValueError: If ``probability_ppm`` is outside the valid ppm range.
    """
    return FixtureForecast(
        forecast_id=entry["forecast_id"],
        market_ticker=entry["market_ticker"],
        probability_ppm=_probability_from_raw(entry[_PROBABILITY_FIELD]),
        eligible_for_live=entry["eligible_for_live"],
        abstention_reason=entry["abstention_reason"],
        traded=entry["traded"],
        baseline_executable_price_pips=entry["baseline_executable_price_pips"],
        correlation_group_id=entry.get("correlation_group_id"),
        created_sequence=entry.get("created_sequence"),
    )


def _require_top_level_keys(payload: Mapping[str, Any]) -> None:
    """Assert the fixture carries the required top-level keys.

    Args:
        payload: The decoded fixture payload.

    Raises:
        ValueError: If ``forecasts`` or ``resolutions`` is absent; the message
            names the missing key.
    """
    for key in (_FORECASTS_KEY, _RESOLUTIONS_KEY):
        if key not in payload:
            raise ValueError(f"fixture is missing required key: {key!r}")


def _temporal_context_from_payload(payload: Mapping[str, Any]) -> TemporalContext:
    """Build the temporal-integrity context from a fixture payload (#52).

    The deployment sequence comes from the fixture's ``mode_transitions`` block
    and the per-market resolution sequences are folded from its
    ``settlement_events`` stream, so the temporal gate has both coordinates it
    needs to classify every forecast.

    Args:
        payload: The decoded fixture payload, already forecast/resolution
            validated.

    Returns:
        The :class:`TemporalContext` for the run.

    Raises:
        ValueError: If the ``settlement_events`` block is absent (message names
            it) or the ``mode_transitions`` block is absent or empty (message
            names it), consistent with :func:`_require_top_level_keys`.
        TypeError: If a ``sequence_number`` is a ``bool`` or not an ``int``.
    """
    if _SETTLEMENT_EVENTS_KEY not in payload:
        raise ValueError(f"fixture is missing required key: {_SETTLEMENT_EVENTS_KEY!r}")
    resolution_sequences = resolution_sequences_from_events(
        settlement_events_from_fixture(payload)
    )
    deployment_sequence = deployment_sequence_from_fixture(payload)
    return TemporalContext(
        deployment_sequence=deployment_sequence,
        resolution_sequences=resolution_sequences,
    )


def _build_inputs(payload: Mapping[str, Any]) -> EvaluationInputs:
    """Build typed :class:`EvaluationInputs` from a validated payload.

    Forecast and resolution rows are constructed and validated first, so a
    malformed forecast (out-of-range probability, ``bool``-as-int) surfaces its
    own error before the temporal-block validation runs.

    Args:
        payload: The decoded fixture payload, already key-checked.

    Returns:
        The typed inputs for the evaluation run, carrying the temporal context.
    """
    forecasts = tuple(_forecast_from_entry(entry) for entry in payload[_FORECASTS_KEY])
    resolutions = resolutions_from_fixture(payload)
    temporal = _temporal_context_from_payload(payload)
    return EvaluationInputs(
        forecasts=forecasts, resolutions=resolutions, temporal=temporal
    )


def _build_tracks(inputs: EvaluationInputs) -> tuple[TrackReport, ...]:
    """Compute every registered metric and group results into track reports.

    Iterating the registry guarantees each registered metric is computed
    exactly once; iterating :class:`Track` fixes the section order and ensures
    all three tracks are present even when a track has no metrics.

    Args:
        inputs: The typed evaluation inputs.

    Returns:
        One :class:`TrackReport` per :class:`Track`, in enum order.
    """
    results: dict[Track, list[MetricResult]] = {track: [] for track in Track}
    for spec in registered_metrics().values():
        results[spec.track].append(
            MetricResult(
                name=spec.name,
                window=spec.window,
                value=spec.compute(inputs),
            )
        )
    return tuple(
        TrackReport(name=track.value, metrics=tuple(results[track])) for track in Track
    )


def run_evaluation(*, fixture_path: Path) -> EvaluationReport:
    """Load a known-answer fixture and build its three-track report.

    Args:
        fixture_path: Path to the known-answer JSON fixture.

    Returns:
        The assembled :class:`EvaluationReport`.

    Raises:
        ValueError: If a required top-level key is missing (including the #52
            ``mode_transitions`` / ``settlement_events`` blocks), a
            ``probability_ppm`` is out of range, or a resolution is malformed.
        TypeError: If a numeric field carries a ``bool`` masquerading as an int.
    """
    payload: Any = json.loads(fixture_path.read_text(encoding="utf-8"))
    _require_top_level_keys(payload)
    inputs = _build_inputs(payload)
    admitted, rejections = gate_evaluation_inputs(inputs)
    power = power_analysis(admitted, seed=POWER_ANALYSIS_SEED)
    cohorts = cohort_brier_table(admitted, window=_SELECTION_WINDOW)
    abstentions = summarize_abstentions(admitted)
    return EvaluationReport(
        tracks=_build_tracks(admitted),
        power=power,
        rejections=rejections,
        cohorts=cohorts,
        abstentions=abstentions,
    )
