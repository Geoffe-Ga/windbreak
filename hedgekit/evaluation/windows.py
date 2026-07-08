"""Observation-window selection strategies for the evaluation harness (#53).

SPEC-EPIC_07 treats an :class:`ObservationWindow` as a *selection strategy*: it
decides which snapshot(s) of a market's forecast history a metric scores, rather
than merely labelling a value. This module is the canonical home of that
vocabulary -- :class:`ObservationWindow` lives here and
:mod:`hedgekit.evaluation.registry` re-exports it, so
``registry.ObservationWindow is windows.ObservationWindow`` holds -- and it owns
:func:`resolve_window`, which narrows a flat forecast tuple to the records the
window admits.

This module is a runtime *leaf* within the evaluation package: it imports
nothing from any sibling evaluation module at runtime (it references
:class:`~hedgekit.evaluation.registry.FixtureForecast` only under
:data:`typing.TYPE_CHECKING`), keeping the intra-package dependency graph
acyclic with every runtime edge pointing away from it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hedgekit.evaluation.registry import FixtureForecast


class ObservationWindow(enum.Enum):
    """The sampling window a metric observes a forecast/market over.

    Different metrics score different slices of a market's life: the first
    forecast seen, the last snapshot before close, every daily snapshot, or only
    the snapshot that triggered a trade. The window is a selection strategy that
    :func:`resolve_window` applies to a forecast tuple, and metadata carried on
    each :class:`~hedgekit.evaluation.registry.MetricSpec` and rendered beside
    the metric's value.
    """

    FIRST_PER_MARKET = "first_per_market"
    LATEST_BEFORE_CLOSE = "latest_before_close"
    DAILY_SNAPSHOTS = "daily_snapshots"
    TRADE_TRIGGERING = "trade_triggering"


#: The headline observation window every forecast-track metric and the
#: selection-bias cohort/abstention detail are scored and labelled under
#: (SPEC-EPIC_07 S13.4). Defined once here, its canonical home, and imported by
#: :mod:`hedgekit.evaluation.registry` and :mod:`hedgekit.evaluation.report` so
#: the "headline metric names its window" choice cannot drift between call sites.
HEADLINE_OBSERVATION_WINDOW = ObservationWindow.LATEST_BEFORE_CLOSE


class MixedObservationWindowError(Exception):
    """Raised when slices spanning more than one :class:`ObservationWindow` mix.

    A metric must never silently average across incompatible sampling
    strategies, so combining slices that name distinct windows is a structural
    error rather than a tolerated merge.
    """


@dataclass(frozen=True, slots=True)
class WindowedForecasts:
    """A forecast tuple bound to the single window that selected it.

    Attributes:
        window: The :class:`ObservationWindow` these forecasts were resolved
            under.
        forecasts: The window-selected forecasts, in deterministic order.
    """

    window: ObservationWindow
    forecasts: tuple[FixtureForecast, ...]


def _pick_extreme(
    records: tuple[FixtureForecast, ...], *, want_max: bool
) -> FixtureForecast:
    """Return the min- or max-``created_sequence`` record, fail-closed on ``None``.

    Args:
        records: One market's forecast records (at least one).
        want_max: Select the maximum ``created_sequence`` when ``True``, the
            minimum when ``False``.

    Returns:
        The single record with the extreme ``created_sequence``.

    Raises:
        ValueError: If any record carries ``created_sequence is None`` -- the
            selection is undefined, so it fails closed rather than picking an
            arbitrary record; the message names the ``created_sequence`` field.
    """
    keyed: list[tuple[int, FixtureForecast]] = []
    for record in records:
        sequence = record.created_sequence
        if sequence is None:
            raise ValueError(
                "created_sequence must be non-None to resolve a "
                "FIRST_PER_MARKET/LATEST_BEFORE_CLOSE window; "
                f"forecast {record.forecast_id!r} has created_sequence=None"
            )
        keyed.append((sequence, record))
    chosen = (
        max(keyed, key=lambda item: item[0])
        if want_max
        else min(keyed, key=lambda item: item[0])
    )
    return chosen[1]


def _select_per_market(
    forecasts: tuple[FixtureForecast, ...], *, want_max: bool
) -> tuple[FixtureForecast, ...]:
    """Select one extreme-``created_sequence`` record per market, market-sorted.

    Args:
        forecasts: The flat forecast tuple to narrow.
        want_max: Passed through to :func:`_pick_extreme`.

    Returns:
        One record per ``market_ticker``, ordered by ticker for byte-stability.

    Raises:
        ValueError: If any market carries a record with
            ``created_sequence is None`` (propagated from :func:`_pick_extreme`).
    """
    by_market: dict[str, list[FixtureForecast]] = {}
    for forecast in forecasts:
        by_market.setdefault(forecast.market_ticker, []).append(forecast)
    return tuple(
        _pick_extreme(tuple(by_market[ticker]), want_max=want_max)
        for ticker in sorted(by_market)
    )


def resolve_window(
    forecasts: tuple[FixtureForecast, ...], *, window: ObservationWindow
) -> WindowedForecasts:
    """Narrow a forecast tuple to the records the ``window`` admits.

    Selection semantics per :class:`ObservationWindow`:

    - ``FIRST_PER_MARKET``: the min-``created_sequence`` record per market.
    - ``LATEST_BEFORE_CLOSE``: the max-``created_sequence`` record per market.
    - ``DAILY_SNAPSHOTS``: every record, unfiltered, in input order.
    - ``TRADE_TRIGGERING``: only ``traded=True`` records, in input order.

    ``FIRST_PER_MARKET`` / ``LATEST_BEFORE_CLOSE`` require a non-``None``
    ``created_sequence`` on every record and fail closed otherwise;
    ``DAILY_SNAPSHOTS`` / ``TRADE_TRIGGERING`` tolerate ``None``.

    Args:
        forecasts: The flat forecast tuple to narrow.
        window: The selection strategy to apply.

    Returns:
        A :class:`WindowedForecasts` binding ``window`` to the selected records.

    Raises:
        ValueError: If a per-market window meets a ``None`` ``created_sequence``.
    """
    if window is ObservationWindow.DAILY_SNAPSHOTS:
        selected = forecasts
    elif window is ObservationWindow.TRADE_TRIGGERING:
        selected = tuple(forecast for forecast in forecasts if forecast.traded)
    else:
        selected = _select_per_market(
            forecasts, want_max=window is ObservationWindow.LATEST_BEFORE_CLOSE
        )
    return WindowedForecasts(window=window, forecasts=selected)


def combine(slices: Iterable[WindowedForecasts]) -> WindowedForecasts:
    """Concatenate same-window slices, refusing to mix distinct windows.

    Args:
        slices: The :class:`WindowedForecasts` slices to concatenate, in order.

    Returns:
        One :class:`WindowedForecasts` carrying the shared window and the
        slices' forecasts concatenated in the order the slices were given.

    Raises:
        MixedObservationWindowError: If the slices name more than one distinct
            :class:`ObservationWindow`.
    """
    materialized = tuple(slices)
    windows = {piece.window for piece in materialized}
    if len(windows) > 1:
        raise MixedObservationWindowError(
            "cannot combine WindowedForecasts spanning multiple windows: "
            f"{sorted(observed.value for observed in windows)}"
        )
    forecasts = tuple(
        forecast for piece in materialized for forecast in piece.forecasts
    )
    return WindowedForecasts(window=materialized[0].window, forecasts=forecasts)
