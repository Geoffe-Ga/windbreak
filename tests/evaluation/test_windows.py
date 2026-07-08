"""Failing-first tests for `hedgekit.evaluation.windows` (issue #53, RED).

`hedgekit.evaluation.windows` does not exist yet, so every test below imports
its new symbols from that module as the FIRST statement inside the test body
(matching this package's established RED convention in
`test_temporal_integrity.py`) so each test collects independently and fails on
its own `ModuleNotFoundError: No module named 'hedgekit.evaluation.windows'`
rather than one collection-time explosion.

Pins SPEC-EPIC_07 issue #53's window-as-selection-strategy vocabulary:

- `ObservationWindow` moves to `windows` as its canonical home;
  `registry.ObservationWindow` becomes a re-export of the exact same enum
  object (`registry.ObservationWindow is windows.ObservationWindow`).
- `resolve_window(forecasts, *, window) -> WindowedForecasts` selects, per
  `ObservationWindow` member, which snapshot(s) of a market's forecast history
  enter a metric:
    * `FIRST_PER_MARKET` -- the min-`created_sequence` record per market.
    * `LATEST_BEFORE_CLOSE` -- the max-`created_sequence` record per market.
    * `DAILY_SNAPSHOTS` -- every record, unfiltered.
    * `TRADE_TRIGGERING` -- only `traded=True` records.
  `FIRST_PER_MARKET` / `LATEST_BEFORE_CLOSE` raise `ValueError` (fail-closed)
  if any record in a market has `created_sequence is None`; `DAILY_SNAPSHOTS`
  / `TRADE_TRIGGERING` tolerate `None`.
- `combine(slices)` concatenates same-window `WindowedForecasts` slices, and
  raises `MixedObservationWindowError` when the slices name more than one
  distinct window -- the DoD "window-mixing raises" guarantee, pinned both at
  the raw-slice level and (via `cohorts.mean_brier_over`) at the metric level.
"""

from __future__ import annotations

import pytest

from hedgekit.evaluation import EvaluationInputs, FixtureForecast, ResolutionOutcome
from hedgekit.numeric.types import ProbabilityPpm


def _forecast(
    *,
    forecast_id: str,
    market_ticker: str,
    probability_ppm: int,
    created_sequence: int | None,
    traded: bool = False,
    baseline_pips: int = 5_000,
) -> FixtureForecast:
    """Build one `FixtureForecast` with the fields these window tests vary.

    Args:
        forecast_id: Stable identifier of the forecast record.
        market_ticker: Ticker of the market this forecast is about.
        probability_ppm: Forecast probability, in ppm.
        created_sequence: The forecast's creation sequence, or `None`.
        traded: Whether a live trade was actually taken.
        baseline_pips: The reference executable price, in pips.

    Returns:
        The constructed `FixtureForecast`.
    """
    return FixtureForecast(
        forecast_id=forecast_id,
        market_ticker=market_ticker,
        probability_ppm=ProbabilityPpm(probability_ppm),
        eligible_for_live=True,
        abstention_reason=None,
        traded=traded,
        baseline_executable_price_pips=baseline_pips,
        created_sequence=created_sequence,
    )


#: Market M: three snapshots at created_sequence 1 (untraded), 3 (traded), and
#: 5 (untraded). Market N: a single snapshot at created_sequence 2 (untraded).
_MARKET_M_SEQ1 = _forecast(
    forecast_id="m-seq1",
    market_ticker="M",
    probability_ppm=200_000,
    created_sequence=1,
    traded=False,
)
_MARKET_M_SEQ3 = _forecast(
    forecast_id="m-seq3",
    market_ticker="M",
    probability_ppm=600_000,
    created_sequence=3,
    traded=True,
)
_MARKET_M_SEQ5 = _forecast(
    forecast_id="m-seq5",
    market_ticker="M",
    probability_ppm=700_000,
    created_sequence=5,
    traded=False,
)
_MARKET_N_SEQ2 = _forecast(
    forecast_id="n-seq2",
    market_ticker="N",
    probability_ppm=400_000,
    created_sequence=2,
    traded=False,
)
_MULTI_MARKET_FORECASTS = (
    _MARKET_M_SEQ1,
    _MARKET_M_SEQ3,
    _MARKET_M_SEQ5,
    _MARKET_N_SEQ2,
)


# ---------------------------------------------------------------------------
# 1. resolve_window: per-ObservationWindow selection semantics.
# ---------------------------------------------------------------------------


def test_first_per_market_selects_min_created_sequence_per_market() -> None:
    """`FIRST_PER_MARKET` keeps the min-`created_sequence` record per market:
    M's seq-1 record and N's (only) seq-2 record.
    """
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    result = resolve_window(
        _MULTI_MARKET_FORECASTS, window=ObservationWindow.FIRST_PER_MARKET
    )

    assert result.window is ObservationWindow.FIRST_PER_MARKET
    ids = {forecast.forecast_id for forecast in result.forecasts}
    assert ids == {"m-seq1", "n-seq2"}
    assert len(result.forecasts) == 2


def test_latest_before_close_selects_max_created_sequence_per_market() -> None:
    """`LATEST_BEFORE_CLOSE` keeps the max-`created_sequence` record per
    market: M's seq-5 record and N's (only) seq-2 record.
    """
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    result = resolve_window(
        _MULTI_MARKET_FORECASTS, window=ObservationWindow.LATEST_BEFORE_CLOSE
    )

    assert result.window is ObservationWindow.LATEST_BEFORE_CLOSE
    ids = {forecast.forecast_id for forecast in result.forecasts}
    assert ids == {"m-seq5", "n-seq2"}
    assert len(result.forecasts) == 2


def test_trade_triggering_selects_only_traded_records() -> None:
    """`TRADE_TRIGGERING` keeps only `traded=True` records: M's seq-3 record
    alone (N has no traded record at all).
    """
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    result = resolve_window(
        _MULTI_MARKET_FORECASTS, window=ObservationWindow.TRADE_TRIGGERING
    )

    assert result.window is ObservationWindow.TRADE_TRIGGERING
    ids = {forecast.forecast_id for forecast in result.forecasts}
    assert ids == {"m-seq3"}
    assert len(result.forecasts) == 1


def test_daily_snapshots_selects_every_record_unfiltered() -> None:
    """`DAILY_SNAPSHOTS` keeps every record in the input, unfiltered: all
    three M snapshots plus N's single snapshot.
    """
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    result = resolve_window(
        _MULTI_MARKET_FORECASTS, window=ObservationWindow.DAILY_SNAPSHOTS
    )

    assert result.window is ObservationWindow.DAILY_SNAPSHOTS
    ids = {forecast.forecast_id for forecast in result.forecasts}
    assert ids == {"m-seq1", "m-seq3", "m-seq5", "n-seq2"}
    assert len(result.forecasts) == 4


# ---------------------------------------------------------------------------
# 2. created_sequence=None: fail-closed for FIRST/LATEST, tolerated by
#    DAILY_SNAPSHOTS/TRADE_TRIGGERING.
# ---------------------------------------------------------------------------


def test_first_per_market_raises_value_error_on_none_created_sequence() -> None:
    """A market carrying a record with `created_sequence=None` makes
    `FIRST_PER_MARKET` selection undefined for that market: fail-closed,
    raise `ValueError`, never silently pick an arbitrary record.
    """
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    forecasts = (
        _forecast(
            forecast_id="none-seq",
            market_ticker="M",
            probability_ppm=500_000,
            created_sequence=None,
        ),
    )

    with pytest.raises(ValueError, match=r"(?i)created_sequence"):
        resolve_window(forecasts, window=ObservationWindow.FIRST_PER_MARKET)


def test_latest_before_close_raises_value_error_on_none_created_sequence() -> None:
    """Same fail-closed rule as `FIRST_PER_MARKET`, for `LATEST_BEFORE_CLOSE`."""
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    forecasts = (
        _forecast(
            forecast_id="none-seq",
            market_ticker="M",
            probability_ppm=500_000,
            created_sequence=None,
        ),
    )

    with pytest.raises(ValueError, match=r"(?i)created_sequence"):
        resolve_window(forecasts, window=ObservationWindow.LATEST_BEFORE_CLOSE)


def test_daily_snapshots_tolerates_none_created_sequence() -> None:
    """`DAILY_SNAPSHOTS` does not care about `created_sequence` at all, so a
    `None` record is included without raising.
    """
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    forecasts = (
        _forecast(
            forecast_id="none-seq",
            market_ticker="M",
            probability_ppm=500_000,
            created_sequence=None,
        ),
    )

    result = resolve_window(forecasts, window=ObservationWindow.DAILY_SNAPSHOTS)

    assert len(result.forecasts) == 1
    assert result.forecasts[0].forecast_id == "none-seq"


def test_trade_triggering_tolerates_none_created_sequence() -> None:
    """`TRADE_TRIGGERING` selects purely on `traded`, so a traded record with
    `created_sequence=None` is included without raising.
    """
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    forecasts = (
        _forecast(
            forecast_id="none-seq-traded",
            market_ticker="M",
            probability_ppm=500_000,
            created_sequence=None,
            traded=True,
        ),
    )

    result = resolve_window(forecasts, window=ObservationWindow.TRADE_TRIGGERING)

    assert len(result.forecasts) == 1
    assert result.forecasts[0].forecast_id == "none-seq-traded"


# ---------------------------------------------------------------------------
# 3. combine: same-window concatenation, mixed-window raises.
# ---------------------------------------------------------------------------


def test_combine_raises_mixed_observation_window_error_on_distinct_windows() -> None:
    """Combining a `FIRST_PER_MARKET` slice with a `LATEST_BEFORE_CLOSE`
    slice names two distinct windows and must raise
    `MixedObservationWindowError` -- the structural guarantee against
    accidentally averaging across incompatible sampling strategies.
    """
    from hedgekit.evaluation.windows import (
        MixedObservationWindowError,
        ObservationWindow,
        combine,
        resolve_window,
    )

    first_slice = resolve_window(
        _MULTI_MARKET_FORECASTS, window=ObservationWindow.FIRST_PER_MARKET
    )
    latest_slice = resolve_window(
        _MULTI_MARKET_FORECASTS, window=ObservationWindow.LATEST_BEFORE_CLOSE
    )

    with pytest.raises(MixedObservationWindowError):
        combine([first_slice, latest_slice])


def test_combine_same_window_slices_concatenates_forecasts() -> None:
    """Combining two slices that both name `FIRST_PER_MARKET` concatenates
    their forecasts (in the order the slices were given) and preserves the
    shared window.
    """
    from hedgekit.evaluation.windows import ObservationWindow, combine, resolve_window

    slice_a = resolve_window(
        (_MARKET_M_SEQ1,), window=ObservationWindow.FIRST_PER_MARKET
    )
    slice_b = resolve_window(
        (_MARKET_N_SEQ2,), window=ObservationWindow.FIRST_PER_MARKET
    )

    combined = combine([slice_a, slice_b])

    assert combined.window is ObservationWindow.FIRST_PER_MARKET
    assert combined.forecasts == slice_a.forecasts + slice_b.forecasts
    assert [forecast.forecast_id for forecast in combined.forecasts] == [
        "m-seq1",
        "n-seq2",
    ]


# ---------------------------------------------------------------------------
# 4. WindowedForecasts carries its window (already exercised implicitly
#    above via `result.window`, pinned here directly for clarity).
# ---------------------------------------------------------------------------


def test_windowed_forecasts_exposes_window_and_forecasts_attributes() -> None:
    """`WindowedForecasts` is a frozen carrier exposing exactly `.window` and
    `.forecasts`, binding one `ObservationWindow` to a tuple of forecasts.
    """
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    result = resolve_window((_MARKET_M_SEQ1,), window=ObservationWindow.DAILY_SNAPSHOTS)

    assert result.window is ObservationWindow.DAILY_SNAPSHOTS
    assert result.forecasts == (_MARKET_M_SEQ1,)


# ---------------------------------------------------------------------------
# 5. Identity: registry.ObservationWindow IS windows.ObservationWindow.
# ---------------------------------------------------------------------------


def test_registry_observation_window_is_the_same_object_as_windows_module() -> None:
    """`windows.py` is the canonical home of `ObservationWindow`;
    `registry.ObservationWindow` must be the exact same enum object (a
    re-export), not a lookalike duplicate with equal member names.
    """
    from hedgekit.evaluation import registry, windows

    assert registry.ObservationWindow is windows.ObservationWindow


# ---------------------------------------------------------------------------
# 6. Metric-level mixing guard: cohorts.mean_brier_over raises on mixed
#    windows too (the DoD "window-mixing raises" test, at the metric layer).
# ---------------------------------------------------------------------------


def test_mean_brier_over_raises_mixed_observation_window_error_on_mixed_slices() -> (
    None
):
    """`cohorts.mean_brier_over` accepts a list of `WindowedForecasts` slices
    and must raise `MixedObservationWindowError`, the same guard `combine`
    enforces, when the slices name more than one distinct window -- a metric
    must never silently average across incompatible sampling strategies.
    """
    from hedgekit.evaluation.cohorts import mean_brier_over
    from hedgekit.evaluation.windows import (
        MixedObservationWindowError,
        ObservationWindow,
        resolve_window,
    )

    first_slice = resolve_window(
        _MULTI_MARKET_FORECASTS, window=ObservationWindow.FIRST_PER_MARKET
    )
    latest_slice = resolve_window(
        _MULTI_MARKET_FORECASTS, window=ObservationWindow.LATEST_BEFORE_CLOSE
    )
    inputs = EvaluationInputs(
        forecasts=_MULTI_MARKET_FORECASTS,
        resolutions={"M": ResolutionOutcome.YES, "N": ResolutionOutcome.NO},
    )

    with pytest.raises(MixedObservationWindowError):
        mean_brier_over([first_slice, latest_slice], inputs)


def test_mean_brier_over_same_window_slices_returns_the_combined_mean() -> None:
    """`cohorts.mean_brier_over` happy path: two same-window slices combine and
    score to their pooled mean Brier.

    Slices, both `FIRST_PER_MARKET` (each over a single-record market, so
    selection is identity):

        slice_a -> M's seq-1 record  p=200_000  (market M resolves YES)
        slice_b -> N's seq-2 record  p=400_000  (market N resolves NO)

    Per-forecast Brier term `(p_ppm - outcome_ppm)^2` (YES=1_000_000, NO=0):

        M seq1: (200_000 - 1_000_000)^2 = (-800_000)^2 = 6.4e11
        N seq2: (400_000 - 0)^2         = ( 400_000)^2 = 1.6e11

    total = 8.0e11; n=2 -> mean = 8.0e11 / (2 * 1_000_000) = 400_000 ppm.
    """
    from hedgekit.evaluation.cohorts import mean_brier_over
    from hedgekit.evaluation.windows import ObservationWindow, resolve_window

    slice_a = resolve_window(
        (_MARKET_M_SEQ1,), window=ObservationWindow.FIRST_PER_MARKET
    )
    slice_b = resolve_window(
        (_MARKET_N_SEQ2,), window=ObservationWindow.FIRST_PER_MARKET
    )
    inputs = EvaluationInputs(
        forecasts=_MULTI_MARKET_FORECASTS,
        resolutions={"M": ResolutionOutcome.YES, "N": ResolutionOutcome.NO},
    )

    assert mean_brier_over([slice_a, slice_b], inputs) == 400_000
