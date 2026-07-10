"""Selection-bias cohorts and per-cohort Brier scoring (SPEC-EPIC_07, #53).

Selection bias is the gap between how a strategy *scores* forecasts and which
forecasts it actually *trades*: a forecaster can look skilled overall yet trade
exactly the subset it is worst at. This module makes that gap measurable by
partitioning forecasts into overlapping :class:`Cohort`s and reporting each
cohort's mean Brier score, plus the headline ``SKIPPED - TRADED`` delta whose
sign says whether the trades the strategy skipped would have scored better than
the ones it took.

Runtime dependencies point strictly left in the package topo order: this module
imports :mod:`windbreak.evaluation.metrics` and
:mod:`windbreak.evaluation.windows` at runtime and references
:mod:`windbreak.evaluation.registry` types only under
:data:`typing.TYPE_CHECKING`, narrowing an :class:`EvaluationInputs` to a cohort
via :func:`dataclasses.replace` rather than importing the registry.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from windbreak.evaluation.metrics import BASELINE_PPM_PER_PIP, mean_brier
from windbreak.evaluation.windows import combine, resolve_window

if TYPE_CHECKING:
    from collections.abc import Iterable

    from windbreak.evaluation.registry import EvaluationInputs, FixtureForecast
    from windbreak.evaluation.windows import ObservationWindow, WindowedForecasts

#: Abstention reasons that classify a skipped forecast as excluded for
#: insufficient liquidity rather than as a discretionary abstention.
LIQUIDITY_EXCLUSION_REASONS = frozenset({"low_liquidity"})
#: Abstention reasons that classify a skipped forecast as excluded because its
#: market category is off-limits rather than as a discretionary abstention.
CATEGORY_EXCLUSION_REASONS = frozenset({"excluded_category"})
#: Every reason that marks a *structural* exclusion (liquidity or category), so
#: a discretionary ``ABSTAINED`` classification can exclude them.
_EXCLUSION_REASONS = LIQUIDITY_EXCLUSION_REASONS | CATEGORY_EXCLUSION_REASONS

#: Minimum absolute gross edge, in ppm, for the ``ABOVE_THRESHOLD`` cohort: the
#: two-sided gross-edge proxy for SPEC §16 ``risk.min_net_edge_ppm``.
ABOVE_THRESHOLD_MIN_EDGE_PPM = 30_000


class Cohort(enum.Enum):
    """A selection-bias partition a forecast can belong to (overlapping).

    A forecast is always in ``ALL`` and in exactly one of ``TRADED`` /
    ``SKIPPED``; the remaining members are orthogonal predicates a forecast may
    additionally satisfy. Defined in report order so iterating the enum yields
    the fixed row order :func:`cohort_brier_table` emits.
    """

    ALL = "all"
    TRADED = "traded"
    SKIPPED = "skipped"
    ABOVE_THRESHOLD = "above_threshold"
    ABSTAINED = "abstained"
    EXCLUDED_BY_LIQUIDITY = "excluded_by_liquidity"
    EXCLUDED_BY_CATEGORY = "excluded_by_category"


class UndefinedBrier(enum.Enum):
    """Single-valued sentinel for a cohort with no resolved records.

    A dedicated enum (rather than ``None`` or a magic number) keeps an
    undefined cohort Brier nominally distinct from every real ppm ``int``, so
    ``brier_ppm is UNDEFINED`` is an unambiguous test and the renderer can print
    the literal ``UNDEFINED`` for it -- mirroring
    :class:`~windbreak.evaluation.registry.NotImplementedSentinel`.
    """

    UNDEFINED = "UNDEFINED"


#: The sentinel a :class:`CohortBrier` carries for an empty/unresolved cohort.
UNDEFINED = UndefinedBrier.UNDEFINED


class EmptyCohortError(ValueError):
    """Raised when a scalar cohort metric is asked to score an empty cohort.

    A dedicated :class:`ValueError` subclass (rather than a bare ``ValueError``)
    lets a caller catch *only* the "cohort has no resolved records" case and
    degrade it to the :data:`UNDEFINED` sentinel, while any other ``ValueError``
    from genuinely invalid inputs still propagates distinctly. It is a
    ``ValueError`` subclass so existing ``pytest.raises(ValueError)`` callers and
    the documented ``Raises: ValueError`` contract remain satisfied.
    """


#: A cohort Brier value: a ppm-scaled ``int`` mean, or the :data:`UNDEFINED`
#: sentinel when the cohort has no resolved records.
CohortBrierValue = int | UndefinedBrier


def _is_above_threshold(forecast: FixtureForecast) -> bool:
    """Report whether a forecast's absolute gross edge clears the threshold.

    Args:
        forecast: The forecast row to classify.

    Returns:
        ``True`` iff ``|probability_ppm - baseline_ppm|`` is at least
        :data:`ABOVE_THRESHOLD_MIN_EDGE_PPM`, where ``baseline_ppm`` is the
        executable price lifted to ppm.
    """
    baseline_ppm = forecast.baseline_executable_price_pips * BASELINE_PPM_PER_PIP
    edge_ppm = abs(forecast.probability_ppm.value - baseline_ppm)
    return edge_ppm >= ABOVE_THRESHOLD_MIN_EDGE_PPM


def _is_abstained(forecast: FixtureForecast) -> bool:
    """Report whether a skipped forecast is a discretionary abstention.

    Args:
        forecast: The forecast row to classify.

    Returns:
        ``True`` iff the forecast was not traded, was found ineligible for live,
        carries an abstention reason, and that reason is not a structural
        liquidity/category exclusion.
    """
    reason = forecast.abstention_reason
    return (
        not forecast.traded
        and not forecast.eligible_for_live
        and reason is not None
        and reason not in _EXCLUSION_REASONS
    )


def _exclusion_cohorts(reason: str | None) -> set[Cohort]:
    """Return the structural-exclusion cohorts an abstention reason implies.

    Args:
        reason: The forecast's abstention reason, or ``None``.

    Returns:
        ``{EXCLUDED_BY_LIQUIDITY}`` and/or ``{EXCLUDED_BY_CATEGORY}`` per the
        reason's exclusion sets; an empty set for a non-exclusion reason.
    """
    cohorts: set[Cohort] = set()
    if reason in LIQUIDITY_EXCLUSION_REASONS:
        cohorts.add(Cohort.EXCLUDED_BY_LIQUIDITY)
    if reason in CATEGORY_EXCLUSION_REASONS:
        cohorts.add(Cohort.EXCLUDED_BY_CATEGORY)
    return cohorts


def assign_cohorts(forecast: FixtureForecast) -> frozenset[Cohort]:
    """Return every :class:`Cohort` a single forecast belongs to.

    Args:
        forecast: The forecast row to classify.

    Returns:
        The forecast's cohort membership. ``ALL`` is always present and exactly
        one of ``TRADED`` / ``SKIPPED`` is present; the remaining members are
        added per their orthogonal predicates.
    """
    cohorts: set[Cohort] = {Cohort.ALL}
    cohorts.add(Cohort.TRADED if forecast.traded else Cohort.SKIPPED)
    if _is_above_threshold(forecast):
        cohorts.add(Cohort.ABOVE_THRESHOLD)
    cohorts |= _exclusion_cohorts(forecast.abstention_reason)
    if _is_abstained(forecast):
        cohorts.add(Cohort.ABSTAINED)
    return frozenset(cohorts)


@dataclass(frozen=True, slots=True)
class CohortBrier:
    """One cohort's mean Brier score over a resolved, window-selected slice.

    Attributes:
        cohort: The :class:`Cohort` this row scores.
        window: The :class:`ObservationWindow` the slice was resolved under.
        count: Number of resolved records in the cohort (``0`` when empty).
        brier_ppm: The cohort's mean Brier score in ppm, or :data:`UNDEFINED`
            when the cohort has no resolved records.
    """

    cohort: Cohort
    window: ObservationWindow
    count: int
    brier_ppm: CohortBrierValue


def _cohort_forecasts(
    inputs: EvaluationInputs, cohort: Cohort
) -> tuple[FixtureForecast, ...]:
    """Narrow an inputs' forecasts to those belonging to one cohort.

    Args:
        inputs: The evaluation inputs whose forecasts are narrowed.
        cohort: The cohort to keep.

    Returns:
        The subset of ``inputs.forecasts`` whose membership includes ``cohort``.
    """
    return tuple(
        forecast for forecast in inputs.forecasts if cohort in assign_cohorts(forecast)
    )


def _windowed_cohort_forecasts(
    inputs: EvaluationInputs, cohort: Cohort, window: ObservationWindow
) -> tuple[FixtureForecast, ...]:
    """Narrow to a cohort then resolve the observation ``window`` over it.

    This is where the window becomes load-bearing on the selection path: after
    narrowing to the cohort, :func:`windbreak.evaluation.windows.resolve_window`
    collapses each market's forecast history to the single declared observation
    (e.g. the ``LATEST_BEFORE_CLOSE`` snapshot per market), so a
    multi-forecast-per-market cohort scores the window's chosen record rather
    than silently averaging every snapshot.

    Args:
        inputs: The evaluation inputs whose forecasts are narrowed.
        cohort: The cohort to keep.
        window: The observation window to resolve the cohort's forecasts under.

    Returns:
        The window-selected forecasts belonging to ``cohort``.

    Raises:
        ValueError: If a per-market window meets a ``None`` ``created_sequence``
            (propagated from :func:`resolve_window`).
    """
    forecasts = _cohort_forecasts(inputs, cohort)
    return resolve_window(forecasts, window=window).forecasts


def _resolved_count(
    forecasts: tuple[FixtureForecast, ...], inputs: EvaluationInputs
) -> int:
    """Count forecasts whose market resolves in ``inputs``.

    Args:
        forecasts: The forecasts to count.
        inputs: The evaluation inputs carrying the resolution mapping.

    Returns:
        The number of forecasts whose ``market_ticker`` has a resolution.
    """
    return sum(
        1 for forecast in forecasts if forecast.market_ticker in inputs.resolutions
    )


def _cohort_brier(
    inputs: EvaluationInputs, cohort: Cohort, window: ObservationWindow
) -> CohortBrier:
    """Build one :class:`CohortBrier` row for a cohort.

    Args:
        inputs: The admitted evaluation inputs (source of resolutions).
        cohort: The cohort to score.
        window: The observation window the cohort's forecasts are resolved and
            scored under, and the label the row carries.

    Returns:
        The cohort's row; ``brier_ppm`` is :data:`UNDEFINED` for an empty cohort.
    """
    forecasts = _windowed_cohort_forecasts(inputs, cohort, window)
    count = _resolved_count(forecasts, inputs)
    if count == 0:
        brier: CohortBrierValue = UNDEFINED
    else:
        brier = mean_brier(replace(inputs, forecasts=forecasts), window=window)
    return CohortBrier(cohort=cohort, window=window, count=count, brier_ppm=brier)


def cohort_brier_table(
    inputs: EvaluationInputs, *, window: ObservationWindow
) -> tuple[CohortBrier, ...]:
    """Build the seven-row per-cohort Brier table.

    Each cohort's forecasts are resolved under ``window`` -- collapsing every
    market's forecast history to the window's single declared observation --
    then scored with :func:`windbreak.evaluation.metrics.mean_brier`. The window
    is genuinely load-bearing: for a multi-forecast-per-market cohort,
    ``FIRST_PER_MARKET`` and ``LATEST_BEFORE_CLOSE`` select different snapshots
    and so can yield different Brier values (for singleton-per-market inputs the
    selection is an identity no-op). ``window`` is also the label each row
    carries.

    Args:
        inputs: The admitted evaluation inputs to score.
        window: The observation window every cohort is resolved, scored, and
            labelled under.

    Returns:
        One :class:`CohortBrier` per :class:`Cohort` (always seven), in enum
        order; a cohort with no resolved records carries :data:`UNDEFINED`.
    """
    return tuple(_cohort_brier(inputs, cohort, window) for cohort in Cohort)


def _cohort_mean_brier(
    inputs: EvaluationInputs, cohort: Cohort, window: ObservationWindow
) -> int:
    """Return one cohort's mean Brier, requiring at least one resolved record.

    Args:
        inputs: The admitted evaluation inputs (source of resolutions).
        cohort: The cohort to score.
        window: The observation window the cohort's forecasts are resolved and
            scored under.

    Returns:
        The cohort's mean Brier score, in ppm.

    Raises:
        EmptyCohortError: If the cohort has no resolved records; the message
            names the cohort and the ``resolved`` requirement. It subclasses
            ``ValueError`` so plain ``ValueError`` callers still catch it.
    """
    forecasts = _windowed_cohort_forecasts(inputs, cohort, window)
    if _resolved_count(forecasts, inputs) == 0:
        raise EmptyCohortError(
            f"cohort {cohort.value!r} has no resolved records; "
            "the traded-vs-skipped delta is undefined"
        )
    return mean_brier(replace(inputs, forecasts=forecasts), window=window)


def traded_vs_skipped_brier_delta(
    inputs: EvaluationInputs, *, window: ObservationWindow
) -> int:
    """Return ``mean_brier(SKIPPED) - mean_brier(TRADED)``, in ppm.

    A negative delta means the skipped forecasts would have scored *better*
    (lower Brier) than the traded ones -- an adverse-selection signal.

    Args:
        inputs: The admitted evaluation inputs to score.
        window: The declared observation-window label for both cohorts.

    Returns:
        The signed delta, in ppm.

    Raises:
        EmptyCohortError: If either the ``TRADED`` or ``SKIPPED`` cohort has no
            resolved records. Callers that need to render rather than crash on
            this ordinary early-deployment state (e.g. the registry adapter)
            catch it and surface the :data:`UNDEFINED` sentinel; it subclasses
            ``ValueError`` so plain ``ValueError`` handlers still catch it.
    """
    skipped = _cohort_mean_brier(inputs, Cohort.SKIPPED, window)
    traded = _cohort_mean_brier(inputs, Cohort.TRADED, window)
    return skipped - traded


def _resolved_track_forecasts(
    inputs: EvaluationInputs, *, live: bool
) -> tuple[FixtureForecast, ...]:
    """Return the resolved forecasts on one live/paper track, in fixture order.

    Args:
        inputs: The admitted evaluation inputs whose forecasts are partitioned.
        live: ``True`` keeps LIVE-track forecasts (``forecast.live is True``),
            ``False`` keeps PAPER-track forecasts.

    Returns:
        The subset of ``inputs.forecasts`` on the requested track whose market
        resolves and which carry a non-``None`` ``created_sequence`` (every
        temporally-admitted forecast does; the guard also keeps the rolling-window
        sort total).
    """
    return tuple(
        forecast
        for forecast in inputs.forecasts
        if forecast.live is live
        and forecast.market_ticker in inputs.resolutions
        and forecast.created_sequence is not None
    )


def _rolling_window(
    forecasts: tuple[FixtureForecast, ...], window_size: int
) -> tuple[FixtureForecast, ...]:
    """Return the most-recent ``window_size`` forecasts by ``created_sequence``.

    Args:
        forecasts: The resolved forecasts to truncate.
        window_size: The maximum number of most-recent forecasts to keep.

    Returns:
        The ``window_size`` forecasts with the highest ``created_sequence``
        (descending); fewer when the cohort is smaller than the window.
    """
    ordered = sorted(
        forecasts,
        key=lambda forecast: forecast.created_sequence or 0,
        reverse=True,
    )
    return tuple(ordered[:window_size])


def live_brier_degradation(
    inputs: EvaluationInputs, *, window: ObservationWindow, window_size: int
) -> int:
    """Return ``mean_brier(LIVE_window) - mean_brier(PAPER)``, in ppm.

    Mirrors :func:`traded_vs_skipped_brier_delta`'s two-cohort partition, but
    splits on ``forecast.live`` instead of ``forecast.traded``. Each track is
    scored in three ordered steps, so a market re-forecast several times still
    contributes exactly one observation:

    1. Keep only the resolved forecasts on the track
       (:func:`_resolved_track_forecasts`).
    2. Collapse per market via :func:`~windbreak.evaluation.windows.resolve_window`
       under ``window`` -- ``LATEST_BEFORE_CLOSE`` keeps each market's
       max-``created_sequence`` record -- satisfying
       :func:`~windbreak.evaluation.metrics.mean_brier`'s documented precondition
       that the caller has already applied ``windows.resolve_window`` (metrics.py),
       exactly as the sibling ``traded_vs_skipped_brier_delta`` does. This matters
       because the value feeds the automatic-demotion gate, so a re-forecast market
       must not be double-counted.
    3. Truncate the collapsed LIVE cohort to the most-recent ``window_size``
       distinct markets (by ``created_sequence`` descending); the PAPER baseline
       is every collapsed resolved PAPER market.

    A positive degradation flags the LIVE track scoring worse (higher Brier)
    than the PAPER baseline. One-forecast-per-market inputs make the collapse an
    identity, so this stays backward-compatible with every existing fixture.

    Args:
        inputs: The admitted evaluation inputs to score.
        window: The declared observation-window label; also the per-market
            collapse strategy passed to
            :func:`~windbreak.evaluation.windows.resolve_window` and through to
            :func:`~windbreak.evaluation.metrics.mean_brier`.
        window_size: The rolling-window size applied to the collapsed LIVE cohort.

    Returns:
        The signed degradation, in ppm.

    Raises:
        EmptyCohortError: If the windowed LIVE cohort or the PAPER cohort has no
            resolved records -- an ordinary early-deployment state. Callers that
            render rather than crash (the registry adapter) catch it and surface
            the :data:`UNDEFINED` sentinel; it subclasses ``ValueError`` so plain
            ``ValueError`` handlers still catch it.
    """
    live_collapsed = resolve_window(
        _resolved_track_forecasts(inputs, live=True), window=window
    ).forecasts
    live = _rolling_window(live_collapsed, window_size)
    paper = resolve_window(
        _resolved_track_forecasts(inputs, live=False), window=window
    ).forecasts
    if not live:
        raise EmptyCohortError(
            "LIVE cohort has no resolved records; live-brier degradation is undefined"
        )
    if not paper:
        raise EmptyCohortError(
            "PAPER cohort has no resolved records; live-brier degradation is undefined"
        )
    live_mean = mean_brier(replace(inputs, forecasts=live), window=window)
    paper_mean = mean_brier(replace(inputs, forecasts=paper), window=window)
    return live_mean - paper_mean


def mean_brier_over(
    slices: Iterable[WindowedForecasts], inputs: EvaluationInputs
) -> int:
    """Return the mean Brier over combined same-window slices.

    Args:
        slices: The window slices to combine and score; they must all name the
            same :class:`ObservationWindow`.
        inputs: The admitted evaluation inputs (source of resolutions).

    Returns:
        The mean Brier score, in ppm, over the combined slice's forecasts.

    Raises:
        MixedObservationWindowError: If the slices name more than one window
            (propagated from :func:`windbreak.evaluation.windows.combine`).
        ValueError: If no forecast in the combined slice resolves.
    """
    combined = combine(slices)
    narrowed = replace(inputs, forecasts=combined.forecasts)
    return mean_brier(narrowed, window=combined.window)
