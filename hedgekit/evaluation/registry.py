"""Metric registry and typed inputs for the evaluation harness (#49, RED).

This module is the *tracer-code* skeleton of SPEC-EPIC_07's three-track
evaluation harness. It fixes the vocabulary the later measurement issues fill
in -- the :class:`Track` and :class:`ObservationWindow` taxonomies, the typed
:class:`FixtureForecast` / :class:`EvaluationInputs` carriers, the
:class:`MetricSpec` shape, and the seed :func:`registered_metrics` catalogue --
while every metric's real arithmetic is still a stub.

Unimplemented metrics do not silently vanish: their ``compute`` returns the
:data:`NOT_IMPLEMENTED` sentinel (a distinct :class:`NotImplementedSentinel`
value, never ``None`` and never a stray ``int``), so the renderer can print the
literal ``NOT_IMPLEMENTED`` rather than omit the row. The one live stub,
``brier_skill_vs_executable_price``, returns the constant ``int`` ``0`` -- a
deliberate "no demonstrated edge" placeholder wired ahead of the real Brier
skill computation.

Successor issues fill the stubs in: the forecast-track Brier metrics (#50), the
selection-track traded-vs-skipped delta (#51), and the execution-track
fill-vs-model slippage (#52).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Final

    from hedgekit.evaluation.resolution import ResolutionOutcome
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
    """

    forecast_id: str
    market_ticker: str
    probability_ppm: ProbabilityPpm
    eligible_for_live: bool
    abstention_reason: str | None
    traded: bool
    baseline_executable_price_pips: int

    def __post_init__(self) -> None:
        """Validate the numeric invariants of the forecast row.

        Raises:
            TypeError: If ``baseline_executable_price_pips`` is a ``bool`` (an
                ``int`` subclass that must not masquerade as a price) or is not
                an ``int`` at all -- mirroring the ``_IntUnit`` guard in
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
    """

    forecasts: tuple[FixtureForecast, ...]
    resolutions: Mapping[str, ResolutionOutcome]


def _compute_brier(inputs: EvaluationInputs) -> MetricValue:
    """Forecast-track Brier score stub (real arithmetic lands in issue #50).

    Args:
        inputs: The evaluation inputs (ignored at this tracer-code stage).

    Returns:
        The :data:`NOT_IMPLEMENTED` sentinel until issue #50 wires the score.
    """
    del inputs  # Tracer stub: the Brier mean is computed in issue #50.
    return NOT_IMPLEMENTED


def _compute_brier_skill_vs_executable_price(inputs: EvaluationInputs) -> MetricValue:
    """Headline forecast-skill stub returning a constant "no edge" ``0``.

    Args:
        inputs: The evaluation inputs (ignored at this tracer-code stage).

    Returns:
        The constant ``int`` ``0`` -- a deliberate no-demonstrated-edge
        placeholder until issue #50 wires the real skill-vs-baseline score.
    """
    del inputs  # Tracer stub: constant no-edge 0 until issue #50 wires it.
    return 0


def _compute_traded_vs_skipped_brier_delta(inputs: EvaluationInputs) -> MetricValue:
    """Selection-track traded-vs-skipped Brier delta stub (issue #51).

    Args:
        inputs: The evaluation inputs (ignored at this tracer-code stage).

    Returns:
        The :data:`NOT_IMPLEMENTED` sentinel until issue #51 wires the delta.
    """
    del inputs  # Tracer stub: the selection-track delta lands in issue #51.
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


#: The registry key of the headline forecast-skill metric the renderer gates the
#: "no edge" banner on.
HEADLINE_SKILL_METRIC: Final[str] = "brier_skill_vs_executable_price"


def _seed_metric_specs() -> list[MetricSpec]:
    """Build the fixed list of seed :class:`MetricSpec`s for this skeleton.

    Returns:
        The four seed metric specifications, one per successor issue's target,
        spanning all three :class:`Track`s.
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
