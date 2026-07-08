"""Failing-first tests for issue #58's registry/cohorts extensions (RED).

Unlike most of this suite's siblings, the symbols under test here
(`FixtureForecast.live`, `EvaluationInputs.execution_records`, the two new
`live_slippage_ratio` / `live_brier_degradation` `MetricSpec`s) are *additions*
to the already-existing `windbreak.evaluation.registry` module rather than a
brand-new module, so there is no `ModuleNotFoundError` collection failure to
rely on. Every test below therefore fails instead on a `TypeError:
__init__() got an unexpected keyword argument 'live'` (or `'execution_records'`)
at the first construction call inside the test body, or on a plain `KeyError`
/ `AssertionError` when a not-yet-registered metric name is looked up --
either way the expected Gate 1 RED state for issue #58's registry-level
changes. `windbreak.evaluation.registry` itself is imported at module scope
(it already exists); the two new metric names are looked up as plain strings
rather than imported symbols, since they name registry entries, not importable
objects.

Pins:

- `FixtureForecast.live` defaults to `False` (every pre-#58 construction call
  keeps working unchanged) and accepts an explicit `True`.
- `EvaluationInputs.execution_records` defaults to `()` and threads unchanged
  through both `gate_evaluation_inputs` and the private per-window narrowing
  helper `registry._windowed` -- the two seams the architecture plan names
  explicitly -- so a windowed/gated view never silently drops a run's
  execution-quality records.
- `registered_metrics()` grows from 9 to 11 entries, gaining
  `"live_slippage_ratio"` and `"live_brier_degradation"`, and every metric
  (old and new alike) still passes through the single temporal-gating choke
  point (`MetricSpec.__post_init__`'s `gated_compute` wrapper) -- verified via
  a pre-deployment (rejected) forecast never reaching either new metric's
  `compute`, mirroring the existing metrics' documented gating guarantee.
"""

from __future__ import annotations

from windbreak.evaluation import registry
from windbreak.evaluation.registry import (
    EvaluationInputs,
    FixtureForecast,
    gate_evaluation_inputs,
    registered_metrics,
)
from windbreak.evaluation.resolution import ResolutionOutcome
from windbreak.evaluation.temporal import TemporalContext
from windbreak.evaluation.windows import ObservationWindow
from windbreak.numeric.types import ProbabilityPpm

#: The nine metric names registered before issue #58.
_PRE_58_METRIC_NAMES = frozenset(
    {
        "brier",
        "brier_skill_vs_executable_price",
        "log_score",
        "expected_calibration_error",
        "calibration_slope",
        "calibration_intercept",
        "sharpness",
        "traded_vs_skipped_brier_delta",
        "fill_vs_model_slippage",
    }
)

#: The two metric names issue #58 adds.
_NEW_METRIC_NAMES = frozenset({"live_slippage_ratio", "live_brier_degradation"})


def _forecast(
    forecast_id: str,
    market_ticker: str,
    probability_ppm: int,
    *,
    created_sequence: int | None,
    live: bool = False,
) -> FixtureForecast:
    """Build one traded, eligible `FixtureForecast` varying `live`/provenance.

    Args:
        forecast_id: Stable forecast identifier.
        market_ticker: The market this forecast is about.
        probability_ppm: Forecast probability, in ppm.
        created_sequence: The forecast's creation sequence, or `None`.
        live: Whether this forecast is on the LIVE (vs PAPER) track.

    Returns:
        The constructed `FixtureForecast`.
    """
    return FixtureForecast(
        forecast_id=forecast_id,
        market_ticker=market_ticker,
        probability_ppm=ProbabilityPpm(probability_ppm),
        eligible_for_live=True,
        abstention_reason=None,
        traded=True,
        baseline_executable_price_pips=probability_ppm // 100,
        created_sequence=created_sequence,
        live=live,
    )


def _execution_record(fill_id: str, *, sequence: int):
    """Build one minimal `ExecutionQualityRecord` for a threading test.

    Args:
        fill_id: Stable fill identifier.
        sequence: The record's creation sequence.

    Returns:
        The constructed record.
    """
    from windbreak.evaluation.execution_quality import ExecutionQualityRecord

    return ExecutionQualityRecord(
        fill_id=fill_id,
        market_ticker="MKT-EXEC",
        side="YES",
        filled_centis=100,
        actual_cost_micros=110_000,
        modeled_cost_micros=100_000,
        model_version="pfm-registry-test",
        created_sequence=sequence,
    )


# ---------------------------------------------------------------------------
# 1. FixtureForecast.live: defaulted, non-breaking, accepts True.
# ---------------------------------------------------------------------------


def test_fixture_forecast_live_defaults_to_false() -> None:
    """Every pre-#58 `FixtureForecast(...)` call (no `live=` kwarg) still
    constructs, defaulting `live` to `False`.
    """
    forecast = FixtureForecast(
        forecast_id="fc-1",
        market_ticker="MKT-A",
        probability_ppm=ProbabilityPpm(500_000),
        eligible_for_live=True,
        abstention_reason=None,
        traded=True,
        baseline_executable_price_pips=5_000,
        created_sequence=1,
    )

    assert forecast.live is False


def test_fixture_forecast_live_accepts_explicit_true() -> None:
    """`FixtureForecast(..., live=True)` marks a forecast as LIVE-track."""
    forecast = _forecast("fc-1", "MKT-A", 500_000, created_sequence=1, live=True)

    assert forecast.live is True


# ---------------------------------------------------------------------------
# 2. EvaluationInputs.execution_records: defaulted, threads through the gates.
# ---------------------------------------------------------------------------


def test_evaluation_inputs_execution_records_defaults_to_empty_tuple() -> None:
    """`EvaluationInputs(...)` with no `execution_records=` kwarg defaults to
    `()`, so every pre-#58 construction call keeps working unchanged.
    """
    inputs = EvaluationInputs(forecasts=(), resolutions={}, temporal=None)

    assert inputs.execution_records == ()


def test_gate_evaluation_inputs_preserves_execution_records() -> None:
    """`gate_evaluation_inputs` narrows `forecasts` for temporal integrity but
    carries `execution_records` through unchanged -- fills are not
    forecast-gated, and the gate must never silently drop them.
    """
    records = (
        _execution_record("F-1", sequence=1),
        _execution_record("F-2", sequence=2),
    )
    inputs = EvaluationInputs(
        forecasts=(_forecast("fc-1", "MKT-A", 500_000, created_sequence=10),),
        resolutions={"MKT-A": ResolutionOutcome.YES},
        temporal=TemporalContext(
            deployment_sequence=0, resolution_sequences={"MKT-A": 100}
        ),
        execution_records=records,
    )

    admitted, _rejections = gate_evaluation_inputs(inputs)

    assert admitted.execution_records == records


def test_windowed_preserves_execution_records() -> None:
    """The private per-window narrowing helper `registry._windowed` carries
    `execution_records` through unchanged too -- the second seam the
    architecture plan names explicitly.
    """
    records = (_execution_record("F-1", sequence=1),)
    inputs = EvaluationInputs(
        forecasts=(_forecast("fc-1", "MKT-A", 500_000, created_sequence=10),),
        resolutions={"MKT-A": ResolutionOutcome.YES},
        temporal=TemporalContext(
            deployment_sequence=0, resolution_sequences={"MKT-A": 100}
        ),
        execution_records=records,
    )

    windowed = registry._windowed(inputs, ObservationWindow.LATEST_BEFORE_CLOSE)

    assert windowed.execution_records == records


# ---------------------------------------------------------------------------
# 3. Registry growth: 9 -> 11 metrics, both new names gated.
# ---------------------------------------------------------------------------


def test_registered_metrics_grows_to_eleven_including_the_two_new_names() -> None:
    """`registered_metrics()` carries all nine pre-#58 metrics plus the two
    new live-divergence metrics, and nothing else.
    """
    metrics = registered_metrics()

    assert set(metrics) == _PRE_58_METRIC_NAMES | _NEW_METRIC_NAMES
    assert len(metrics) == 11


def test_live_metrics_are_gated_by_the_single_temporal_choke_point() -> None:
    """A pre-deployment (rejected) LIVE forecast never reaches either new
    metric's `compute` -- the same mandatory `gate_evaluation_inputs` choke
    point every existing metric is routed through
    (`MetricSpec.__post_init__`'s `gated_compute` wrapper), verified by
    comparing the metric's value over an ungated run that is *entirely*
    pre-deployment (and therefore rejects to nothing) against the same run
    passed through the gate explicitly: both must agree, because the gate is
    unconditional and idempotent.
    """
    pre_deployment_forecast = _forecast(
        "fc-early", "MKT-EARLY", 500_000, created_sequence=None, live=True
    )
    inputs = EvaluationInputs(
        forecasts=(pre_deployment_forecast,),
        resolutions={"MKT-EARLY": ResolutionOutcome.YES},
        temporal=TemporalContext(deployment_sequence=0, resolution_sequences={}),
        execution_records=(),
    )
    metrics = registered_metrics()

    for name in _NEW_METRIC_NAMES:
        ungated_result = metrics[name].compute(inputs)
        admitted, _rejections = gate_evaluation_inputs(inputs)
        gated_result = metrics[name].compute(admitted)
        assert ungated_result == gated_result, name
