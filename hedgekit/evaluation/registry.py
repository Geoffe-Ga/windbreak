"""Metric registry and typed inputs for the evaluation harness (#49, #51).

This module owns SPEC-EPIC_07's three-track evaluation vocabulary -- the
:class:`Track` and :class:`ObservationWindow` taxonomies, the typed
:class:`FixtureForecast` / :class:`EvaluationInputs` carriers, the
:class:`MetricSpec` shape, and the :func:`registered_metrics` catalogue -- and
wires each spec's ``compute`` to its real arithmetic.

As of issue #51 the seven forecast-track metrics (``brier``,
``brier_skill_vs_executable_price``, ``log_score``,
``expected_calibration_error``, ``calibration_slope``,
``calibration_intercept``, ``sharpness``) delegate to
:mod:`hedgekit.evaluation.metrics`. The registry->metrics import is a one-way
runtime edge with no cycle: ``metrics`` references this module's types only under
``TYPE_CHECKING``.

The two remaining metrics (``traded_vs_skipped_brier_delta``,
``fill_vs_model_slippage``) are still stubs whose ``compute`` returns the
:data:`NOT_IMPLEMENTED` sentinel (a distinct :class:`NotImplementedSentinel`
value, never ``None`` and never a stray ``int``) so the renderer prints the
literal ``NOT_IMPLEMENTED`` rather than omitting the row; the selection- and
execution-track work lands in issue #52 and beyond.
"""

from __future__ import annotations

import enum
import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING

import hedgekit.evaluation.metrics as metrics
from hedgekit.evaluation.temporal import enforce_temporal_integrity

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Final

    from hedgekit.evaluation.resolution import ResolutionOutcome
    from hedgekit.evaluation.temporal import RejectionEvent, TemporalContext
    from hedgekit.numeric.types import ProbabilityPpm

#: Inclusive lower bound of a valid ``probability_ppm`` (0.0 as parts-per-million).
_PROBABILITY_PPM_MIN = 0
#: Inclusive upper bound of a valid ``probability_ppm`` (1.0 as parts-per-million).
_PROBABILITY_PPM_MAX = 1_000_000


class Track(enum.Enum):
    """The three evaluation tracks a metric can belong to (SPEC-EPIC_07).

    Defined in report order -- forecast quality first, then selection quality,
    then execution quality -- so iterating the enum yields the fixed section
    order the renderer emits.
    """

    FORECAST = "forecast"
    SELECTION = "selection"
    EXECUTION = "execution"


class ObservationWindow(enum.Enum):
    """The sampling window a metric observes a forecast/market over.

    Different metrics score different slices of a market's life: the first
    forecast seen, the last snapshot before close, every daily snapshot, or only
    the snapshot that triggered a trade. The window is metadata carried on each
    :class:`MetricSpec` and rendered beside the metric's value.
    """

    FIRST_PER_MARKET = "first_per_market"
    LATEST_BEFORE_CLOSE = "latest_before_close"
    DAILY_SNAPSHOTS = "daily_snapshots"
    TRADE_TRIGGERING = "trade_triggering"


class NotImplementedSentinel(enum.Enum):
    """Single-valued sentinel marking a metric whose ``compute`` is a stub.

    A dedicated enum (rather than ``None`` or a magic string) keeps an
    unimplemented metric value nominally distinct from every real ``int``
    measurement, so ``value is NOT_IMPLEMENTED`` is an unambiguous test and the
    renderer can print the literal ``NOT_IMPLEMENTED`` for it.
    """

    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


#: The sentinel value returned by every not-yet-implemented metric ``compute``.
NOT_IMPLEMENTED: Final = NotImplementedSentinel.NOT_IMPLEMENTED

#: A computed metric value: a ppm-scaled ``int`` measurement, or the
#: :data:`NOT_IMPLEMENTED` sentinel when the metric's arithmetic is still a stub.
MetricValue = int | NotImplementedSentinel


@dataclass(frozen=True, slots=True)
class FixtureForecast:
    """One forecast row from a known-answer evaluation fixture.

    Carries the fields the evaluation metrics read for a single forecast: its
    identity, the market it named, its probability, whether it was eligible for
    and actually taken as a live trade, any abstention reason, and the baseline
    executable price the skill metric compares against.

    Attributes:
        forecast_id: Stable identifier of the forecast record.
        market_ticker: Ticker of the market this forecast is about.
        probability_ppm: Forecast probability in parts-per-million (0..1_000_000).
        eligible_for_live: Whether the forecast passed live-eligibility gates.
        abstention_reason: Why the forecast was skipped, or ``None`` if traded.
        traded: Whether a live trade was actually taken on this forecast.
        baseline_executable_price_pips: Reference executable price, in pips,
            the skill metric measures the forecast against.
        correlation_group_id: Identifier of the correlation cluster this
            forecast belongs to, or ``None`` when the market is its own
            singleton cluster (the clustered-bootstrap resampling unit, #51).
        created_sequence: The forecast's creation sequence on the append-only
            ledger, or ``None`` when it carried no recorded provenance (which
            the temporal gate treats as fail-closed pre-deployment, #52).
    """

    forecast_id: str
    market_ticker: str
    probability_ppm: ProbabilityPpm
    eligible_for_live: bool
    abstention_reason: str | None
    traded: bool
    baseline_executable_price_pips: int
    correlation_group_id: str | None = None
    created_sequence: int | None = None

    def __post_init__(self) -> None:
        """Validate the numeric invariants of the forecast row.

        Raises:
            TypeError: If ``baseline_executable_price_pips`` or a non-``None``
                ``created_sequence`` is a ``bool`` (an ``int`` subclass that
                must not masquerade as a number) or is not an ``int`` at all --
                mirroring the ``_IntUnit`` guard in
                :mod:`hedgekit.numeric.types`; the message names the field.
            ValueError: If ``probability_ppm`` falls outside the inclusive
                ``[0, 1_000_000]`` ppm range; the message names the field.
        """
        price = self.baseline_executable_price_pips
        if isinstance(price, bool) or not isinstance(price, int):
            raise TypeError(
                "baseline_executable_price_pips requires a non-bool int, "
                f"got {type(price).__name__}"
            )
        created = self.created_sequence
        if created is not None and (
            isinstance(created, bool) or not isinstance(created, int)
        ):
            raise TypeError(
                "created_sequence requires a non-bool int, "
                f"got {type(created).__name__}"
            )
        ppm = self.probability_ppm.value
        if not _PROBABILITY_PPM_MIN <= ppm <= _PROBABILITY_PPM_MAX:
            raise ValueError(
                "probability_ppm must be within "
                f"[{_PROBABILITY_PPM_MIN}, {_PROBABILITY_PPM_MAX}], got {ppm}"
            )


@dataclass(frozen=True, slots=True)
class EvaluationInputs:
    """The immutable inputs one evaluation run scores metrics over.

    Attributes:
        forecasts: The forecast rows to score, in fixture order.
        resolutions: Ground-truth outcomes keyed by ``market_ticker``.
        temporal: The temporal context the run's forecasts are gated against
            (#52), or ``None`` for a run that carries no temporal coordinates
            (e.g. a renderer or stub unit test over empty inputs).
    """

    forecasts: tuple[FixtureForecast, ...]
    resolutions: Mapping[str, ResolutionOutcome]
    temporal: TemporalContext | None = None


def gate_evaluation_inputs(
    inputs: EvaluationInputs,
) -> tuple[EvaluationInputs, tuple[RejectionEvent, ...]]:
    """Gate inputs for temporal integrity, returning admitted inputs + ledger.

    Runs :func:`~hedgekit.evaluation.temporal.enforce_temporal_integrity` and
    reconstructs an :class:`EvaluationInputs` carrying only the admitted
    forecasts while preserving the original ``resolutions`` and ``temporal``
    context, so a metric never observes a rejected record. This module owns the
    reconstruction seam because :mod:`hedgekit.evaluation.temporal` cannot
    import :class:`EvaluationInputs` without forming a cycle.

    Args:
        inputs: The raw evaluation inputs to gate.

    Returns:
        A ``(admitted_inputs, rejections)`` pair: the inputs narrowed to the
        admitted forecasts, and the rejection ledger in fixture order.

    Raises:
        ValueError: If ``inputs.temporal`` is ``None`` while forecasts are
            present (propagated from the gate; there is no silent skip).
    """
    result = enforce_temporal_integrity(inputs)
    admitted = EvaluationInputs(
        forecasts=result.admitted_forecasts,
        resolutions=inputs.resolutions,
        temporal=inputs.temporal,
    )
    return admitted, result.rejections


#: The observation window every forecast-track metric is scored over (S13.4);
#: the compute adapters thread it explicitly into their metric call.
_FORECAST_WINDOW = ObservationWindow.LATEST_BEFORE_CLOSE


def _compute_brier(inputs: EvaluationInputs) -> MetricValue:
    """Compute the forecast-track mean Brier score, in ppm.

    Args:
        inputs: The evaluation inputs to score.

    Returns:
        The mean Brier score delegated to :func:`hedgekit.evaluation.metrics`.
    """
    return metrics.mean_brier(inputs, window=_FORECAST_WINDOW)


def _compute_brier_skill_vs_executable_price(inputs: EvaluationInputs) -> MetricValue:
    """Compute the headline Brier skill versus the executable-price baseline.

    Args:
        inputs: The evaluation inputs to score.

    Returns:
        The Brier skill in ppm delegated to :func:`hedgekit.evaluation.metrics`.
    """
    return metrics.brier_skill(inputs, window=_FORECAST_WINDOW)


def _compute_log_score(inputs: EvaluationInputs) -> MetricValue:
    """Compute the forecast-track mean logarithmic score, in micro-nats.

    Args:
        inputs: The evaluation inputs to score.

    Returns:
        The mean log score delegated to :func:`hedgekit.evaluation.metrics`.
    """
    return metrics.mean_log_score(inputs, window=_FORECAST_WINDOW)


def _compute_expected_calibration_error(inputs: EvaluationInputs) -> MetricValue:
    """Compute the forecast-track expected calibration error, in ppm.

    Args:
        inputs: The evaluation inputs to score.

    Returns:
        The ECE delegated to :func:`hedgekit.evaluation.metrics`.
    """
    return metrics.expected_calibration_error(inputs, window=_FORECAST_WINDOW)


def _compute_calibration_slope(inputs: EvaluationInputs) -> MetricValue:
    """Compute the forecast-track calibration slope, in ppm.

    Args:
        inputs: The evaluation inputs to score.

    Returns:
        The calibration slope delegated to :func:`hedgekit.evaluation.metrics`.
    """
    return metrics.calibration_slope(inputs, window=_FORECAST_WINDOW)


def _compute_calibration_intercept(inputs: EvaluationInputs) -> MetricValue:
    """Compute the forecast-track calibration intercept, in ppm.

    Args:
        inputs: The evaluation inputs to score.

    Returns:
        The calibration intercept delegated to
        :func:`hedgekit.evaluation.metrics`.
    """
    return metrics.calibration_intercept(inputs, window=_FORECAST_WINDOW)


def _compute_sharpness(inputs: EvaluationInputs) -> MetricValue:
    """Compute the forecast-track sharpness (forecast variance), in ppm.

    Args:
        inputs: The evaluation inputs to score.

    Returns:
        The sharpness delegated to :func:`hedgekit.evaluation.metrics`.
    """
    return metrics.sharpness(inputs, window=_FORECAST_WINDOW)


def _compute_traded_vs_skipped_brier_delta(inputs: EvaluationInputs) -> MetricValue:
    """Selection-track traded-vs-skipped Brier delta stub (issue #52).

    Args:
        inputs: The evaluation inputs (ignored at this tracer-code stage).

    Returns:
        The :data:`NOT_IMPLEMENTED` sentinel until issue #52 wires the delta.
    """
    del inputs  # Tracer stub: the selection-track delta lands in issue #52.
    return NOT_IMPLEMENTED


def _compute_fill_vs_model_slippage(inputs: EvaluationInputs) -> MetricValue:
    """Execution-track fill-vs-model slippage stub (issue #52).

    Args:
        inputs: The evaluation inputs (ignored at this tracer-code stage).

    Returns:
        The :data:`NOT_IMPLEMENTED` sentinel until issue #52 wires the slippage.
    """
    del inputs  # Tracer stub: the execution-track slippage lands in issue #52.
    return NOT_IMPLEMENTED


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """The static definition of one evaluation metric.

    Attributes:
        name: Unique metric name, used as its registry key and render label.
        track: The :class:`Track` this metric reports under.
        window: The :class:`ObservationWindow` the metric observes.
        compute: Callable turning :class:`EvaluationInputs` into a
            :data:`MetricValue` (an ``int`` measurement or the sentinel).
    """

    name: str
    track: Track
    window: ObservationWindow
    compute: Callable[[EvaluationInputs], MetricValue]

    def __post_init__(self) -> None:
        """Wrap ``compute`` so every call is temporally gated at the choke point.

        The original ``compute`` is replaced with a wrapper that first routes
        its inputs through :func:`gate_evaluation_inputs` and only ever calls
        the original with the *admitted* inputs. This is the single, mandatory
        choke point: there is no ungated call path and no opt-out, so no
        ``MetricSpec`` -- registered or freshly constructed -- can observe a
        rejected record. Re-gating already-admitted inputs is idempotent, so the
        wrap is safe to apply even when the caller has pre-gated.
        """
        original = self.compute

        @functools.wraps(original)
        def gated_compute(inputs: EvaluationInputs) -> MetricValue:
            """Gate inputs then delegate to the original ``compute``.

            Args:
                inputs: The raw evaluation inputs handed to the metric.

            Returns:
                The original metric's value over the temporally-admitted inputs.
            """
            admitted, _ = gate_evaluation_inputs(inputs)
            return original(admitted)

        object.__setattr__(self, "compute", gated_compute)


#: The registry key of the headline forecast-skill metric the renderer gates the
#: "no edge" banner on.
HEADLINE_SKILL_METRIC: Final[str] = "brier_skill_vs_executable_price"


def _seed_metric_specs() -> list[MetricSpec]:
    """Build the fixed list of seed :class:`MetricSpec`s for the harness.

    Issue #51 turns ``brier`` and ``brier_skill_vs_executable_price`` into real
    computations and adds five more forecast-track metrics (``log_score``,
    ``expected_calibration_error``, ``calibration_slope``,
    ``calibration_intercept``, ``sharpness``); ``traded_vs_skipped_brier_delta``
    and ``fill_vs_model_slippage`` remain stubs (issues #52 and beyond).

    Returns:
        The nine metric specifications, spanning all three :class:`Track`s.
    """
    return [
        MetricSpec(
            name="brier",
            track=Track.FORECAST,
            window=ObservationWindow.LATEST_BEFORE_CLOSE,
            compute=_compute_brier,
        ),
        MetricSpec(
            name=HEADLINE_SKILL_METRIC,
            track=Track.FORECAST,
            window=ObservationWindow.LATEST_BEFORE_CLOSE,
            compute=_compute_brier_skill_vs_executable_price,
        ),
        MetricSpec(
            name="log_score",
            track=Track.FORECAST,
            window=ObservationWindow.LATEST_BEFORE_CLOSE,
            compute=_compute_log_score,
        ),
        MetricSpec(
            name="expected_calibration_error",
            track=Track.FORECAST,
            window=ObservationWindow.LATEST_BEFORE_CLOSE,
            compute=_compute_expected_calibration_error,
        ),
        MetricSpec(
            name="calibration_slope",
            track=Track.FORECAST,
            window=ObservationWindow.LATEST_BEFORE_CLOSE,
            compute=_compute_calibration_slope,
        ),
        MetricSpec(
            name="calibration_intercept",
            track=Track.FORECAST,
            window=ObservationWindow.LATEST_BEFORE_CLOSE,
            compute=_compute_calibration_intercept,
        ),
        MetricSpec(
            name="sharpness",
            track=Track.FORECAST,
            window=ObservationWindow.LATEST_BEFORE_CLOSE,
            compute=_compute_sharpness,
        ),
        MetricSpec(
            name="traded_vs_skipped_brier_delta",
            track=Track.SELECTION,
            window=ObservationWindow.TRADE_TRIGGERING,
            compute=_compute_traded_vs_skipped_brier_delta,
        ),
        MetricSpec(
            name="fill_vs_model_slippage",
            track=Track.EXECUTION,
            window=ObservationWindow.TRADE_TRIGGERING,
            compute=_compute_fill_vs_model_slippage,
        ),
    ]


def registered_metrics() -> Mapping[str, MetricSpec]:
    """Return the seed metric catalogue keyed by unique metric name.

    Returns:
        A mapping from each metric's ``name`` to its :class:`MetricSpec`.

    Raises:
        ValueError: If two specs share a ``name`` -- a construction-time
            invariant guarding against a silent drop that a naive
            list-to-dict conversion would hide.
    """
    registry: dict[str, MetricSpec] = {}
    for spec in _seed_metric_specs():
        if spec.name in registry:
            raise ValueError(f"duplicate metric name in registry: {spec.name!r}")
        registry[spec.name] = spec
    return registry
