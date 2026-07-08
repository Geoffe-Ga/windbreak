"""Failing-first tests for `hedgekit.evaluation` (issue #49, RED).

`hedgekit.evaluation` does not exist yet, so every import below fails
collection with `ModuleNotFoundError: No module named 'hedgekit.evaluation'`
-- the expected Gate 1 RED state for issue #49.

Pins the three-track evaluation report skeleton (SPEC-EPIC_07):

- ``run_evaluation(*, fixture_path)`` loads a known-answer JSON fixture and
  returns an ``EvaluationReport`` with exactly one ``TrackReport`` per
  ``Track`` (forecast, selection, execution), each carrying one
  ``MetricResult`` per metric registered in that track's window.
- Every metric in ``registered_metrics()`` renders exactly once in
  ``EvaluationReport.render_text()`` -- no silent omission of an
  unimplemented metric.
- The renderer prints the literal sentinel string ``NOT_IMPLEMENTED`` for
  metrics whose ``compute`` has not been wired yet, and prints
  ``NO_EDGE_BANNER`` under the forecast track whenever the headline skill
  metric (``brier_skill_vs_executable_price``) resolves to an ``int`` <= 0
  -- bluntly, with no hedging language.
- ``FixtureForecast`` and the JSON loader reject out-of-range probabilities,
  bool-as-int numeric fields, unknown resolution outcomes, and duplicate
  resolution tickers, naming the offending field in every error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The epic-wide known-answer fixture shared by issues #49-#55; see the
#: fixture's own "description" and "expected" keys for the hand-computed
#: Brier arithmetic this suite pins against.
SYNTHETIC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_known_answer.json"
)

#: The three track names, exactly as `Track.value` must render them.
_TRACK_NAMES = frozenset({"forecast", "selection", "execution"})


def _assert_no_float_leaves(value: object, *, path: str = "$") -> None:
    """Recursively assert that no leaf in a decoded JSON structure is a float.

    Args:
        value: A JSON-decoded value (dict, list, or scalar) to inspect.
        path: A breadcrumb path used to make failures locatable.

    Raises:
        AssertionError: If any leaf in the structure is a ``float``.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_no_float_leaves(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_float_leaves(child, path=f"{path}[{index}]")
    else:
        assert not isinstance(value, float), f"float leaf found at {path}: {value!r}"


# ---------------------------------------------------------------------------
# 1. Verbatim per-issue scenario: the fixture renders "no edge" bluntly.
# ---------------------------------------------------------------------------


def test_three_track_report_renders_no_edge_bluntly() -> None:
    """The synthetic known-answer fixture renders a blunt NO EDGE banner.

    `brier_skill_vs_executable_price` is now a real computation (issue #51):
    over the synthetic fixture it resolves to the exact `int` `-49_375`
    (see `test_metrics.py`'s
    `test_brier_skill_matches_hand_computation_on_synthetic_fixture`
    for the full hand-derived arithmetic) -- and `-49_375 <= 0`, so the
    forecast-track section must still print the literal `NO_EDGE_BANNER`
    text, not a hedge like "inconclusive" or "insufficient data".
    """
    from hedgekit.evaluation import NO_EDGE_BANNER, run_evaluation

    report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)
    text = report.render_text()

    assert NO_EDGE_BANNER in text
    assert NO_EDGE_BANNER == "NO EDGE DEMONSTRATED"


# ---------------------------------------------------------------------------
# 2. End-to-end smoke: run_evaluation wires all three tracks.
# ---------------------------------------------------------------------------


def test_run_evaluation_returns_exactly_three_uniquely_named_tracks() -> None:
    """`run_evaluation` returns one `TrackReport` per `Track`, no dupes."""
    from hedgekit.evaluation import EvaluationReport, run_evaluation

    report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)

    assert isinstance(report, EvaluationReport)
    assert len(report.tracks) == 3
    names = [track.name for track in report.tracks]
    assert set(names) == _TRACK_NAMES
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# 3. No silent omission of a registered metric.
# ---------------------------------------------------------------------------


def test_every_registered_metric_appears_exactly_once_and_renders() -> None:
    """Every name in `registered_metrics()` appears exactly once across all
    tracks, has a value that is either an `int` or the `NOT_IMPLEMENTED`
    sentinel, and shows up verbatim in `render_text()`.
    """
    from hedgekit.evaluation import (
        NOT_IMPLEMENTED,
        registered_metrics,
        run_evaluation,
    )

    report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)
    text = report.render_text()

    all_results = [metric for track in report.tracks for metric in track.metrics]
    rendered_names = [result.name for result in all_results]

    expected_names = set(registered_metrics().keys())
    assert set(rendered_names) == expected_names
    assert len(rendered_names) == len(set(rendered_names))

    for result in all_results:
        assert isinstance(result.value, int) or result.value is NOT_IMPLEMENTED
        assert result.name in text


# ---------------------------------------------------------------------------
# 4. Renderer, both directions, constructed directly (no loader involved).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headline_value", "banner_expected"),
    [
        (0, True),
        (-125_000, True),
        (1, False),
        (500_000, False),
    ],
)
def test_render_text_no_edge_banner_gates_on_headline_skill_sign(
    headline_value: int, banner_expected: bool
) -> None:
    """The NO EDGE banner appears under the forecast track iff the headline
    skill metric is a non-positive `int` (`<= 0`); a positive `int` renders
    with no banner.
    """
    from hedgekit.evaluation import (
        HEADLINE_SKILL_METRIC,
        NO_EDGE_BANNER,
        EvaluationReport,
        MetricResult,
        ObservationWindow,
        Track,
        TrackReport,
    )

    forecast_track = TrackReport(
        name=Track.FORECAST.value,
        metrics=(
            MetricResult(
                name=HEADLINE_SKILL_METRIC,
                window=ObservationWindow.LATEST_BEFORE_CLOSE,
                value=headline_value,
            ),
        ),
    )
    report = EvaluationReport(
        tracks=(
            forecast_track,
            TrackReport(name=Track.SELECTION.value, metrics=()),
            TrackReport(name=Track.EXECUTION.value, metrics=()),
        )
    )

    text = report.render_text()

    assert (NO_EDGE_BANNER in text) is banner_expected


def test_render_text_not_implemented_sentinel_never_triggers_the_banner() -> None:
    """A `NOT_IMPLEMENTED` headline value renders the literal sentinel text
    and never the NO EDGE banner -- "not measured yet" is not "no edge".
    """
    from hedgekit.evaluation import (
        HEADLINE_SKILL_METRIC,
        NO_EDGE_BANNER,
        NOT_IMPLEMENTED,
        EvaluationReport,
        MetricResult,
        ObservationWindow,
        Track,
        TrackReport,
    )

    report = EvaluationReport(
        tracks=(
            TrackReport(
                name=Track.FORECAST.value,
                metrics=(
                    MetricResult(
                        name=HEADLINE_SKILL_METRIC,
                        window=ObservationWindow.LATEST_BEFORE_CLOSE,
                        value=NOT_IMPLEMENTED,
                    ),
                ),
            ),
            TrackReport(name=Track.SELECTION.value, metrics=()),
            TrackReport(name=Track.EXECUTION.value, metrics=()),
        )
    )

    text = report.render_text()

    assert "NOT_IMPLEMENTED" in text
    assert NO_EDGE_BANNER not in text


def test_evaluation_report_post_init_rejects_missing_or_duplicate_tracks() -> None:
    """`EvaluationReport.__post_init__` requires exactly the three
    `Track.value` names, each exactly once.
    """
    from hedgekit.evaluation import EvaluationReport, Track, TrackReport

    with pytest.raises(ValueError, match="track"):
        EvaluationReport(
            tracks=(
                TrackReport(name=Track.FORECAST.value, metrics=()),
                TrackReport(name=Track.SELECTION.value, metrics=()),
            )
        )

    with pytest.raises(ValueError, match="track"):
        EvaluationReport(
            tracks=(
                TrackReport(name=Track.FORECAST.value, metrics=()),
                TrackReport(name=Track.FORECAST.value, metrics=()),
                TrackReport(name=Track.EXECUTION.value, metrics=()),
            )
        )


# ---------------------------------------------------------------------------
# 5. Registry typing: every spec is well-formed; seed stubs return as
#    documented when invoked with a minimal EvaluationInputs.
# ---------------------------------------------------------------------------


def test_registered_metrics_has_the_nine_seed_specs_with_correct_shape() -> None:
    """Each of the nine seed `MetricSpec`s carries a real `Track`, a real
    `ObservationWindow`, and a callable `compute`; the headline metric's
    window is `LATEST_BEFORE_CLOSE`.

    Issue #51 registers five additional real forecast-track metrics
    (`log_score`, `expected_calibration_error`, `calibration_slope`,
    `calibration_intercept`, `sharpness`) alongside the original four seed
    slots, growing the registry from four specs to nine. Issue #53 turns
    `traded_vs_skipped_brier_delta` into a real computation too (delegating
    to `hedgekit.evaluation.cohorts`) and moves its window from
    `TRADE_TRIGGERING` to `LATEST_BEFORE_CLOSE`; only
    `fill_vs_model_slippage` remains unimplemented.
    """
    from hedgekit.evaluation import (
        HEADLINE_SKILL_METRIC,
        ObservationWindow,
        Track,
        registered_metrics,
    )

    metrics = registered_metrics()

    expected_names = {
        "brier",
        "brier_skill_vs_executable_price",
        "traded_vs_skipped_brier_delta",
        "fill_vs_model_slippage",
        "log_score",
        "expected_calibration_error",
        "calibration_slope",
        "calibration_intercept",
        "sharpness",
    }
    assert set(metrics.keys()) == expected_names

    for name, spec in metrics.items():
        assert spec.name == name
        assert isinstance(spec.track, Track)
        assert isinstance(spec.window, ObservationWindow)
        assert callable(spec.compute)

    headline_spec = metrics[HEADLINE_SKILL_METRIC]
    assert headline_spec.window == ObservationWindow.LATEST_BEFORE_CLOSE
    assert headline_spec.track == Track.FORECAST

    selection_spec = metrics["traded_vs_skipped_brier_delta"]
    assert selection_spec.window == ObservationWindow.LATEST_BEFORE_CLOSE
    assert selection_spec.track == Track.SELECTION


def test_seed_metric_compute_stub_returns_the_documented_value() -> None:
    """`fill_vs_model_slippage`, the one seed metric still unimplemented,
    returns the documented `NOT_IMPLEMENTED` sentinel when called with a
    minimal, empty `EvaluationInputs`.

    `brier` and `brier_skill_vs_executable_price` were dropped from this
    check as of issue #51 (both are real computations, see
    `test_metrics.py`), and `traded_vs_skipped_brier_delta` is dropped as of
    issue #53: it now delegates to `hedgekit.evaluation.cohorts` and is a
    real `int` computation too, so a "documented stub value" assertion no
    longer applies to it either -- see `test_cohorts.py` for its exact
    hand-computed values.
    """
    from hedgekit.evaluation import (
        NOT_IMPLEMENTED,
        EvaluationInputs,
        registered_metrics,
    )

    inputs = EvaluationInputs(forecasts=(), resolutions={})
    spec = registered_metrics()["fill_vs_model_slippage"]

    result = spec.compute(inputs)

    assert result is NOT_IMPLEMENTED


def test_registered_metrics_returns_a_mapping_with_no_duplicate_names() -> None:
    """`registered_metrics()` is keyed by metric name with no collisions.

    The seed set is fixed and known not to collide; this pins the observable
    guarantee (a `Mapping[str, MetricSpec]` cannot silently drop a duplicate
    name the way a naive list-to-dict conversion could). The duplicate-name
    `ValueError` itself is an internal construction-time invariant with no
    public entry point to trigger from outside the seed set, and is left as
    a follow-up for whoever builds the registry to unit-test at that layer.
    """
    from hedgekit.evaluation import registered_metrics

    metrics = registered_metrics()

    names = list(metrics.keys())
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# 6. Fixture validation error paths, each naming the offending field.
# ---------------------------------------------------------------------------


def _write_fixture(tmp_path: Path, payload: Mapping[str, Any]) -> Path:
    """Write a JSON fixture payload to a temp file and return its path.

    Args:
        tmp_path: pytest's per-test temporary directory.
        payload: The JSON-serializable fixture body.

    Returns:
        The path to the written fixture file.
    """
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


_VALID_FORECAST: dict[str, Any] = {
    "forecast_id": "fc-1",
    "market_ticker": "MKT-X",
    "probability_ppm": 500000,
    "eligible_for_live": True,
    "abstention_reason": None,
    "traded": True,
    "baseline_executable_price_pips": 5000,
}


def test_run_evaluation_missing_forecasts_key_raises_value_error(
    tmp_path: Path,
) -> None:
    """A fixture with no `forecasts` key raises `ValueError` naming it."""
    from hedgekit.evaluation import run_evaluation

    path = _write_fixture(tmp_path, {"resolutions": []})

    with pytest.raises(ValueError, match="forecasts"):
        run_evaluation(fixture_path=path)


def test_run_evaluation_missing_resolutions_key_raises_value_error(
    tmp_path: Path,
) -> None:
    """A fixture with no `resolutions` key raises `ValueError` naming it."""
    from hedgekit.evaluation import run_evaluation

    path = _write_fixture(tmp_path, {"forecasts": []})

    with pytest.raises(ValueError, match="resolutions"):
        run_evaluation(fixture_path=path)


def test_run_evaluation_out_of_range_probability_ppm_raises_value_error(
    tmp_path: Path,
) -> None:
    """A `probability_ppm` outside `[0, 1_000_000]` raises `ValueError`
    naming the `probability_ppm` field.
    """
    from hedgekit.evaluation import run_evaluation

    bad_forecast = {**_VALID_FORECAST, "probability_ppm": 1_000_001}
    path = _write_fixture(
        tmp_path,
        {
            "forecasts": [bad_forecast],
            "resolutions": [{"market_ticker": "MKT-X", "outcome": "yes"}],
        },
    )

    with pytest.raises(ValueError, match="probability_ppm"):
        run_evaluation(fixture_path=path)


def test_run_evaluation_bool_masquerading_as_int_raises_type_error(
    tmp_path: Path,
) -> None:
    """A JSON `true`/`false` in `probability_ppm` -- decoded as a Python
    `bool`, an `int` subclass -- raises `TypeError` naming the field, per
    the repo-wide "no bool-as-int" rule (see `hedgekit.numeric.types`).
    """
    from hedgekit.evaluation import run_evaluation

    bad_forecast = {**_VALID_FORECAST, "probability_ppm": True}
    path = _write_fixture(
        tmp_path,
        {
            "forecasts": [bad_forecast],
            "resolutions": [{"market_ticker": "MKT-X", "outcome": "yes"}],
        },
    )

    with pytest.raises(TypeError, match="probability_ppm"):
        run_evaluation(fixture_path=path)


def test_run_evaluation_unknown_resolution_outcome_raises_value_error(
    tmp_path: Path,
) -> None:
    """A resolution `outcome` other than `"yes"`/`"no"` raises `ValueError`
    naming the `outcome` field.
    """
    from hedgekit.evaluation import run_evaluation

    path = _write_fixture(
        tmp_path,
        {
            "forecasts": [_VALID_FORECAST],
            "resolutions": [{"market_ticker": "MKT-X", "outcome": "maybe"}],
        },
    )

    with pytest.raises(ValueError, match="outcome"):
        run_evaluation(fixture_path=path)


def test_run_evaluation_duplicate_resolution_ticker_raises_value_error(
    tmp_path: Path,
) -> None:
    """Two resolutions for the same `market_ticker` raise `ValueError`
    naming the `market_ticker` field.
    """
    from hedgekit.evaluation import run_evaluation

    path = _write_fixture(
        tmp_path,
        {
            "forecasts": [_VALID_FORECAST],
            "resolutions": [
                {"market_ticker": "MKT-X", "outcome": "yes"},
                {"market_ticker": "MKT-X", "outcome": "no"},
            ],
        },
    )

    with pytest.raises(ValueError, match="market_ticker"):
        run_evaluation(fixture_path=path)


# ---------------------------------------------------------------------------
# 7. Fixture integrity: the synthetic JSON itself is well-formed and
#    the hand-computed expected value is auditable.
# ---------------------------------------------------------------------------


def test_synthetic_fixture_loads_as_valid_json() -> None:
    """The fixture file parses as JSON without error."""
    payload = json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))

    assert isinstance(payload, dict)
    assert "forecasts" in payload
    assert "resolutions" in payload
    assert "expected" in payload


def test_synthetic_fixture_has_at_least_one_abstained_and_one_traded_forecast() -> None:
    """The fixture mixes traded and abstained forecasts, per the epic's
    known-answer coverage requirement.
    """
    payload = json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))
    forecasts = payload["forecasts"]

    abstained = [
        forecast
        for forecast in forecasts
        if forecast["traded"] is False and forecast["abstention_reason"] is not None
    ]
    traded = [forecast for forecast in forecasts if forecast["traded"] is True]

    assert len(abstained) >= 1
    assert len(traded) >= 1


def test_synthetic_fixture_has_no_float_leaf_anywhere() -> None:
    """Every numeric leaf in the fixture is an `int` -- SPEC S6.1 bans
    floats on the probability/money path, including in test fixtures.
    """
    payload = json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))

    _assert_no_float_leaves(payload)


def test_synthetic_fixture_expected_brier_mean_ppm_matches_hand_computation() -> None:
    """The fixture's `expected.brier_mean_ppm` equals an independent
    hand computation over its 10 forecasts and resolutions.

    Per-forecast Brier term = (probability_ppm / 1_000_000 - outcome)^2,
    where outcome is 1 for "yes" and 0 for "no". All ten probabilities here
    are multiples of 100_000 ppm (0.1), so every term is an exact multiple
    of 0.01:

        MKT-01 p=0.9 o=1 -> 0.01   MKT-06 p=0.5 o=0 -> 0.25
        MKT-02 p=0.1 o=0 -> 0.01   MKT-07 p=0.8 o=1 -> 0.04
        MKT-03 p=0.7 o=1 -> 0.09   MKT-08 p=0.2 o=0 -> 0.04
        MKT-04 p=0.3 o=0 -> 0.09   MKT-09 p=1.0 o=1 -> 0.00
        MKT-05 p=0.5 o=1 -> 0.25   MKT-10 p=0.0 o=0 -> 0.00

    Sum = 0.78; mean over 10 = 0.078; ppm-scaled (x1_000_000) = 78_000.
    """
    payload = json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))
    forecasts = payload["forecasts"]
    outcome_by_ticker = {
        r["market_ticker"]: r["outcome"] for r in payload["resolutions"]
    }

    # Integer-only recomputation: accumulate squared-diff numerators scaled
    # by 1_000_000^2, then reduce, to avoid any float arithmetic even here.
    scale = 1_000_000
    total_scaled = 0
    for forecast in forecasts:
        p_ppm = forecast["probability_ppm"]
        outcome = outcome_by_ticker[forecast["market_ticker"]]
        outcome_ppm = scale if outcome == "yes" else 0
        diff_ppm = p_ppm - outcome_ppm
        total_scaled += diff_ppm * diff_ppm

    # total_scaled is in units of ppm^2; dividing by (scale * len) converts
    # back to a ppm-scaled mean: sum((diff/scale)^2) / n * scale
    #   == total_scaled / scale^2 / n * scale == total_scaled / (scale * n)
    brier_mean_ppm = total_scaled // (scale * len(forecasts))

    assert brier_mean_ppm == payload["expected"]["brier_mean_ppm"]
    assert payload["expected"]["brier_mean_ppm"] == 78000


# ---------------------------------------------------------------------------
# 8. resolutions_from_fixture: direct unit test against the JSON schema.
# ---------------------------------------------------------------------------


def test_resolutions_from_fixture_returns_exact_typed_resolutions() -> None:
    """`resolutions_from_fixture` maps each `{market_ticker, outcome}` entry
    to a `ResolutionOutcome`-valued mapping, matching the input exactly.
    """
    from hedgekit.evaluation.resolution import (
        ResolutionOutcome,
        resolutions_from_fixture,
    )

    fixture = {
        "resolutions": [
            {"market_ticker": "MKT-A", "outcome": "yes"},
            {"market_ticker": "MKT-B", "outcome": "no"},
        ],
    }

    resolutions = resolutions_from_fixture(fixture)

    assert resolutions == {
        "MKT-A": ResolutionOutcome.YES,
        "MKT-B": ResolutionOutcome.NO,
    }
    for outcome in resolutions.values():
        assert isinstance(outcome, ResolutionOutcome)


def test_resolutions_from_fixture_rejects_duplicate_ticker() -> None:
    """A duplicate `market_ticker` across resolutions raises `ValueError`
    naming the `market_ticker` field.
    """
    from hedgekit.evaluation.resolution import resolutions_from_fixture

    fixture = {
        "resolutions": [
            {"market_ticker": "MKT-A", "outcome": "yes"},
            {"market_ticker": "MKT-A", "outcome": "no"},
        ],
    }

    with pytest.raises(ValueError, match="market_ticker"):
        resolutions_from_fixture(fixture)


def test_resolutions_from_fixture_rejects_unknown_outcome() -> None:
    """An `outcome` string other than `"yes"`/`"no"` raises `ValueError`
    naming the `outcome` field.
    """
    from hedgekit.evaluation.resolution import resolutions_from_fixture

    fixture = {"resolutions": [{"market_ticker": "MKT-A", "outcome": "unresolved"}]}

    with pytest.raises(ValueError, match="outcome"):
        resolutions_from_fixture(fixture)


# ---------------------------------------------------------------------------
# FixtureForecast validation, constructed directly.
# ---------------------------------------------------------------------------


def test_fixture_forecast_rejects_out_of_range_probability_ppm() -> None:
    """`FixtureForecast.__post_init__` rejects a `probability_ppm` outside
    `[0, 1_000_000]`, naming the field.
    """
    from hedgekit.evaluation import FixtureForecast
    from hedgekit.numeric.types import ProbabilityPpm

    with pytest.raises(ValueError, match="probability_ppm"):
        FixtureForecast(
            forecast_id="fc-1",
            market_ticker="MKT-X",
            probability_ppm=ProbabilityPpm(-1),
            eligible_for_live=True,
            abstention_reason=None,
            traded=True,
            baseline_executable_price_pips=1000,
        )

    with pytest.raises(ValueError, match="probability_ppm"):
        FixtureForecast(
            forecast_id="fc-1",
            market_ticker="MKT-X",
            probability_ppm=ProbabilityPpm(1_000_001),
            eligible_for_live=True,
            abstention_reason=None,
            traded=True,
            baseline_executable_price_pips=1000,
        )


def test_fixture_forecast_rejects_bool_as_baseline_price() -> None:
    """`FixtureForecast.__post_init__` rejects a `bool` masquerading as
    `baseline_executable_price_pips`, naming the field -- the same
    "no bool-as-int" rule `hedgekit.numeric.types._IntUnit` already
    enforces for the wrapped unit types.
    """
    from hedgekit.evaluation import FixtureForecast
    from hedgekit.numeric.types import ProbabilityPpm

    with pytest.raises(TypeError, match="baseline_executable_price_pips"):
        FixtureForecast(
            forecast_id="fc-1",
            market_ticker="MKT-X",
            probability_ppm=ProbabilityPpm(500_000),
            eligible_for_live=True,
            abstention_reason=None,
            traded=True,
            baseline_executable_price_pips=True,
        )
