"""Failing-first tests for `windbreak.evaluation.metrics` and
`windbreak.evaluation.power` (issue #51, RED; SPEC-EPIC_07 S13.5).

`windbreak.evaluation.metrics` and `windbreak.evaluation.power` do not exist yet,
so every test below fails collection/execution with `ModuleNotFoundError: No
module named 'windbreak.evaluation.metrics'` (or `'...power'`) -- the expected
Gate 1 RED state for issue #51.

Pins SPEC S13.5's forecast-track statistical machinery:

- Scalar metrics `mean_brier`, `mean_log_score`, `brier_skill`,
  `expected_calibration_error`, `calibration_slope`, `calibration_intercept`,
  `sharpness` -- each `(inputs, *, window) -> int`, ppm/micro-nat scaled,
  computed only over forecasts whose ticker resolves (S13.6: unresolved
  forecasts never enter a headline metric).
- Rich (non-scalar) reports `reliability_diagram`, `price_bucket_report`,
  `edge_bucket_report` -- each `(inputs, *, window) -> tuple[...]`.
- The power-analysis document (`windbreak.evaluation.power.power_analysis`),
  which this file also covers per the issue's file fence ("power tests live
  here, not a new file").

Every pinned exact value is hand-derived in a comment at its assertion,
including `mean_log_score`, whose deterministic integer `_ln` reproduces the
ceiling-rounded reference value byte-for-byte (SPEC S3.5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from windbreak.evaluation import (
    EvaluationInputs,
    FixtureForecast,
    ObservationWindow,
    ResolutionOutcome,
)
from windbreak.numeric.types import ProbabilityPpm

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The epic-wide known-answer fixture shared by issues #49-#55; see its own
#: "description" key for the hand-computed Brier arithmetic this suite pins
#: against (`expected.brier_mean_ppm == 78_000`).
SYNTHETIC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_known_answer.json"
)

#: The window every call below uses. SPEC S13.4: mixing observation windows
#: within one metric call is a test failure; enforcement lands in #53, but
#: every metric signature already takes this parameter.
_WINDOW = ObservationWindow.LATEST_BEFORE_CLOSE

#: Hand-computed exact `mean_brier` over the synthetic fixture -- see
#: `test_skeleton.py`'s own derivation and this file's mirroring comment
#: below; both independently agree on `78_000`.
_EXPECTED_MEAN_BRIER_PPM = 78_000

#: Hand-computed exact `brier_skill` over the synthetic fixture; see the
#: dedicated test below for the full arithmetic.
_EXPECTED_BRIER_SKILL_PPM = -49_375

#: A tight hand-derived bound (not an exact pin -- see module docstring) for
#: `mean_log_score` over the synthetic fixture. Per-forecast term is
#: `-ln(p)` (outcome yes) or `-ln(1-p)` (outcome no), in nats; MKT-09 (p=1.0,
#: yes) and MKT-10 (p=0.0, no) both score exactly `0`. The other eight terms,
#: computed at full double precision:
#:   -ln(0.9) = 0.105360515657826303   (MKT-01, MKT-02: -ln(1-0.1)=-ln(0.9))
#:   -ln(0.7) = 0.356674943938732      (MKT-03, MKT-04: -ln(1-0.3)=-ln(0.7))
#:   -ln(0.5) = 0.693147180559945      (MKT-05, MKT-06: -ln(1-0.5)=-ln(0.5))
#:   -ln(0.8) = 0.223143551314210      (MKT-07, MKT-08: -ln(1-0.2)=-ln(0.8))
#: sum = 2 * (0.105360515657826303 + 0.356674943938732 + 0.693147180559945
#:            + 0.223143551314210) = 2 * 1.378326191470713 = 2.756652382941426
#: mean over 10 = 0.2756652382941426; scaled to micro-nats (x1_000_000) =
#: 275_665.2382941426 -- OVERSTATE_COST (ceiling) rounds this to 275_666.
#: The deterministic integer `_ln` reproduces this ceiling exactly (SPEC S3.5),
#: so this is pinned to the exact int rather than a tolerance band.
_EXPECTED_LOG_SCORE_PPM = 275_666


def _load_fixture() -> dict[str, Any]:
    """Load and JSON-decode the shared synthetic known-answer fixture.

    Returns:
        The decoded fixture payload.
    """
    return json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))


def _forecast_from_entry(entry: Mapping[str, Any]) -> FixtureForecast:
    """Build a `FixtureForecast` from one raw fixture forecast entry.

    Duplicated locally (rather than imported from `windbreak.evaluation.report`)
    to keep this suite import-isolated, matching `test_baselines.py`'s
    established convention of local, non-cross-test-module helpers.

    Args:
        entry: The decoded forecast object from a fixture.

    Returns:
        The typed, validated forecast row.
    """
    return FixtureForecast(
        forecast_id=entry["forecast_id"],
        market_ticker=entry["market_ticker"],
        probability_ppm=ProbabilityPpm(entry["probability_ppm"]),
        eligible_for_live=entry["eligible_for_live"],
        abstention_reason=entry["abstention_reason"],
        traded=entry["traded"],
        baseline_executable_price_pips=entry["baseline_executable_price_pips"],
        correlation_group_id=entry.get("correlation_group_id"),
    )


def _resolutions_from_entries(
    entries: list[Mapping[str, Any]],
) -> dict[str, ResolutionOutcome]:
    """Build a ticker-keyed resolution mapping from raw resolution entries.

    Args:
        entries: The decoded `resolutions` list.

    Returns:
        A mapping from `market_ticker` to its `ResolutionOutcome`.
    """
    return {
        entry["market_ticker"]: ResolutionOutcome(entry["outcome"]) for entry in entries
    }


def _inputs_from_payload(payload: Mapping[str, Any]) -> EvaluationInputs:
    """Build typed `EvaluationInputs` directly from a decoded fixture payload.

    Args:
        payload: The decoded fixture payload.

    Returns:
        The typed evaluation inputs.
    """
    forecasts = tuple(_forecast_from_entry(entry) for entry in payload["forecasts"])
    resolutions = _resolutions_from_entries(payload["resolutions"])
    return EvaluationInputs(forecasts=forecasts, resolutions=resolutions)


def _synthetic_inputs() -> EvaluationInputs:
    """Build `EvaluationInputs` from the shared synthetic known-answer fixture.

    Returns:
        The typed evaluation inputs for the 10-forecast synthetic fixture.
    """
    return _inputs_from_payload(_load_fixture())


def _forecast(
    *,
    forecast_id: str,
    market_ticker: str,
    probability_ppm: int,
    baseline_pips: int,
    traded: bool = True,
    eligible: bool = True,
    abstention: str | None = None,
) -> FixtureForecast:
    """Build one `FixtureForecast` with the fields these tests vary.

    Args:
        forecast_id: Stable identifier of the forecast record.
        market_ticker: Ticker of the market this forecast is about.
        probability_ppm: Forecast probability, in ppm.
        baseline_pips: The reference executable price, in pips.
        traded: Whether a live trade was actually taken.
        eligible: Whether the forecast passed live-eligibility gates.
        abstention: The abstention reason, or `None` if traded.

    Returns:
        The constructed `FixtureForecast`.
    """
    return FixtureForecast(
        forecast_id=forecast_id,
        market_ticker=market_ticker,
        probability_ppm=ProbabilityPpm(probability_ppm),
        eligible_for_live=eligible,
        abstention_reason=abstention,
        traded=traded,
        baseline_executable_price_pips=baseline_pips,
    )


# ---------------------------------------------------------------------------
# 1. mean_brier
# ---------------------------------------------------------------------------


def test_mean_brier_matches_hand_computation_on_synthetic_fixture() -> None:
    """`mean_brier` reproduces the fixture's own hand-derived 78_000 ppm.

    Sum of (p-o)^2 over the 10 forecasts is exactly 0.78 (see the fixture's
    `description` and `test_skeleton.py`'s independent recomputation); the
    mean over 10 is 0.078, scaled to ppm is 78_000 -- an exact division (no
    remainder), so OVERSTATE_COST and UNDERSTATE_EQUITY agree here.
    """
    from windbreak.evaluation.metrics import mean_brier

    result = mean_brier(_synthetic_inputs(), window=_WINDOW)

    assert result == _EXPECTED_MEAN_BRIER_PPM
    assert isinstance(result, int)


@given(
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=1_000_000), st.booleans()),
        min_size=1,
        max_size=8,
    )
)
def test_mean_brier_is_always_within_zero_to_one_million_ppm(
    rows: list[tuple[int, bool]],
) -> None:
    """`mean_brier` is bounded in `[0, 1_000_000]` for any resolved inputs."""
    from windbreak.evaluation.metrics import mean_brier

    forecasts = tuple(
        _forecast(
            forecast_id=f"fc-{index}",
            market_ticker=f"MKT-{index}",
            probability_ppm=probability_ppm,
            baseline_pips=5_000,
        )
        for index, (probability_ppm, _outcome_yes) in enumerate(rows)
    )
    resolutions = {
        f"MKT-{index}": (ResolutionOutcome.YES if outcome_yes else ResolutionOutcome.NO)
        for index, (_probability_ppm, outcome_yes) in enumerate(rows)
    }
    inputs = EvaluationInputs(forecasts=forecasts, resolutions=resolutions)

    result = mean_brier(inputs, window=_WINDOW)

    assert 0 <= result <= 1_000_000


def test_mean_brier_raises_value_error_on_empty_resolved_set() -> None:
    """An `EvaluationInputs` with no resolved forecasts raises `ValueError`
    naming the empty/resolved condition -- a headline metric must never be
    silently computed over zero observations (S13.6).
    """
    from windbreak.evaluation.metrics import mean_brier

    unresolved_only = EvaluationInputs(
        forecasts=(
            _forecast(
                forecast_id="fc-unresolved",
                market_ticker="MKT-UNRESOLVED",
                probability_ppm=500_000,
                baseline_pips=5_000,
            ),
        ),
        resolutions={},
    )

    with pytest.raises(ValueError, match=r"(?i)resolved"):
        mean_brier(unresolved_only, window=_WINDOW)


def test_empty_resolved_set_raises_the_dedicated_no_resolved_forecasts_error() -> None:
    """The empty-resolved guard raises `NoResolvedForecastsError` specifically
    (issue #188), not a bare `ValueError` -- so the registry's `gated_compute`
    choke point can catch it by type (mirroring the existing
    `EmptyCohortError` adapter) and map it to the `UNDEFINED` sentinel rather
    than crashing the whole report. It must still subclass `ValueError` so
    every pre-existing `pytest.raises(ValueError)` expectation (including the
    one directly above) keeps passing unchanged.
    """
    from windbreak.evaluation.metrics import NoResolvedForecastsError, mean_brier

    unresolved_only = EvaluationInputs(
        forecasts=(
            _forecast(
                forecast_id="fc-unresolved",
                market_ticker="MKT-UNRESOLVED",
                probability_ppm=500_000,
                baseline_pips=5_000,
            ),
        ),
        resolutions={},
    )

    assert issubclass(NoResolvedForecastsError, ValueError)
    with pytest.raises(NoResolvedForecastsError):
        mean_brier(unresolved_only, window=_WINDOW)


def test_mean_brier_excludes_forecasts_with_no_matching_resolution() -> None:
    """A forecast whose ticker has no entry in `resolutions` never enters the
    metric (S13.6): adding one alongside a resolved forecast leaves the
    result identical to computing over the resolved forecast alone.
    """
    from windbreak.evaluation.metrics import mean_brier

    resolved_only = EvaluationInputs(
        forecasts=(
            _forecast(
                forecast_id="fc-resolved",
                market_ticker="MKT-RESOLVED",
                probability_ppm=800_000,
                baseline_pips=7_000,
            ),
        ),
        resolutions={"MKT-RESOLVED": ResolutionOutcome.YES},
    )
    with_unresolved_added = EvaluationInputs(
        forecasts=(
            *resolved_only.forecasts,
            _forecast(
                forecast_id="fc-unresolved",
                market_ticker="MKT-UNRESOLVED",
                probability_ppm=999_999,
                baseline_pips=1,
            ),
        ),
        resolutions=resolved_only.resolutions,
    )

    # (0.8 - 1)^2 = 0.04 -> 40_000 ppm, exact.
    expected = 40_000
    assert mean_brier(resolved_only, window=_WINDOW) == expected
    assert mean_brier(with_unresolved_added, window=_WINDOW) == expected


# ---------------------------------------------------------------------------
# 2. mean_log_score
# ---------------------------------------------------------------------------


def test_mean_log_score_matches_hand_computation_on_synthetic_fixture() -> None:
    """`mean_log_score` over the synthetic fixture equals the exact ceiling-
    rounded micro-nat value 275_666, hand-derived from the double-precision
    reference 275_665.24 (see the module-level derivation comment). The
    deterministic integer `_ln` reproduces this exactly, so it is an exact
    known-answer pin, not a tolerance band.
    """
    from windbreak.evaluation.metrics import mean_log_score

    result = mean_log_score(_synthetic_inputs(), window=_WINDOW)

    assert isinstance(result, int)
    assert result >= 0
    assert result == _EXPECTED_LOG_SCORE_PPM


def test_mean_log_score_raises_value_error_on_certain_wrong_yes_forecast() -> None:
    """A forecast of `p=0` on a `YES` resolution is certain-and-wrong: `-ln(0)`
    is undefined, so `mean_log_score` must raise `ValueError` rather than
    silently return `inf` or crash with an unrelated exception.
    """
    from windbreak.evaluation.metrics import mean_log_score

    inputs = EvaluationInputs(
        forecasts=(
            _forecast(
                forecast_id="fc-certain-wrong",
                market_ticker="MKT-CW",
                probability_ppm=0,
                baseline_pips=1,
            ),
        ),
        resolutions={"MKT-CW": ResolutionOutcome.YES},
    )

    with pytest.raises(ValueError, match=r"(?i)probability|log"):
        mean_log_score(inputs, window=_WINDOW)


def test_mean_log_score_raises_value_error_on_certain_wrong_no_forecast() -> None:
    """A forecast of `p=1_000_000` (certainty) on a `NO` resolution is
    certain-and-wrong: `-ln(1-1)` is undefined, so `mean_log_score` must
    raise `ValueError`.
    """
    from windbreak.evaluation.metrics import mean_log_score

    inputs = EvaluationInputs(
        forecasts=(
            _forecast(
                forecast_id="fc-certain-wrong-no",
                market_ticker="MKT-CWN",
                probability_ppm=1_000_000,
                baseline_pips=9_999,
            ),
        ),
        resolutions={"MKT-CWN": ResolutionOutcome.NO},
    )

    with pytest.raises(ValueError, match=r"(?i)probability|log"):
        mean_log_score(inputs, window=_WINDOW)


# ---------------------------------------------------------------------------
# 3. brier_skill
# ---------------------------------------------------------------------------


def test_brier_skill_matches_hand_computation_on_synthetic_fixture() -> None:
    """`brier_skill` reproduces the fixture's exact hand-computed -49_375 ppm.

    Baseline probability is `baseline_executable_price_pips * 100` ppm.
    Per-forecast baseline terms `(baseline_p - o)^2`, in order:
        MKT-01 (0.88,yes)=0.0144  MKT-06 (0.48,no)=0.2304
        MKT-02 (0.15,no)=0.0225   MKT-07 (0.79,yes)=0.0441
        MKT-03 (0.72,yes)=0.0784  MKT-08 (0.22,no)=0.0484
        MKT-04 (0.32,no)=0.1024   MKT-09 (0.99,yes)=0.0001
        MKT-05 (0.55,yes)=0.2025  MKT-10 (0.01,no)=0.0001
    Sum = 0.7433 (743_300 in ppm units). Forecast-term sum is the fixture's
    own 0.78 (780_000 in ppm units; see `mean_brier`'s test above).
    skill = 1 - 780_000/743_300 = (743_300 - 780_000)/743_300 = -36_700/743_300
          = -367/7433 (dividing by 100)
    skill_ppm = floor(-367 * 1_000_000 / 7433) = floor(-49_374.41...) = -49_375
    (UNDERSTATE_EQUITY floors toward -infinity, so -49_374.41 floors to the
    more-negative -49_375).
    """
    from windbreak.evaluation.metrics import brier_skill

    result = brier_skill(_synthetic_inputs(), window=_WINDOW)

    assert result == _EXPECTED_BRIER_SKILL_PPM
    assert isinstance(result, int)


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=9_999),
            st.booleans(),
        ),
        min_size=1,
        max_size=6,
    )
)
def test_brier_skill_of_executable_baseline_against_itself_is_exactly_zero(
    rows: list[tuple[int, bool]],
) -> None:
    """A forecast whose probability exactly equals its own executable-price
    baseline (in ppm) demonstrates no skill over that baseline: the forecast
    and baseline terms are identical for every row, so the ratio is exactly
    `1` and `brier_skill` is exactly `0`, regardless of the (non-degenerate)
    probabilities chosen.

    `pips` is restricted to `[1, 9_999]` so the ppm probability is always
    strictly between `0` and `1_000_000`: this keeps every per-row baseline
    term strictly positive (`(p-o)^2 > 0` for `p` in `(0, 1)`), so the
    baseline-term sum can never be zero.
    """
    from windbreak.evaluation.metrics import brier_skill

    forecasts = tuple(
        _forecast(
            forecast_id=f"fc-{index}",
            market_ticker=f"MKT-{index}",
            probability_ppm=pips * 100,
            baseline_pips=pips,
        )
        for index, (pips, _outcome_yes) in enumerate(rows)
    )
    resolutions = {
        f"MKT-{index}": (ResolutionOutcome.YES if outcome_yes else ResolutionOutcome.NO)
        for index, (_pips, outcome_yes) in enumerate(rows)
    }
    inputs = EvaluationInputs(forecasts=forecasts, resolutions=resolutions)

    result = brier_skill(inputs, window=_WINDOW)

    assert result == 0


# ---------------------------------------------------------------------------
# 4. expected_calibration_error / calibration_slope / calibration_intercept /
#    sharpness -- a hand-built perfectly-calibrated 4-row construction.
# ---------------------------------------------------------------------------

#: Four forecasts whose observed frequency exactly equals their forecast
#: probability at every distinct probability level: p=0 -> 0/1 yes, p=1.0 ->
#: 1/1 yes, p=0.5 -> 1/2 yes. This is the textbook "perfectly calibrated"
#: construction the issue names explicitly.
_CALIBRATED_FORECASTS = (
    _forecast(
        forecast_id="cal-fc-0",
        market_ticker="CAL-0",
        probability_ppm=0,
        baseline_pips=0,
    ),
    _forecast(
        forecast_id="cal-fc-1",
        market_ticker="CAL-1",
        probability_ppm=1_000_000,
        baseline_pips=10_000,
    ),
    _forecast(
        forecast_id="cal-fc-half-yes",
        market_ticker="CAL-HALF-YES",
        probability_ppm=500_000,
        baseline_pips=5_000,
    ),
    _forecast(
        forecast_id="cal-fc-half-no",
        market_ticker="CAL-HALF-NO",
        probability_ppm=500_000,
        baseline_pips=5_000,
    ),
)
_CALIBRATED_RESOLUTIONS = {
    "CAL-0": ResolutionOutcome.NO,
    "CAL-1": ResolutionOutcome.YES,
    "CAL-HALF-YES": ResolutionOutcome.YES,
    "CAL-HALF-NO": ResolutionOutcome.NO,
}
_CALIBRATED_INPUTS = EvaluationInputs(
    forecasts=_CALIBRATED_FORECASTS, resolutions=_CALIBRATED_RESOLUTIONS
)


def test_expected_calibration_error_is_zero_on_perfectly_calibrated_set() -> None:
    """ECE is exactly `0` when every bin's mean forecast equals its observed
    frequency: bin 0 (p=0, 0/1 yes -> freq 0) diff 0; bin 5 (p=0.5, 1/2 yes ->
    freq 500_000) diff 0; bin 9 (p=1.0, 1/1 yes -> freq 1_000_000) diff 0.
    """
    from windbreak.evaluation.metrics import expected_calibration_error

    result = expected_calibration_error(_CALIBRATED_INPUTS, window=_WINDOW)

    assert result == 0


def test_calibration_slope_and_intercept_are_one_and_zero_on_calibrated_set() -> None:
    """OLS of outcome on forecast over the calibrated construction gives an
    exact slope of `1.0` (`1_000_000` ppm) and intercept `0`.

    Using p in probability units (0, 1, 0.5, 0.5) and o in {0, 1, 0, 1}:
    pbar = obar = 0.5. Deviations: p-pbar = (-0.5, 0.5, 0, 0); o-obar =
    (-0.5, 0.5, -0.5, 0.5). cov-numerator (sum of products, n cancels in the
    slope ratio) = 0.25 + 0.25 + 0 + 0 = 0.5. var-numerator (sum of squared
    p-deviations) = 0.25 + 0.25 + 0 + 0 = 0.5. slope = 0.5 / 0.5 = 1.0 exactly
    -> 1_000_000 ppm. intercept = obar - slope * pbar = 0.5 - 1*0.5 = 0.
    """
    from windbreak.evaluation.metrics import (
        calibration_intercept,
        calibration_slope,
    )

    slope = calibration_slope(_CALIBRATED_INPUTS, window=_WINDOW)
    intercept = calibration_intercept(_CALIBRATED_INPUTS, window=_WINDOW)

    assert slope == 1_000_000
    assert intercept == 0


def test_sharpness_matches_hand_computed_variance_on_calibrated_set() -> None:
    """Sharpness (variance of forecast probabilities) on the calibrated set:
    p values in ppm are (0, 1_000_000, 500_000, 500_000), mean 500_000;
    squared deviations are (250_000_000_000, 250_000_000_000, 0, 0), summing
    to 5e11; divided by (n * 1e6 = 4_000_000) gives exactly 125_000 ppm.
    """
    from windbreak.evaluation.metrics import sharpness

    result = sharpness(_CALIBRATED_INPUTS, window=_WINDOW)

    assert result == 125_000


def test_calibration_slope_raises_value_error_on_zero_variance() -> None:
    """When every forecast shares the same probability, `var(p) == 0` and the
    OLS slope is undefined: `calibration_slope` must raise `ValueError`
    rather than divide by zero silently or crash with `ZeroDivisionError`.
    """
    from windbreak.evaluation.metrics import calibration_slope

    inputs = EvaluationInputs(
        forecasts=(
            _forecast(
                forecast_id="fc-const-1",
                market_ticker="MKT-CONST-1",
                probability_ppm=500_000,
                baseline_pips=5_000,
            ),
            _forecast(
                forecast_id="fc-const-2",
                market_ticker="MKT-CONST-2",
                probability_ppm=500_000,
                baseline_pips=5_000,
            ),
        ),
        resolutions={
            "MKT-CONST-1": ResolutionOutcome.YES,
            "MKT-CONST-2": ResolutionOutcome.NO,
        },
    )

    with pytest.raises(ValueError, match=r"(?i)variance"):
        calibration_slope(inputs, window=_WINDOW)


# ---------------------------------------------------------------------------
# 5. reliability_diagram
# ---------------------------------------------------------------------------


def test_reliability_diagram_has_ten_contiguous_equal_width_bins() -> None:
    """`reliability_diagram` always returns exactly 10 bins, each spanning
    100_000 ppm, contiguous from 0 to 1_000_000.
    """
    from windbreak.evaluation.metrics import reliability_diagram

    bins = reliability_diagram(_CALIBRATED_INPUTS, window=_WINDOW)

    assert len(bins) == 10
    for index, one_bin in enumerate(bins):
        assert one_bin.bin_low_ppm == index * 100_000
        assert one_bin.bin_high_ppm == (index + 1) * 100_000


def test_reliability_diagram_populated_bins_match_hand_computation() -> None:
    """On the calibrated construction, bin 0 holds the single p=0 forecast
    (0/1 yes -> freq 0), bin 5 holds both p=0.5 forecasts (1/2 yes -> freq
    500_000), and bin 9 holds the single p=1.0 forecast (1/1 yes -> freq
    1_000_000); every other bin is empty. Bin index is `min(p//100_000, 9)`.
    """
    from windbreak.evaluation.metrics import reliability_diagram

    bins = reliability_diagram(_CALIBRATED_INPUTS, window=_WINDOW)
    by_index = dict(enumerate(bins))

    assert by_index[0].count == 1
    assert by_index[0].mean_forecast_ppm == 0
    assert by_index[0].observed_frequency_ppm == 0

    assert by_index[5].count == 2
    assert by_index[5].mean_forecast_ppm == 500_000
    assert by_index[5].observed_frequency_ppm == 500_000

    assert by_index[9].count == 1
    assert by_index[9].mean_forecast_ppm == 1_000_000
    assert by_index[9].observed_frequency_ppm == 1_000_000

    for index in (1, 2, 3, 4, 6, 7, 8):
        assert by_index[index].count == 0

    assert sum(one_bin.count for one_bin in bins) == 4


# ---------------------------------------------------------------------------
# 6. price_bucket_report
# ---------------------------------------------------------------------------

#: Four forecasts landing in three distinct price-deciles (0, 1, 9), with one
#: decile (1) holding two forecasts -- one traded, one not -- to pin the
#: "PnL over traded forecasts only" rule.
_PRICE_BUCKET_FORECASTS = (
    _forecast(
        forecast_id="pb-fc-1",
        market_ticker="MKT-PB-1",
        probability_ppm=600_000,
        baseline_pips=500,
        traded=True,
    ),
    _forecast(
        forecast_id="pb-fc-2",
        market_ticker="MKT-PB-2",
        probability_ppm=200_000,
        baseline_pips=1_500,
        traded=False,
        eligible=False,
        abstention="below_edge_threshold",
    ),
    _forecast(
        forecast_id="pb-fc-3",
        market_ticker="MKT-PB-3",
        probability_ppm=100_000,
        baseline_pips=1_600,
        traded=True,
    ),
    _forecast(
        forecast_id="pb-fc-4",
        market_ticker="MKT-PB-4",
        probability_ppm=900_000,
        baseline_pips=9_500,
        traded=True,
    ),
)
_PRICE_BUCKET_RESOLUTIONS = {
    "MKT-PB-1": ResolutionOutcome.YES,
    "MKT-PB-2": ResolutionOutcome.NO,
    "MKT-PB-3": ResolutionOutcome.NO,
    "MKT-PB-4": ResolutionOutcome.YES,
}
_PRICE_BUCKET_INPUTS = EvaluationInputs(
    forecasts=_PRICE_BUCKET_FORECASTS, resolutions=_PRICE_BUCKET_RESOLUTIONS
)


def test_price_bucket_report_has_ten_thousand_pip_wide_deciles() -> None:
    """`price_bucket_report` always returns 10 buckets of 1_000 pips each,
    contiguous from 0 to 10_000 pips.
    """
    from windbreak.evaluation.metrics import price_bucket_report

    buckets = price_bucket_report(_PRICE_BUCKET_INPUTS, window=_WINDOW)

    assert len(buckets) == 10
    for index, bucket in enumerate(buckets):
        assert bucket.bucket_low_pips == index * 1_000
        assert bucket.bucket_high_pips == (index + 1) * 1_000


def test_price_bucket_report_matches_hand_computation() -> None:
    """Hand-derived per-bucket stats for the four constructed forecasts:

    Bucket 0 (0-999 pips): pb-fc-1 only (ask 500). count=1,
        mean_forecast=600_000, freq=1_000_000 (yes), brier=(0.6-1)^2=0.16 ->
        160_000 ppm. PnL (traded, long-yes @ ask): 1*10_000 - 500 = 9_500.
    Bucket 1 (1000-1999 pips): pb-fc-2 (untraded, ask 1500) and pb-fc-3
        (traded, ask 1600). count=2, mean_forecast=(200_000+100_000)/2=
        150_000 (exact). Both resolve NO -> freq=0. Brier: pb-fc-2
        (0.2-0)^2=0.04=40_000 ppm; pb-fc-3 (0.1-0)^2=0.01=10_000 ppm; mean=
        (40_000+10_000)/2=25_000 (exact). PnL over TRADED only: pb-fc-2 is
        excluded (untraded); pb-fc-3 contributes 0*10_000 - 1_600 = -1_600.
    Bucket 9 (9000-9999 pips): pb-fc-4 only (ask 9500). count=1,
        mean_forecast=900_000, freq=1_000_000 (yes), brier=(0.9-1)^2=0.01 ->
        10_000 ppm. PnL (traded): 1*10_000 - 9_500 = 500.
    All other buckets are empty (count=0).
    """
    from windbreak.evaluation.metrics import price_bucket_report

    buckets = price_bucket_report(_PRICE_BUCKET_INPUTS, window=_WINDOW)
    by_index = dict(enumerate(buckets))

    bucket0 = by_index[0]
    assert bucket0.count == 1
    assert bucket0.mean_forecast_ppm == 600_000
    assert bucket0.observed_frequency_ppm == 1_000_000
    assert bucket0.brier_ppm == 160_000
    assert bucket0.pnl_pips == 9_500

    bucket1 = by_index[1]
    assert bucket1.count == 2
    assert bucket1.mean_forecast_ppm == 150_000
    assert bucket1.observed_frequency_ppm == 0
    assert bucket1.brier_ppm == 25_000
    assert bucket1.pnl_pips == -1_600

    bucket9 = by_index[9]
    assert bucket9.count == 1
    assert bucket9.mean_forecast_ppm == 900_000
    assert bucket9.observed_frequency_ppm == 1_000_000
    assert bucket9.brier_ppm == 10_000
    assert bucket9.pnl_pips == 500

    for index in (2, 3, 4, 5, 6, 7, 8):
        assert by_index[index].count == 0


# ---------------------------------------------------------------------------
# 7. edge_bucket_report
# ---------------------------------------------------------------------------

#: Four forecasts whose edge = p_ppm - baseline_ppm lands in four distinct
#: symmetric edge buckets, each with a distinct traded status.
_EDGE_BUCKET_FORECASTS = (
    _forecast(
        forecast_id="eb-fc-1",
        market_ticker="MKT-EB-1",
        probability_ppm=200_000,
        baseline_pips=100,
        traded=True,
    ),
    _forecast(
        forecast_id="eb-fc-2",
        market_ticker="MKT-EB-2",
        probability_ppm=100_000,
        baseline_pips=2_000,
        traded=False,
        eligible=False,
        abstention="below_edge_threshold",
    ),
    _forecast(
        forecast_id="eb-fc-3",
        market_ticker="MKT-EB-3",
        probability_ppm=400_000,
        baseline_pips=3_800,
        traded=True,
    ),
    _forecast(
        forecast_id="eb-fc-4",
        market_ticker="MKT-EB-4",
        probability_ppm=900_000,
        baseline_pips=9_200,
        traded=True,
    ),
)
_EDGE_BUCKET_RESOLUTIONS = {
    "MKT-EB-1": ResolutionOutcome.YES,
    "MKT-EB-2": ResolutionOutcome.NO,
    "MKT-EB-3": ResolutionOutcome.NO,
    "MKT-EB-4": ResolutionOutcome.YES,
}
_EDGE_BUCKET_INPUTS = EvaluationInputs(
    forecasts=_EDGE_BUCKET_FORECASTS, resolutions=_EDGE_BUCKET_RESOLUTIONS
)


def test_edge_bucket_report_has_six_symmetric_buckets() -> None:
    """The six symmetric edge-bucket boundaries are `(-1_000_000, -100_000,
    -50_000, 0, 50_000, 100_000, 1_000_000)`.
    """
    from windbreak.evaluation.metrics import edge_bucket_report

    boundaries = (-1_000_000, -100_000, -50_000, 0, 50_000, 100_000, 1_000_000)
    buckets = edge_bucket_report(_EDGE_BUCKET_INPUTS, window=_WINDOW)

    assert len(buckets) == 6
    for index, bucket in enumerate(buckets):
        assert bucket.bucket_low_ppm == boundaries[index]
        assert bucket.bucket_high_ppm == boundaries[index + 1]


def test_edge_bucket_report_matches_hand_computation() -> None:
    """Hand-derived edges (edge = p_ppm - baseline_ppm, baseline_ppm =
    baseline_pips * 100):

    eb-fc-1: p=200_000, baseline=100*100=10_000 -> edge=190_000 (bucket
        [100_000, 1_000_000)). Outcome YES: brier=(0.2-1)^2=0.64 ->
        640_000 ppm, freq=1_000_000. Traded: PnL=1*10_000-100=9_900.
    eb-fc-2: p=100_000, baseline=2_000*100=200_000 -> edge=-100_000 (bucket
        [-100_000, -50_000)). Outcome NO: brier=(0.1-0)^2=0.01 -> 10_000 ppm,
        freq=0. Untraded: PnL=0 (no traded forecasts in this bucket).
    eb-fc-3: p=400_000, baseline=3_800*100=380_000 -> edge=20_000 (bucket
        [0, 50_000)). Outcome NO: brier=(0.4-0)^2=0.16 -> 160_000 ppm,
        freq=0. Traded: PnL=0*10_000-3_800=-3_800.
    eb-fc-4: p=900_000, baseline=9_200*100=920_000 -> edge=-20_000 (bucket
        [-50_000, 0)). Outcome YES: brier=(0.9-1)^2=0.01 -> 10_000 ppm,
        freq=1_000_000. Traded: PnL=1*10_000-9_200=800.
    The remaining two buckets ([-1_000_000,-100_000) and [50_000,100_000))
    are empty.
    """
    from windbreak.evaluation.metrics import edge_bucket_report

    buckets = edge_bucket_report(_EDGE_BUCKET_INPUTS, window=_WINDOW)
    by_index = dict(enumerate(buckets))

    bucket_minus_100k = by_index[1]
    assert bucket_minus_100k.count == 1
    assert bucket_minus_100k.mean_edge_ppm == -100_000
    assert bucket_minus_100k.brier_ppm == 10_000
    assert bucket_minus_100k.observed_frequency_ppm == 0
    assert bucket_minus_100k.pnl_pips == 0

    bucket_minus_20k = by_index[2]
    assert bucket_minus_20k.count == 1
    assert bucket_minus_20k.mean_edge_ppm == -20_000
    assert bucket_minus_20k.brier_ppm == 10_000
    assert bucket_minus_20k.observed_frequency_ppm == 1_000_000
    assert bucket_minus_20k.pnl_pips == 800

    bucket_20k = by_index[3]
    assert bucket_20k.count == 1
    assert bucket_20k.mean_edge_ppm == 20_000
    assert bucket_20k.brier_ppm == 160_000
    assert bucket_20k.observed_frequency_ppm == 0
    assert bucket_20k.pnl_pips == -3_800

    bucket_190k = by_index[5]
    assert bucket_190k.count == 1
    assert bucket_190k.mean_edge_ppm == 190_000
    assert bucket_190k.brier_ppm == 640_000
    assert bucket_190k.observed_frequency_ppm == 1_000_000
    assert bucket_190k.pnl_pips == 9_900

    for index in (0, 4):
        assert by_index[index].count == 0


# ---------------------------------------------------------------------------
# 8. power_analysis (windbreak.evaluation.power) -- lives here per the file
#    fence in this issue's instructions, not in a separate test_power.py.
# ---------------------------------------------------------------------------


def test_power_analysis_module_constants_have_the_documented_defaults() -> None:
    """`POWER_TARGET_N` is 300 and `POWER_TARGET_PPM` is 800_000 (80%
    power), the documented default target sample size and power level.
    """
    from windbreak.evaluation.power import POWER_TARGET_N, POWER_TARGET_PPM

    assert POWER_TARGET_N == 300
    assert POWER_TARGET_PPM == 800_000


def test_power_analysis_returns_positive_min_detectable_skill() -> None:
    """`power_analysis` returns a `PowerAnalysis` whose
    `min_detectable_brier_skill_ppm` is a strictly positive `int` -- the
    minimum detectable effect size at the target sample size is a meaningful
    positive threshold, never zero or negative.
    """
    from windbreak.evaluation.power import power_analysis

    result = power_analysis(_synthetic_inputs(), seed=7, window=_WINDOW)

    assert isinstance(result.min_detectable_brier_skill_ppm, int)
    assert result.min_detectable_brier_skill_ppm > 0
    assert result.seed == 7


def test_power_analysis_is_deterministic_for_a_fixed_seed() -> None:
    """Two calls with identical inputs and seed produce byte-identical
    results (SPEC S3.5: identical inputs + seed -> identical output).
    """
    from windbreak.evaluation.power import power_analysis

    first = power_analysis(_synthetic_inputs(), seed=99, window=_WINDOW)
    second = power_analysis(_synthetic_inputs(), seed=99, window=_WINDOW)

    assert first == second


def test_power_analysis_render_text_contains_mde_and_seed() -> None:
    """`PowerAnalysis.render_text()` includes both the minimum-detectable-
    effect integer and the seed used to produce it, so a report reader can
    audit reproducibility without re-running the analysis.
    """
    from windbreak.evaluation.power import power_analysis

    result = power_analysis(_synthetic_inputs(), seed=42, window=_WINDOW)
    text = result.render_text()

    assert str(result.min_detectable_brier_skill_ppm) in text
    assert str(result.seed) in text


def test_power_analysis_rejects_confidence_ppm_outside_open_unit_interval() -> None:
    """A `confidence_ppm` of `0` or `1_000_000` (the closed boundary) is
    rejected: a confidence level must be strictly between 0% and 100%.
    """
    from windbreak.evaluation.power import power_analysis

    with pytest.raises(ValueError, match="confidence_ppm"):
        power_analysis(_synthetic_inputs(), seed=1, confidence_ppm=0, window=_WINDOW)

    with pytest.raises(ValueError, match="confidence_ppm"):
        power_analysis(
            _synthetic_inputs(), seed=1, confidence_ppm=1_000_000, window=_WINDOW
        )


def test_run_evaluation_report_includes_power_section() -> None:
    """`run_evaluation`'s report carries a non-`None` `power` analysis whose
    `render_text()` output (including its `== power ==` banner) appears in
    the full report text, and is reproducible across repeated runs against
    the same fixture (SPEC S3.5: fixed module seed constant).
    """
    from windbreak.evaluation import run_evaluation

    first_report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)
    second_report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)

    assert first_report.power is not None
    assert first_report.power == second_report.power
    assert "== power ==" in first_report.render_text()
    assert str(first_report.power.min_detectable_brier_skill_ppm) in (
        first_report.render_text()
    )
