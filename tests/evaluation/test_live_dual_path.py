"""Failing-first dual-path parity tests for issue #58's two new live gate
inputs (`live_slippage_ratio`, `live_brier_degradation`) -- SPEC T12, RED.

Neither `windbreak.evaluation.execution_quality` nor the two new
`FixtureForecast.live` / `EvaluationInputs.execution_records` fields nor the
`"live_slippage_ratio"` / `"live_brier_degradation"` registry entries nor the
`GatePlan.live_rolling_window_size` (and sibling) fields exist yet, so every
test below fails independently and for a distinct, legitimate reason:

- Most tests import `windbreak.evaluation.execution_quality` (used to build an
  `ExecutionQualityRecord`) as their FIRST statement, so they fail on
  `ModuleNotFoundError: No module named 'windbreak.evaluation.execution_quality'`.
- Every fixture-building helper below that passes `live=True`/`live=False` to
  `FixtureForecast(...)` or `execution_records=...` to `EvaluationInputs(...)`
  fails on `TypeError: __init__() got an unexpected keyword argument 'live'`
  (or `'execution_records'`) the moment it is actually called inside a test
  body -- mirroring `test_registry_live_fields.py`'s documented convention,
  since these are additions to already-existing dataclasses rather than a
  brand-new module.
- Once construction succeeds (post-implementation), `plan.live_rolling_window_size`
  fails on `AttributeError` until `GatePlan` grows the field (mirroring
  `test_preregistration_live_thresholds.py`), and `SqlGateComputer().compute(...)`
  degrades to the `SQL_QUERY_FAILED` sentinel for both new metric names until
  `DEFAULT_GATE_QUERIES` grows the two matching entries -- so even a test that
  gets past every constructor still fails, on assertion, until the real
  arithmetic lands on both paths.

This file's job is exactly the property T12 requires: proving the SQL
reproduction and the Python reference agree EXACTLY (never merely within the
crosscheck's usual `+/-1` `INTEGER_ROUNDING_TOLERANCE`) on these two new
metrics, because the architecture plan requires every final division for them
to happen in a shared-rounding-direction Python UDF on the SQL side. Every
`==` assertion below is therefore a bare equality, never an `abs(...) <= 1`
check.

ASSUMPTIONS this file pins (the architecture plan's prose does not give a
literal signature for either the SQL-side binding or the Python-side rolling
window, so these are this suite's best-effort, reconcile-don't-silently-match
design points):

- `SqlGateComputer.compute(inputs, plan)` already accepts a `plan` argument
  (existing signature, issue #55) and is expected to grow two new
  `DEFAULT_GATE_QUERIES` entries -- `"live_slippage_ratio"` and
  `"live_brier_degradation"` -- whose queries bind `plan.live_rolling_window_size`
  via a `sqlite3` `?` parameter (e.g. `... ORDER BY created_sequence DESC LIMIT ?`),
  backed by new `hk_ratio` / `hk_degradation` UDFs mirroring the existing
  `hk_skill` / `hk_delta` pattern, over a new `execution_records` table and new
  `forecasts.live` / `forecasts.created_sequence` projected columns.
- The Python reference path's `registered_metrics()["live_slippage_ratio"]` /
  `["live_brier_degradation"]` `MetricSpec`s delegate to
  `windbreak.evaluation.execution_quality.live_slippage_ratio` and a new
  `windbreak.evaluation.cohorts.live_brier_degradation` (mirroring
  `traded_vs_skipped_brier_delta`'s `SKIPPED`/`TRADED` partition, but on
  `forecast.live` instead of `forecast.traded`) and apply the SAME
  `live_rolling_window_size` (default `100`) truncation internally, keyed off
  `created_sequence` descending, so a `MetricSpec.compute(inputs)` call given
  MORE than 100 admitted LIVE forecasts already returns the windowed answer
  without the caller doing any pre-truncation.
- Test 4's fixture keeps its PAPER baseline cohort at 2 records (far under the
  100-record window) specifically so the test's pinned answer is agnostic to
  whether the rolling window is scoped to the LIVE cohort alone or to the
  whole admitted forecast set: either reading truncates the PAPER cohort to a
  no-op, so this test does not have to pick a side on that particular
  ambiguity to be a valid, unambiguous RED pin.

PR #199 review fix (Gate 4 RED): `crosscheck_gates` currently scores
`live_slippage_ratio` on BOTH paths with no `require_model_version` guard at
all (unlike `monitor_live_divergence`, which already calls it), so a recorded
execution-quality fill whose `model_version` disagrees with
`plan.paper_fill_model_version` is silently scored rather than failing closed.
The tests in section 5 below pin the fix: `crosscheck_gates` must raise
`ValueError` before scoring or ledgering anything on a model-version mismatch,
and must behave exactly as before (no raise) when every record's version
matches the plan.

Symbols from already-existing modules (`windbreak.evaluation.registry`,
`windbreak.evaluation.resolution`, `windbreak.evaluation.temporal`,
`windbreak.evaluation.cohorts`, `windbreak.evaluation.crosscheck`,
`windbreak.evaluation.sql_gates`, `windbreak.evaluation.preregistration`,
`windbreak.config.schema`, `windbreak.numeric.types`, `windbreak.ledger.store`,
`windbreak.alerts.registry`) are imported at module scope; symbols from
`windbreak.evaluation.execution_quality` (which does not exist yet) are
imported as the first statement inside whichever helper or test body needs
them, mirroring `test_live_divergence.py`'s `_execution_record` convention.
`_main_admitted_inputs` and `_paper_only_inputs` are reused directly from
`tests/evaluation/test_dual_path.py` / `tests/evaluation/test_live_divergence.py`
(DRY, mirroring `test_live_divergence_kernel_wiring.py`'s own reuse of the
latter).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from tests.evaluation.test_dual_path import _main_admitted_inputs
from tests.evaluation.test_live_divergence import _paper_only_inputs
from windbreak.config.schema import EvaluationConfig
from windbreak.evaluation import cohorts
from windbreak.evaluation.preregistration import build_gate_plan
from windbreak.evaluation.registry import (
    EvaluationInputs,
    FixtureForecast,
    gate_evaluation_inputs,
    registered_metrics,
)
from windbreak.evaluation.resolution import ResolutionOutcome
from windbreak.evaluation.temporal import TemporalContext
from windbreak.ledger.store import SqliteLedgerStore
from windbreak.numeric.types import ProbabilityPpm

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from windbreak.alerts.registry import AlertSeverity
    from windbreak.evaluation.preregistration import GatePlan

    _AlertHook = Callable[[AlertSeverity, str], None]

#: Fixed paper fill-model version shared by every plan this suite builds.
_PFM_VERSION = "pfm-live-dual-path-test"


class _DeterministicUtcClock:
    """A minimal deterministic UTC clock, mirroring `test_dual_path.py`'s twin."""

    def __init__(self) -> None:
        """Initialize the clock at a fixed 2024-01-01T00:00:00+00:00 UTC."""
        self._current = datetime(2024, 1, 1, tzinfo=UTC)
        self._calls = 0

    def __call__(self) -> datetime:
        """Return the next deterministic UTC datetime.

        Returns:
            The fixed start time on the first call, then a value advanced by
            one second on every subsequent call.
        """
        if self._calls > 0:
            self._current = self._current + timedelta(seconds=1)
        self._calls += 1
        return self._current


def _ledger_store(directory: Path) -> SqliteLedgerStore:
    """Build a directory-backed `SqliteLedgerStore` with a deterministic clock.

    Args:
        directory: The directory to root the database file in.

    Returns:
        A fresh `SqliteLedgerStore`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    return SqliteLedgerStore(directory / "ledger.db", now=_DeterministicUtcClock())


def _recording_alert_hook() -> tuple[list[tuple[AlertSeverity, str]], _AlertHook]:
    """Build an alert hook that records every call, mirroring `test_dual_path.py`.

    Returns:
        A `(calls, hook)` pair.
    """
    calls: list[tuple[AlertSeverity, str]] = []

    def _hook(severity: AlertSeverity, message: str) -> None:
        """Record one alert call."""
        calls.append((severity, message))

    return calls, _hook


def _built_plan() -> GatePlan:
    """Build the shared `GatePlan` this suite's dual-path calls are scored under.

    Returns:
        The plan produced by `build_gate_plan` off a stock `EvaluationConfig`,
        carrying the confirmed live-threshold defaults
        (`live_rolling_window_size=100`, `live_slippage_ratio_limit_ppm=1_500_000`,
        `live_brier_degradation_band_ppm=50_000`).
    """
    return build_gate_plan(EvaluationConfig(), paper_fill_model_version=_PFM_VERSION)


def _live_forecast(
    forecast_id: str, market_ticker: str, probability_ppm: int, *, created_sequence: int
) -> FixtureForecast:
    """Build one resolved, traded LIVE-track forecast (`live=True`).

    Args:
        forecast_id: Stable forecast identifier.
        market_ticker: The market this forecast is about.
        probability_ppm: Forecast probability, in ppm.
        created_sequence: The forecast's creation sequence.

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
        live=True,
    )


def _paper_forecast(
    forecast_id: str, market_ticker: str, probability_ppm: int, *, created_sequence: int
) -> FixtureForecast:
    """Build one resolved, traded PAPER-track forecast (`live=False`).

    Args:
        forecast_id: Stable forecast identifier.
        market_ticker: The market this forecast is about.
        probability_ppm: Forecast probability, in ppm.
        created_sequence: The forecast's creation sequence.

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
        live=False,
    )


def _execution_record(
    fill_id: str, *, actual_cost_micros: int, modeled_cost_micros: int, sequence: int
):
    """Build one `ExecutionQualityRecord` for this suite's slippage fixtures.

    Args:
        fill_id: Stable fill identifier.
        actual_cost_micros: The recorded actual cost, in micros.
        modeled_cost_micros: The recorded paper-model cost, in micros.
        sequence: The record's creation sequence.

    Returns:
        The constructed `ExecutionQualityRecord`.
    """
    from windbreak.evaluation.execution_quality import ExecutionQualityRecord

    return ExecutionQualityRecord(
        fill_id=fill_id,
        market_ticker="MKT-DP-EXEC",
        side="YES",
        filled_centis=100,
        actual_cost_micros=actual_cost_micros,
        modeled_cost_micros=modeled_cost_micros,
        model_version=_PFM_VERSION,
        created_sequence=sequence,
    )


def _known_answer_inputs() -> EvaluationInputs:
    """Build the shared known-answer fixture: 2 LIVE + 2 PAPER forecasts, 2 fills.

    LIVE cohort (both resolved): `fc-l1` (p=500_000, outcome YES, term
    `(500_000-1_000_000)^2 = 250_000_000_000`), `fc-l2` (p=500_000, outcome NO,
    term `(500_000-0)^2 = 250_000_000_000`); sum = `500_000_000_000`, mean =
    `ceil(500_000_000_000 / (2 * 1_000_000)) = 250_000` ppm exactly (no
    remainder).

    PAPER cohort (both resolved): `fc-p1` (p=800_000, outcome YES, term
    `(800_000-1_000_000)^2 = 40_000_000_000`), `fc-p2` (p=600_000, outcome NO,
    term `(600_000-0)^2 = 360_000_000_000`); sum = `400_000_000_000`, mean =
    `ceil(400_000_000_000 / (2 * 1_000_000)) = 200_000` ppm exactly.

    `live_brier_degradation = LIVE_mean - PAPER_mean = 250_000 - 200_000 =
    50_000` ppm exactly.

    Execution-quality records: `sum(actual) = 3_000_000 + 1_000_000 =
    4_000_000`, `sum(modeled) = 1_500_000 + 500_000 = 2_000_000`;
    `live_slippage_ratio = ceil(4_000_000 * 1_000_000 / 2_000_000) =
    2_000_000` ppm exactly (no remainder).

    Returns:
        The raw (temporally-admittable) `EvaluationInputs`.
    """
    live_forecasts = (
        _live_forecast("fc-l1", "MKT-DP-L1", 500_000, created_sequence=10),
        _live_forecast("fc-l2", "MKT-DP-L2", 500_000, created_sequence=11),
    )
    paper_forecasts = (
        _paper_forecast("fc-p1", "MKT-DP-P1", 800_000, created_sequence=20),
        _paper_forecast("fc-p2", "MKT-DP-P2", 600_000, created_sequence=21),
    )
    forecasts = live_forecasts + paper_forecasts
    resolutions = {
        "MKT-DP-L1": ResolutionOutcome.YES,
        "MKT-DP-L2": ResolutionOutcome.NO,
        "MKT-DP-P1": ResolutionOutcome.YES,
        "MKT-DP-P2": ResolutionOutcome.NO,
    }
    temporal = TemporalContext(
        deployment_sequence=0, resolution_sequences=dict.fromkeys(resolutions, 100)
    )
    execution_records = (
        _execution_record(
            "F-dp-1",
            actual_cost_micros=3_000_000,
            modeled_cost_micros=1_500_000,
            sequence=1,
        ),
        _execution_record(
            "F-dp-2",
            actual_cost_micros=1_000_000,
            modeled_cost_micros=500_000,
            sequence=2,
        ),
    )
    return EvaluationInputs(
        forecasts=forecasts,
        resolutions=resolutions,
        temporal=temporal,
        execution_records=execution_records,
    )


def _grown_catalogue_inputs() -> EvaluationInputs:
    """Extend `test_dual_path.py`'s 6-forecast fixture with 2 LIVE forecasts and
    2 execution-quality records, so every one of the (now 11) registered
    metrics has well-defined, non-trivial data to score.

    Deliberately does NOT re-pin the pre-existing nine metrics' exact values
    (adding data changes them); this fixture exists solely to prove the full,
    grown catalogue still agrees end-to-end via `crosscheck_gates`, not to
    re-verify arithmetic `test_dual_path.py` already pins.

    Returns:
        The raw (temporally-admittable) `EvaluationInputs`.
    """
    base = _main_admitted_inputs()
    live_additions = (
        _live_forecast("fc-live-a", "MKT-LIVE-A", 400_000, created_sequence=16),
        _live_forecast("fc-live-b", "MKT-LIVE-B", 600_000, created_sequence=17),
    )
    forecasts = base.forecasts + live_additions
    resolutions = {
        **base.resolutions,
        "MKT-LIVE-A": ResolutionOutcome.NO,
        "MKT-LIVE-B": ResolutionOutcome.YES,
    }
    temporal = TemporalContext(
        deployment_sequence=base.temporal.deployment_sequence,
        resolution_sequences={
            **base.temporal.resolution_sequences,
            "MKT-LIVE-A": 100,
            "MKT-LIVE-B": 100,
        },
    )
    execution_records = (
        _execution_record(
            "F-grown-1",
            actual_cost_micros=2_200_000,
            modeled_cost_micros=2_000_000,
            sequence=1,
        ),
        _execution_record(
            "F-grown-2",
            actual_cost_micros=1_100_000,
            modeled_cost_micros=1_000_000,
            sequence=2,
        ),
    )
    return EvaluationInputs(
        forecasts=forecasts,
        resolutions=resolutions,
        temporal=temporal,
        execution_records=execution_records,
    )


# ---------------------------------------------------------------------------
# 1. Per-metric exact parity on known-answer data.
# ---------------------------------------------------------------------------


def test_live_slippage_ratio_exact_parity_on_known_answer_data() -> None:
    """`live_slippage_ratio` agrees EXACTLY (never `+/-1`) between the SQL and
    Python paths, and both equal the hand-derived `2_000_000` ppm.
    """
    from windbreak.evaluation.sql_gates import SqlGateComputer

    inputs = _known_answer_inputs()
    plan = _built_plan()
    admitted, _rejections = gate_evaluation_inputs(inputs)
    specs = registered_metrics()

    python_value = specs["live_slippage_ratio"].compute(admitted)
    sql_values = SqlGateComputer().compute(admitted, plan)

    assert python_value == 2_000_000
    assert sql_values["live_slippage_ratio"] == 2_000_000
    assert python_value == sql_values["live_slippage_ratio"]


def test_live_brier_degradation_exact_parity_on_known_answer_data() -> None:
    """`live_brier_degradation` agrees EXACTLY between the SQL and Python
    paths, and both equal the hand-derived `50_000` ppm.
    """
    from windbreak.evaluation.sql_gates import SqlGateComputer

    inputs = _known_answer_inputs()
    plan = _built_plan()
    admitted, _rejections = gate_evaluation_inputs(inputs)
    specs = registered_metrics()

    python_value = specs["live_brier_degradation"].compute(admitted)
    sql_values = SqlGateComputer().compute(admitted, plan)

    assert python_value == 50_000
    assert sql_values["live_brier_degradation"] == 50_000
    assert python_value == sql_values["live_brier_degradation"]


# ---------------------------------------------------------------------------
# 2. Crosscheck MATCH over the grown, 11-metric catalogue.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_matches_over_the_grown_eleven_metric_catalogue(
    tmp_path: Path,
) -> None:
    """`crosscheck_gates` reports `MATCH` over all 11 registered metrics
    (the original 9 plus `live_slippage_ratio` / `live_brier_degradation`) on
    a fixture carrying LIVE forecasts and execution-quality records: appends
    nothing, alerts nothing.
    """
    from windbreak.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    inputs = _grown_catalogue_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        assert result.status is CrosscheckStatus.MATCH
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        assert set(by_name) == set(registered_metrics())
        assert len(by_name) == 11
        assert "live_slippage_ratio" in by_name
        assert "live_brier_degradation" in by_name

        for comparison in result.comparisons:
            assert comparison.python_value == comparison.sql_value, comparison.name
            assert comparison.within_tolerance is True

        assert store.read_all() == []
        assert calls == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. UNDEFINED-sentinel parity: empty LIVE cohort, empty execution records.
# ---------------------------------------------------------------------------


def test_live_dual_path_undefined_sentinel_parity_on_paper_only_inputs(
    tmp_path: Path,
) -> None:
    """A PAPER-only run (no LIVE forecast at all, no execution record at all)
    yields `cohorts.UNDEFINED` on BOTH paths for BOTH new metrics -- never an
    exception, never a real `int` on one side only -- and `crosscheck_gates`
    treats the two identical sentinels as agreement (`MATCH`).
    """
    from windbreak.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates
    from windbreak.evaluation.sql_gates import SqlGateComputer

    inputs = _paper_only_inputs()
    plan = _built_plan()
    admitted, _rejections = gate_evaluation_inputs(inputs)
    specs = registered_metrics()

    python_ratio = specs["live_slippage_ratio"].compute(admitted)
    python_degradation = specs["live_brier_degradation"].compute(admitted)
    sql_values = SqlGateComputer().compute(admitted, plan)

    assert python_ratio is cohorts.UNDEFINED
    assert python_degradation is cohorts.UNDEFINED
    assert sql_values["live_slippage_ratio"] is cohorts.UNDEFINED
    assert sql_values["live_brier_degradation"] is cohorts.UNDEFINED

    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        by_name = {comparison.name: comparison for comparison in result.comparisons}
        assert by_name["live_slippage_ratio"].within_tolerance is True
        assert by_name["live_brier_degradation"].within_tolerance is True
        assert result.status is CrosscheckStatus.MATCH
        assert store.read_all() == []
        assert calls == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 4. Rolling-window parity: > live_rolling_window_size (100) LIVE forecasts.
# ---------------------------------------------------------------------------


def test_live_brier_degradation_rolling_window_parity_over_105_live_forecasts() -> None:
    """Only the most-recent `live_rolling_window_size` (100) LIVE forecasts
    (by `created_sequence` desc) feed `live_brier_degradation` on BOTH paths,
    and both agree EXACTLY on the windowed answer -- proving the SQL side's
    `LIMIT ?` binds `plan.live_rolling_window_size` exactly like the Python
    side's internal `[:N]` truncation.

    105 LIVE forecasts, built in Python (impractical to hand-author as a
    105-entry fixture, the same rationale `test_dual_path.py`'s module
    docstring documents for the OLS sums), two constant per-forecast
    probability/outcome pairs so both the windowed and full-set sums are exact
    hand arithmetic:

    - 5 "old" LIVE forecasts (`created_sequence` 1-5): `p=200_000`, outcome
      YES; term `(200_000-1_000_000)^2 = 640_000_000_000` each.
    - 100 "recent" LIVE forecasts (`created_sequence` 6-105): `p=500_000`,
      outcome YES; term `(500_000-1_000_000)^2 = 250_000_000_000` each.

    Windowed (last 100 by `created_sequence` desc -- the 100 "recent"
    forecasts only): `sum = 100 * 250_000_000_000 = 25_000_000_000_000`;
    `mean = ceil(25_000_000_000_000 / (100 * 1_000_000)) = 250_000` ppm
    exactly (no remainder).

    A fixed, 2-forecast PAPER baseline (`p=300_000` each, one YES one NO
    outcome) stays far under the 100-record window, so its mean is unaffected
    by whichever cohort(s) the truncation applies to: `fc-bp1` (outcome NO,
    term `300_000^2 = 90_000_000_000`), `fc-bp2` (outcome YES, term
    `(300_000-1_000_000)^2 = 490_000_000_000`); sum = `580_000_000_000`;
    mean = `ceil(580_000_000_000 / (2 * 1_000_000)) = 290_000` ppm exactly.

    Windowed `live_brier_degradation = 250_000 - 290_000 = -40_000` ppm
    exactly -- the pinned answer both the manually-windowed-100 fixture AND
    the full, untruncated 105-forecast fixture must agree on (the latter only
    if both paths correctly truncate internally; without truncation the naive
    full-105 mean would instead be `ceil(28_200_000_000_000 / 105_000_000) =
    268_572`, giving a materially different, WRONG degradation of `-21_428`).
    """
    from windbreak.evaluation.sql_gates import SqlGateComputer

    old_live = tuple(
        _live_forecast(
            f"fc-lo-{sequence}",
            f"MKT-LO-{sequence}",
            200_000,
            created_sequence=sequence,
        )
        for sequence in range(1, 6)
    )
    recent_live = tuple(
        _live_forecast(
            f"fc-lr-{sequence}",
            f"MKT-LR-{sequence}",
            500_000,
            created_sequence=sequence,
        )
        for sequence in range(6, 106)
    )
    all_live = old_live + recent_live
    assert len(all_live) == 105

    windowed_live = tuple(
        sorted(all_live, key=lambda forecast: forecast.created_sequence, reverse=True)
    )[:100]
    assert len(windowed_live) == 100
    assert all(
        forecast.market_ticker.startswith("MKT-LR-") for forecast in windowed_live
    )

    paper_baseline = (
        _paper_forecast("fc-bp1", "MKT-BP-1", 300_000, created_sequence=200),
        _paper_forecast("fc-bp2", "MKT-BP-2", 300_000, created_sequence=201),
    )
    baseline_resolutions = {
        "MKT-BP-1": ResolutionOutcome.NO,
        "MKT-BP-2": ResolutionOutcome.YES,
    }

    def _inputs_for(live_forecasts: tuple[FixtureForecast, ...]) -> EvaluationInputs:
        """Build inputs combining `live_forecasts` with the fixed PAPER baseline.

        Args:
            live_forecasts: The LIVE-track forecasts to include (either the
                windowed 100 or the full untruncated 105).

        Returns:
            The raw (temporally-admittable) `EvaluationInputs`.
        """
        forecasts = live_forecasts + paper_baseline
        resolutions = {
            **baseline_resolutions,
            **dict.fromkeys(
                (forecast.market_ticker for forecast in live_forecasts),
                ResolutionOutcome.YES,
            ),
        }
        temporal = TemporalContext(
            deployment_sequence=0,
            resolution_sequences=dict.fromkeys(resolutions, 1_000),
        )
        return EvaluationInputs(
            forecasts=forecasts,
            resolutions=resolutions,
            temporal=temporal,
            execution_records=(),
        )

    windowed_inputs = _inputs_for(windowed_live)
    full_inputs = _inputs_for(all_live)

    plan = _built_plan()
    assert plan.live_rolling_window_size == 100
    specs = registered_metrics()

    windowed_admitted, _ = gate_evaluation_inputs(windowed_inputs)
    full_admitted, _ = gate_evaluation_inputs(full_inputs)

    python_windowed = specs["live_brier_degradation"].compute(windowed_admitted)
    python_full = specs["live_brier_degradation"].compute(full_admitted)
    sql_windowed = SqlGateComputer().compute(windowed_admitted, plan)[
        "live_brier_degradation"
    ]
    sql_full = SqlGateComputer().compute(full_admitted, plan)["live_brier_degradation"]

    assert python_windowed == -40_000
    assert sql_windowed == -40_000
    assert python_full == -40_000
    assert sql_full == -40_000
    assert python_full == python_windowed == sql_full == sql_windowed


# ---------------------------------------------------------------------------
# 5. Fail-closed parity: `crosscheck_gates` must reject a model-version
#    mismatch exactly like `monitor_live_divergence` already does (PR #199
#    review fix).
# ---------------------------------------------------------------------------


def test_crosscheck_gates_model_version_mismatch_fails_closed(tmp_path: Path) -> None:
    """A recorded execution-quality fill whose `model_version` disagrees with
    `plan.paper_fill_model_version` must fail closed: `crosscheck_gates` should
    raise `ValueError` before scoring or ledgering anything, mirroring
    `monitor_live_divergence`'s existing `require_model_version` guard.

    Today `crosscheck_gates` has no such guard at all -- it scores
    `live_slippage_ratio` on both paths regardless of `model_version` -- so
    this currently returns an ordinary `CrosscheckResult` instead of raising,
    and this test fails with "DID NOT RAISE".
    """
    from windbreak.evaluation.crosscheck import crosscheck_gates
    from windbreak.evaluation.execution_quality import ExecutionQualityRecord

    mismatched_record = ExecutionQualityRecord(
        fill_id="F-model-mismatch",
        market_ticker="MKT-DP-EXEC",
        side="YES",
        filled_centis=100,
        actual_cost_micros=1_000_000,
        modeled_cost_micros=900_000,
        model_version="pfm-a-completely-different-version",
        created_sequence=1,
    )
    base = _main_admitted_inputs()
    inputs = EvaluationInputs(
        forecasts=base.forecasts,
        resolutions=base.resolutions,
        temporal=base.temporal,
        execution_records=(mismatched_record,),
    )
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    _calls, hook = _recording_alert_hook()
    try:
        with pytest.raises(ValueError, match="model_version"):
            crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        # Fail-closed means before anything is ledgered, not merely reported.
        assert store.read_all() == []
    finally:
        store.close()


def test_crosscheck_gates_model_version_match_behaves_normally(tmp_path: Path) -> None:
    """When every execution record's `model_version` matches the plan's
    `paper_fill_model_version`, `crosscheck_gates` behaves exactly as it does
    today: no raise, an ordinary `CrosscheckResult`. Guards against the
    model-version fail-closed fix over-triggering on the well-formed case.
    """
    from windbreak.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    inputs = _known_answer_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        assert result.status is CrosscheckStatus.MATCH
        assert calls == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 6. Zero-modeled-cost-sum parity: the SQL path must fail closed too, not
#    silently degrade to a sentinel (PR #199 review fix, Fix 3's SQL twin).
# ---------------------------------------------------------------------------


def test_live_slippage_ratio_sql_path_fails_closed_on_zero_modeled_sum() -> None:
    """The SQL path fails closed on the same degenerate zero-modeled-cost sum
    input Fix 3 pins on the Python side -- but by a DIFFERENT, architecturally
    uniform mechanism, which this test reconciles rather than fights.

    The two paths fail closed differently, and both are correct:

    - Python (`live_slippage_ratio`) raises a documented `ValueError` on a
      non-empty record set whose modeled-cost sum is `0`.
    - SQL (`SqlGateComputer().compute(...)`) does NOT raise: `_hk_ratio`'s
      `ZeroDivisionError` surfaces to sqlite3 as a `sqlite3.Error`, which
      `_value_for` uniformly catches and degrades to the fail-loud
      `SQL_QUERY_FAILED` sentinel (`sql_gates.py`) -- the same convention
      every UDF failure follows. This is the SQL architecture's single,
      deliberate failure path; making `_hk_ratio` bypass the sentinel to raise
      would be fighting that architecture, not fixing a bug.

    Both are fail-closed: neither ever returns a silently-wrong number. So
    this test asserts the SQL path degrades to `SQL_QUERY_FAILED` (its
    fail-loud sentinel) while the Python path raises `ValueError` -- a
    correctness reconciliation of this test's original (mistaken) assumption
    that both paths would raise the identical exception, NOT a weakening.
    """
    from windbreak.evaluation.execution_quality import ExecutionQualityRecord
    from windbreak.evaluation.sql_gates import SQL_QUERY_FAILED, SqlGateComputer

    zero_modeled_record = ExecutionQualityRecord(
        fill_id="F-sql-zero-model",
        market_ticker="MKT-SQL-ZERO",
        side="YES",
        filled_centis=100,
        actual_cost_micros=1_000_000,
        modeled_cost_micros=0,
        model_version=_PFM_VERSION,
        created_sequence=1,
    )
    inputs = EvaluationInputs(
        forecasts=(),
        resolutions={},
        temporal=TemporalContext(deployment_sequence=0, resolution_sequences={}),
        execution_records=(zero_modeled_record,),
    )
    plan = _built_plan()
    admitted, _rejections = gate_evaluation_inputs(inputs)

    sql_values = SqlGateComputer().compute(admitted, plan)

    assert sql_values["live_slippage_ratio"] is SQL_QUERY_FAILED


# ---------------------------------------------------------------------------
# 7. Per-market collapse: a market re-forecast twice on the LIVE track must
#    score once (its LATEST_BEFORE_CLOSE record), not once per raw forecast
#    (Gate 4 round-2 review fix, Fix 1: `live_brier_degradation` must collapse
#    per-market before scoring, on BOTH the Python and SQL paths).
# ---------------------------------------------------------------------------


def test_live_brier_degradation_collapses_re_forecast_market_before_scoring() -> None:
    """A LIVE market forecast TWICE contributes ONE observation -- its
    `LATEST_BEFORE_CLOSE` (max `created_sequence`) record -- to
    `live_brier_degradation`, on BOTH the Python and SQL paths, agreeing
    EXACTLY (never `+/-1`) on the hand-derived, per-market-collapsed answer.

    Hand computation, per-market collapse applied (the CORRECT answer):

    LIVE cohort, 2 markets after collapse:
    - `MKT-COLLAPSE-RF` is forecast TWICE: `fc-rf-1` (seq=1, p=200_000) and
      `fc-rf-2` (seq=2, p=900_000). `LATEST_BEFORE_CLOSE` keeps only the
      max-`created_sequence` record, `fc-rf-2`. Outcome YES (1_000_000):
      term = (900_000-1_000_000)^2 = 10_000_000_000.
    - `MKT-COLLAPSE-L2` (`fc-l2`, seq=3, p=500_000), outcome NO (0):
      term = (500_000-0)^2 = 250_000_000_000.
    Collapsed LIVE sum = 260_000_000_000, n=2 (one row per market); mean =
    `ceil(260_000_000_000 / (2 * 1_000_000)) = 130_000` ppm exactly (no
    remainder).

    PAPER cohort, 1 market: `MKT-COLLAPSE-P1` (`fc-p1`, seq=4, p=700_000),
    outcome YES: term = (700_000-1_000_000)^2 = 90_000_000_000; n=1; mean =
    `ceil(90_000_000_000 / 1_000_000) = 90_000` ppm exactly.

    `live_brier_degradation` (collapsed, correct) = 130_000 - 90_000 =
    `40_000` ppm exactly -- the value this test pins on BOTH paths.

    Today (RED), neither path applies the per-market collapse before
    scoring, so `fc-rf-1` is counted as a THIRD, separate LIVE observation
    instead of being superseded by `fc-rf-2`: sum = 640_000_000_000
    (fc-rf-1, uncollapsed term) + 10_000_000_000 (fc-rf-2) +
    250_000_000_000 (fc-l2) = 900_000_000_000; n=3; mean =
    `ceil(900_000_000_000 / (3 * 1_000_000)) = 300_000` ppm; degradation =
    300_000 - 90_000 = 210_000 -- WRONG, and != the pinned `40_000` below.
    This fails today on the Python assertion
    (`cohorts.live_brier_degradation` feeds `_resolved_track_forecasts`'s
    raw, uncollapsed per-track forecasts straight into `_rolling_window` and
    `mean_brier`, violating `mean_brier`'s documented precondition that the
    caller already applied `windows.resolve_window`) and on the SQL
    assertion (`sql_gates.py`'s `"live_brier_degradation"` query selects
    straight from the raw `forecasts` rows with no per-market
    `MAX(created_sequence)` collapse either).
    """
    from windbreak.evaluation.sql_gates import SqlGateComputer

    live_forecasts = (
        _live_forecast("fc-rf-1", "MKT-COLLAPSE-RF", 200_000, created_sequence=1),
        _live_forecast("fc-rf-2", "MKT-COLLAPSE-RF", 900_000, created_sequence=2),
        _live_forecast("fc-l2", "MKT-COLLAPSE-L2", 500_000, created_sequence=3),
    )
    paper_forecasts = (
        _paper_forecast("fc-p1", "MKT-COLLAPSE-P1", 700_000, created_sequence=4),
    )
    forecasts = live_forecasts + paper_forecasts
    resolutions = {
        "MKT-COLLAPSE-RF": ResolutionOutcome.YES,
        "MKT-COLLAPSE-L2": ResolutionOutcome.NO,
        "MKT-COLLAPSE-P1": ResolutionOutcome.YES,
    }
    temporal = TemporalContext(
        deployment_sequence=0, resolution_sequences=dict.fromkeys(resolutions, 100)
    )
    inputs = EvaluationInputs(
        forecasts=forecasts,
        resolutions=resolutions,
        temporal=temporal,
        execution_records=(),
    )
    plan = _built_plan()
    admitted, _rejections = gate_evaluation_inputs(inputs)
    specs = registered_metrics()

    python_value = specs["live_brier_degradation"].compute(admitted)
    sql_values = SqlGateComputer().compute(admitted, plan)

    assert python_value == 40_000
    assert sql_values["live_brier_degradation"] == 40_000
    assert python_value == sql_values["live_brier_degradation"]
