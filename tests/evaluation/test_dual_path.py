"""Failing-first tests for `hedgekit.evaluation.crosscheck` (issue #55, RED).

`hedgekit.evaluation.crosscheck` and `hedgekit.evaluation.sql_gates` do not
exist yet, so every test below imports their new symbols from those modules as
the FIRST statement inside the test body (matching this package's established
RED convention in `test_preregistration.py` / `test_cohorts.py`) so each test
collects and fails independently on its own
`ModuleNotFoundError: No module named 'hedgekit.evaluation.crosscheck'` (or
`...sql_gates`) rather than one collection-time explosion. Symbols from
already-existing modules (`hedgekit.config.schema`, `hedgekit.evaluation.registry`,
`hedgekit.evaluation.resolution`, `hedgekit.evaluation.temporal`,
`hedgekit.evaluation.cohorts`, `hedgekit.alerts.registry`, `hedgekit.ledger.store`,
`hedgekit.numeric.types`) are imported at module scope.

Pins issue #55's dual-path SQL/Python gate crosscheck:

- `create_gate_database(inputs)` projects the temporally-admitted inputs into
  an in-memory SQLite `forecasts`/`resolutions` pair; `DEFAULT_GATE_QUERIES` is
  the static per-metric SQL catalogue; `SqlGateComputer.compute(inputs, plan)`
  reproduces every `plan.metric_windows` metric independently in SQL.
- `crosscheck_gates(inputs, *, plan, store, alert, sql_path=None, tolerance=1,
  component="evaluation")` runs the Python reference path
  (`registered_metrics()`) and the SQL path on the same admitted inputs, and
  compares every metric: two ints agree within `tolerance` (inclusive), two
  identical sentinels agree, an int-vs-sentinel or a raised SQL query is
  always a mismatch. On any mismatch it appends exactly one
  `GateComputationMismatch` ledger event, fires one `AlertSeverity.CRITICAL`
  alert naming the disagreeing metrics, and returns `CrosscheckStatus.MISMATCH`;
  on full agreement it appends nothing, alerts nothing, and returns
  `CrosscheckStatus.MATCH`.

Shared known-answer fixture (`_main_admitted_inputs`): 6 forecasts, 5 resolved
(mixed YES/NO) plus 1 unresolved market, a 3-traded/2-skipped split. Every
resolved forecast is a singleton per market (no duplicate `market_ticker`), so
the `LATEST_BEFORE_CLOSE` window is an identity no-op and the arithmetic below
reduces to plain per-market Brier terms:

    ticker  p_ppm    outcome  baseline_ppm  traded   forecast_term  baseline_term
    MKT-1   700_000  YES      600_000       True     (300_000)^2    (400_000)^2
    MKT-2   300_000  NO       400_000       True     (300_000)^2    (400_000)^2
    MKT-3   800_000  YES      500_000       False    (200_000)^2    (500_000)^2
    MKT-4   200_000  NO       300_000       False    (200_000)^2    (300_000)^2
    MKT-5   900_000  YES      800_000       True     (100_000)^2    (200_000)^2
    MKT-6   ---      (unresolved; excluded from every metric on both paths)

- `brier` = sum(forecast_term) / (5 * 1_000_000) = 270_000_000_000 / 5_000_000
  = 54_000 ppm (exact, `OVERSTATE_COST`).
- `brier_skill_vs_executable_price`: baseline_sum = 700_000_000_000,
  forecast_sum = 270_000_000_000; skill =
  floor((700_000_000_000 - 270_000_000_000) * 1_000_000 / 700_000_000_000)
  = floor(430_000_000 / 700) = 614_285 ppm (`UNDERSTATE_EQUITY`).
- `traded_vs_skipped_brier_delta` = mean_brier(SKIPPED) - mean_brier(TRADED):
  SKIPPED (MKT-3, MKT-4) = 80_000_000_000 / 2_000_000 = 40_000 exact;
  TRADED (MKT-1, MKT-2, MKT-5) = ceil(190_000_000_000 / 3_000_000) =
  ceil(63_333.33) = 63_334; delta = 40_000 - 63_334 = -23_334 ppm.
- `fill_vs_model_slippage` = `NOT_IMPLEMENTED` (registry stub) on both paths.

The remaining five forecast-track metrics (`log_score`,
`expected_calibration_error`, `calibration_slope`, `calibration_intercept`,
`sharpness`) involve fixed-point base-2 logarithms and OLS sums that are
impractical to hand-transcribe without a code-execution channel (the same
constraint `test_preregistration.py`'s module docstring documents for a
SHA-256 digest); this suite does not pin their exact integers but does assert,
for every one of the nine registered metrics, that the two independently
computed paths agree -- which is exactly the property this crosscheck exists
to prove.

Resolved API detail this suite assumes and the implementer must honor: a
`GateComputationMismatch` payload carries the disagreeing metrics under a
`"mismatches"` key, each entry shaped
`{"name", "window", "python_value", "sql_value"}` with any sentinel rendered
by its `.name` (mirroring `hedgekit.evaluation.report._format_value`), plus
top-level `"plan_hash"` and `"tolerance"` keys.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from hedgekit.alerts.registry import AlertSeverity
from hedgekit.config.schema import EvaluationConfig
from hedgekit.evaluation import cohorts
from hedgekit.evaluation.preregistration import build_gate_plan
from hedgekit.evaluation.registry import EvaluationInputs, FixtureForecast
from hedgekit.evaluation.resolution import ResolutionOutcome
from hedgekit.evaluation.temporal import TemporalContext
from hedgekit.ledger.store import SqliteLedgerStore
from hedgekit.numeric.types import ProbabilityPpm

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from hedgekit.evaluation.preregistration import GatePlan
    from hedgekit.evaluation.sql_gates import SqlGateComputer

    _AlertHook = Callable[[AlertSeverity, str], None]

#: Fixed pfm version so `build_gate_plan` calls below share one plan identity.
_PFM_VERSION = "pfm-dual-path-test"


class _DeterministicUtcClock:
    """A minimal, self-contained deterministic UTC clock for a ledger store.

    Mirrors `test_preregistration.py`'s clock of the same name (fixed
    2024-01-01T00:00:00+00:00 UTC start, one-second steps) so every ledger
    row's `created_at` (and therefore its `event_hash`) is reproducible.
    """

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
        directory: The directory to root the database file in; created if
            absent so a test may pass a fresh subdirectory of `tmp_path`.

    Returns:
        A fresh `SqliteLedgerStore` whose `created_at` values are reproducible.
    """
    directory.mkdir(parents=True, exist_ok=True)
    return SqliteLedgerStore(directory / "ledger.db", now=_DeterministicUtcClock())


def _recording_alert_hook() -> tuple[list[tuple[AlertSeverity, str]], _AlertHook]:
    """Build an `AlertHook`-compatible callable that records every call.

    Returns:
        A `(calls, hook)` pair: `calls` accumulates `(severity, message)`
        tuples in call order, and `hook` is the callable to pass as
        `crosscheck_gates(..., alert=hook)`.
    """
    calls: list[tuple[AlertSeverity, str]] = []

    def _hook(severity: AlertSeverity, message: str) -> None:
        """Record one alert call.

        Args:
            severity: The alert's severity.
            message: The alert's human-readable message.
        """
        calls.append((severity, message))

    return calls, _hook


def _forecast(
    forecast_id: str,
    market_ticker: str,
    probability_ppm: int,
    baseline_pips: int,
    *,
    traded: bool,
    created_sequence: int,
    eligible: bool = True,
    abstention: str | None = None,
) -> FixtureForecast:
    """Build one `FixtureForecast` with the fields this suite's fixtures vary.

    Args:
        forecast_id: Stable identifier of the forecast record.
        market_ticker: Ticker of the market this forecast is about.
        probability_ppm: Forecast probability, in ppm.
        baseline_pips: The reference executable price, in pips.
        traded: Whether a live trade was actually taken.
        created_sequence: The forecast's creation sequence on the ledger.
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
        created_sequence=created_sequence,
    )


def _main_admitted_inputs() -> EvaluationInputs:
    """Build the shared 6-forecast fixture: 5 resolved, 1 unresolved.

    Every `created_sequence` (10-15) clears `deployment_sequence=0` and stays
    strictly before each resolved market's `resolution_sequences` entry (100),
    so MKT-1..MKT-5 are admitted by the temporal gate unchanged; MKT-6 has no
    resolution entry at all and is rejected `UNRESOLVED`.

    Returns:
        The raw (pre-gate) `EvaluationInputs`; `crosscheck_gates` is
        responsible for temporally admitting it before scoring.
    """
    forecasts = (
        _forecast("fc-1", "MKT-1", 700_000, 6_000, traded=True, created_sequence=10),
        _forecast("fc-2", "MKT-2", 300_000, 4_000, traded=True, created_sequence=11),
        _forecast(
            "fc-3",
            "MKT-3",
            800_000,
            5_000,
            traded=False,
            eligible=False,
            abstention="low_conviction",
            created_sequence=12,
        ),
        _forecast(
            "fc-4",
            "MKT-4",
            200_000,
            3_000,
            traded=False,
            eligible=False,
            abstention="low_conviction",
            created_sequence=13,
        ),
        _forecast("fc-5", "MKT-5", 900_000, 8_000, traded=True, created_sequence=14),
        _forecast(
            "fc-6",
            "MKT-6",
            500_000,
            5_000,
            traded=False,
            eligible=False,
            abstention="excluded_category",
            created_sequence=15,
        ),
    )
    resolutions = {
        "MKT-1": ResolutionOutcome.YES,
        "MKT-2": ResolutionOutcome.NO,
        "MKT-3": ResolutionOutcome.YES,
        "MKT-4": ResolutionOutcome.NO,
        "MKT-5": ResolutionOutcome.YES,
    }
    temporal = TemporalContext(
        deployment_sequence=0,
        resolution_sequences={
            "MKT-1": 100,
            "MKT-2": 100,
            "MKT-3": 100,
            "MKT-4": 100,
            "MKT-5": 100,
        },
    )
    return EvaluationInputs(
        forecasts=forecasts, resolutions=resolutions, temporal=temporal
    )


def _empty_traded_cohort_inputs() -> EvaluationInputs:
    """Build a 2-forecast fixture where every forecast is SKIPPED (never traded).

    Both markets resolve, so every forecast-track metric is well-defined, but
    the TRADED cohort is empty, making `traded_vs_skipped_brier_delta`
    genuinely undefined on the Python reference path.

    Returns:
        The raw (pre-gate) `EvaluationInputs`.
    """
    forecasts = (
        _forecast(
            "fc-a",
            "MKT-A",
            600_000,
            5_000,
            traded=False,
            eligible=False,
            abstention="low_conviction",
            created_sequence=10,
        ),
        _forecast(
            "fc-b",
            "MKT-B",
            400_000,
            5_000,
            traded=False,
            eligible=False,
            abstention="low_conviction",
            created_sequence=11,
        ),
    )
    resolutions = {"MKT-A": ResolutionOutcome.YES, "MKT-B": ResolutionOutcome.NO}
    temporal = TemporalContext(
        deployment_sequence=0,
        resolution_sequences={"MKT-A": 100, "MKT-B": 100},
    )
    return EvaluationInputs(
        forecasts=forecasts, resolutions=resolutions, temporal=temporal
    )


def _flat_probability_inputs() -> EvaluationInputs:
    """Build a 3-forecast fixture whose forecasts all share one probability.

    Every forecast carries the identical `500_000` ppm probability, so the
    forecast variance is exactly zero and the OLS calibration fit is genuinely
    undefined: the Python reference `calibration_slope` / `calibration_intercept`
    raise `ValueError` (via `_require_forecast_variance`) on these
    degenerate-but-*temporally-admitted* inputs. All three markets resolve (mixed
    YES/NO) with distinct baselines, so every other forecast-track metric stays
    well-defined -- isolating the raise to the two calibration-fit metrics.

    Returns:
        The raw (pre-gate) `EvaluationInputs`; `created_sequence` 10-12 clears
        `deployment_sequence=0` and precedes each `resolution_sequences` entry
        (100), so all three are admitted unchanged.
    """
    forecasts = (
        _forecast("fc-p", "MKT-P", 500_000, 4_000, traded=True, created_sequence=10),
        _forecast("fc-q", "MKT-Q", 500_000, 6_000, traded=True, created_sequence=11),
        _forecast(
            "fc-r",
            "MKT-R",
            500_000,
            5_000,
            traded=False,
            eligible=False,
            abstention="low_conviction",
            created_sequence=12,
        ),
    )
    resolutions = {
        "MKT-P": ResolutionOutcome.YES,
        "MKT-Q": ResolutionOutcome.NO,
        "MKT-R": ResolutionOutcome.YES,
    }
    temporal = TemporalContext(
        deployment_sequence=0,
        resolution_sequences={"MKT-P": 100, "MKT-Q": 100, "MKT-R": 100},
    )
    return EvaluationInputs(
        forecasts=forecasts, resolutions=resolutions, temporal=temporal
    )


def _corrupted_computer(metric_name: str, delta: int) -> SqlGateComputer:
    """Build a `SqlGateComputer` whose `metric_name` query is shifted by `delta`.

    Wraps the real default query for `metric_name` as a scalar subquery and
    adds `delta` to it, which is schema-agnostic: it works regardless of the
    inner query's column name(s), as long as it yields exactly one row and one
    column (a requirement `SqlGateComputer` must satisfy for every metric
    anyway).

    Args:
        metric_name: The registered metric whose query is corrupted.
        delta: The (possibly negative) integer offset to add.

    Returns:
        A `SqlGateComputer` identical to the default except for one query.
    """
    from hedgekit.evaluation.sql_gates import DEFAULT_GATE_QUERIES, SqlGateComputer

    corrupted_query = f"SELECT ({DEFAULT_GATE_QUERIES[metric_name]}) + ({delta})"
    return SqlGateComputer(
        queries={**DEFAULT_GATE_QUERIES, metric_name: corrupted_query}
    )


def _raising_computer(metric_name: str) -> SqlGateComputer:
    """Build a `SqlGateComputer` whose `metric_name` query is malformed SQL.

    Args:
        metric_name: The registered metric whose query is replaced.

    Returns:
        A `SqlGateComputer` that raises when computing `metric_name`.
    """
    from hedgekit.evaluation.sql_gates import DEFAULT_GATE_QUERIES, SqlGateComputer

    return SqlGateComputer(
        queries={
            **DEFAULT_GATE_QUERIES,
            metric_name: "SELECT * FROM this_table_does_not_exist_at_all",
        }
    )


def _built_plan() -> GatePlan:
    """Build the shared `GatePlan` this suite's crosscheck calls are scored under.

    Returns:
        The plan produced by `build_gate_plan(EvaluationConfig())`, carrying
        all nine registered metrics.
    """
    return build_gate_plan(EvaluationConfig(), paper_fill_model_version=_PFM_VERSION)


# ---------------------------------------------------------------------------
# 1. Known-answer agreement: both paths equal the hand-computed values.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_known_answer_agreement_matches_hand_computation(
    tmp_path: Path,
) -> None:
    """Both paths equal the module docstring's hand-derived values; MATCH.

    No `GateComputationMismatch` is appended and no alert fires when every
    metric agrees.
    """
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates
    from hedgekit.evaluation.registry import NOT_IMPLEMENTED, registered_metrics

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        assert result.status is CrosscheckStatus.MATCH
        assert result.plan_hash == plan.plan_hash
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        assert set(by_name) == set(registered_metrics())

        assert by_name["brier"].python_value == 54_000
        assert by_name["brier"].sql_value == 54_000
        assert by_name["brier"].window == "latest_before_close"

        assert by_name["brier_skill_vs_executable_price"].python_value == 614_285
        assert by_name["brier_skill_vs_executable_price"].sql_value == 614_285

        assert by_name["traded_vs_skipped_brier_delta"].python_value == -23_334
        assert by_name["traded_vs_skipped_brier_delta"].sql_value == -23_334

        assert by_name["fill_vs_model_slippage"].python_value is NOT_IMPLEMENTED
        assert by_name["fill_vs_model_slippage"].sql_value is NOT_IMPLEMENTED

        # Every registered metric's SQL reproduction agrees with the Python
        # reference -- including the five not hand-transcribed above
        # (log_score, expected_calibration_error, calibration_slope,
        # calibration_intercept, sharpness); see the module docstring for why.
        for comparison in result.comparisons:
            assert comparison.python_value == comparison.sql_value, comparison.name
            assert comparison.within_tolerance is True

        assert store.read_all() == []
        assert calls == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. Corrupted SQL is loud: ledgered, alerted, MISMATCH.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_corrupted_sql_is_ledgered_and_alerted_loudly(
    tmp_path: Path,
) -> None:
    """A SQL query corrupted by +1000 on `brier` is loudly reported.

    `status` is `MISMATCH`, a single `GateComputationMismatch` is appended
    naming both raw values and the plan hash, and the alert hook receives one
    `AlertSeverity.CRITICAL` call naming the metric.
    """
    from hedgekit.evaluation.crosscheck import (
        INTEGER_ROUNDING_TOLERANCE,
        CrosscheckStatus,
        crosscheck_gates,
    )

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    corrupted = _corrupted_computer("brier", 1000)
    try:
        result = crosscheck_gates(
            inputs, plan=plan, store=store, alert=hook, sql_path=corrupted
        )

        assert result.status is CrosscheckStatus.MISMATCH
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        assert by_name["brier"].python_value == 54_000
        assert by_name["brier"].sql_value == 55_000
        assert by_name["brier"].within_tolerance is False

        records = store.read_all()
        assert len(records) == 1
        assert records[-1].event_type == "GateComputationMismatch"
        assert records[-1].component == "evaluation"

        envelope = json.loads(records[-1].payload_json)
        payload = envelope["data"]
        assert payload["plan_hash"] == plan.plan_hash
        assert payload["tolerance"] == INTEGER_ROUNDING_TOLERANCE
        mismatches = {entry["name"]: entry for entry in payload["mismatches"]}
        assert "brier" in mismatches
        assert mismatches["brier"]["python_value"] == 54_000
        assert mismatches["brier"]["sql_value"] == 55_000
        assert mismatches["brier"]["window"] == "latest_before_close"
        # Every other metric agreed, so only "brier" is reported as mismatched.
        assert len(mismatches) == 1

        assert len(calls) == 1
        severity, message = calls[0]
        assert severity is AlertSeverity.CRITICAL
        assert "brier" in message
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. Tolerance boundary: delta=1 agrees, delta=2 (either sign) disagrees.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta", [1, -1])
def test_crosscheck_gates_tolerance_boundary_delta_one_is_a_match(
    tmp_path: Path, delta: int
) -> None:
    """A `brier` delta of exactly +/-1 is within the default tolerance."""
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    corrupted = _corrupted_computer("brier", delta)
    try:
        result = crosscheck_gates(
            inputs, plan=plan, store=store, alert=hook, sql_path=corrupted
        )

        assert result.status is CrosscheckStatus.MATCH
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        assert by_name["brier"].sql_value == 54_000 + delta
        assert by_name["brier"].within_tolerance is True
        assert store.read_all() == []
        assert calls == []
    finally:
        store.close()


@pytest.mark.parametrize("delta", [2, -2])
def test_crosscheck_gates_tolerance_boundary_delta_two_is_a_mismatch(
    tmp_path: Path, delta: int
) -> None:
    """A `brier` delta of +/-2 exceeds the default tolerance of 1."""
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    corrupted = _corrupted_computer("brier", delta)
    try:
        result = crosscheck_gates(
            inputs, plan=plan, store=store, alert=hook, sql_path=corrupted
        )

        assert result.status is CrosscheckStatus.MISMATCH
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        assert by_name["brier"].sql_value == 54_000 + delta
        assert by_name["brier"].within_tolerance is False
        assert len(store.read_all()) == 1
        assert len(calls) == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 4. Sentinels: NOT_IMPLEMENTED agrees, a forced int-vs-sentinel mismatches,
#    an empty-cohort UNDEFINED agrees on both paths.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_fill_vs_model_slippage_not_implemented_agrees(
    tmp_path: Path,
) -> None:
    """`fill_vs_model_slippage` is `NOT_IMPLEMENTED` on both paths -> agree."""
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates
    from hedgekit.evaluation.registry import NOT_IMPLEMENTED

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    _calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        by_name = {comparison.name: comparison for comparison in result.comparisons}
        comparison = by_name["fill_vs_model_slippage"]
        assert comparison.python_value is NOT_IMPLEMENTED
        assert comparison.sql_value is NOT_IMPLEMENTED
        assert comparison.within_tolerance is True
        assert result.status is CrosscheckStatus.MATCH
    finally:
        store.close()


def test_crosscheck_gates_forced_int_vs_sentinel_is_a_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `int` on one path against a sentinel on the other is a mismatch.

    Monkeypatches the registry's private `traded_vs_skipped_brier_delta`
    adapter to always return the `UNDEFINED` sentinel regardless of input,
    while the SQL side computes its ordinary `int` over the (non-empty-cohort)
    main fixture.
    """
    from hedgekit.evaluation import registry
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    def _always_undefined(_inputs: EvaluationInputs) -> cohorts.UndefinedBrier:
        """Return `UNDEFINED` unconditionally, ignoring the real inputs."""
        return cohorts.UNDEFINED

    monkeypatch.setattr(
        registry, "_compute_traded_vs_skipped_brier_delta", _always_undefined
    )

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        assert result.status is CrosscheckStatus.MISMATCH
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        comparison = by_name["traded_vs_skipped_brier_delta"]
        assert comparison.python_value is cohorts.UNDEFINED
        assert isinstance(comparison.sql_value, int)
        assert comparison.sql_value == -23_334
        assert comparison.within_tolerance is False
        assert len(calls) == 1
    finally:
        store.close()


def test_crosscheck_gates_empty_traded_cohort_agrees_as_undefined_on_both_paths(
    tmp_path: Path,
) -> None:
    """An empty TRADED cohort yields `UNDEFINED` on both paths -> agree."""
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    inputs = _empty_traded_cohort_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        by_name = {comparison.name: comparison for comparison in result.comparisons}
        comparison = by_name["traded_vs_skipped_brier_delta"]
        assert comparison.python_value is cohorts.UNDEFINED
        assert comparison.sql_value is cohorts.UNDEFINED
        assert comparison.within_tolerance is True
        assert result.status is CrosscheckStatus.MATCH
        assert store.read_all() == []
        assert calls == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 5. Path independence: corrupting the Python reference alone must not
#    change the SQL path's independently-computed value.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_sql_path_is_independent_of_the_python_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupting `metrics.mean_brier` alone must not perturb the SQL value.

    `hedgekit.evaluation.registry._compute_brier` delegates to
    `hedgekit.evaluation.metrics.mean_brier` via a module-attribute reference,
    so patching that one function forces the Python `brier` value to garbage
    while the (unrelated, independently implemented) SQL path must still
    report the correct `54_000` -- proving the SQL path shares no computation
    with the Python reference.
    """
    from hedgekit.evaluation import metrics as metrics_module
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    def _garbage_mean_brier(_inputs: EvaluationInputs, *, window: object) -> int:
        """Return an obviously-wrong constant, ignoring the real inputs."""
        del window
        return 999_999

    monkeypatch.setattr(metrics_module, "mean_brier", _garbage_mean_brier)

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        assert result.status is CrosscheckStatus.MISMATCH
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        comparison = by_name["brier"]
        assert comparison.python_value == 999_999
        assert comparison.sql_value == 54_000
        assert len(calls) == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 6. Never averaged: both raw values are carried verbatim, never blended.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_never_averages_the_two_raw_values(tmp_path: Path) -> None:
    """A mismatched comparison carries both raw values verbatim, not a blend.

    With `brier` corrupted by +250, the comparison must carry `54_000` and
    `54_250` distinctly -- never their average (`54_125`) or any other
    combined figure.
    """
    from hedgekit.evaluation.crosscheck import crosscheck_gates

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    _calls, hook = _recording_alert_hook()
    corrupted = _corrupted_computer("brier", 250)
    try:
        result = crosscheck_gates(
            inputs, plan=plan, store=store, alert=hook, sql_path=corrupted
        )

        by_name = {comparison.name: comparison for comparison in result.comparisons}
        comparison = by_name["brier"]
        assert comparison.python_value == 54_000
        assert comparison.sql_value == 54_250
        average = (comparison.python_value + comparison.sql_value) // 2
        assert comparison.python_value != average
        assert comparison.sql_value != average
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 7. Unresolved markets are excluded by both paths, changing nothing.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_unresolved_market_is_excluded_from_both_paths(
    tmp_path: Path,
) -> None:
    """Including the unresolved MKT-6 forecast changes no computed value.

    Comparing a run over the full 6-forecast fixture against a run over just
    its first 5 (resolved) forecasts must yield byte-identical `brier`
    comparisons on both paths and an overall `MATCH` in both runs.
    """
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    with_unresolved = _main_admitted_inputs()
    without_unresolved = EvaluationInputs(
        forecasts=with_unresolved.forecasts[:5],
        resolutions=with_unresolved.resolutions,
        temporal=with_unresolved.temporal,
    )
    plan = _built_plan()
    store_a = _ledger_store(tmp_path / "a")
    store_b = _ledger_store(tmp_path / "b")
    calls_a, hook_a = _recording_alert_hook()
    calls_b, hook_b = _recording_alert_hook()
    try:
        result_with = crosscheck_gates(
            with_unresolved, plan=plan, store=store_a, alert=hook_a
        )
        result_without = crosscheck_gates(
            without_unresolved, plan=plan, store=store_b, alert=hook_b
        )

        by_name_with = {c.name: c for c in result_with.comparisons}
        by_name_without = {c.name: c for c in result_without.comparisons}
        assert by_name_with["brier"].python_value == 54_000
        assert by_name_without["brier"].python_value == 54_000
        assert by_name_with["brier"].sql_value == 54_000
        assert by_name_without["brier"].sql_value == 54_000
        assert result_with.status is CrosscheckStatus.MATCH
        assert result_without.status is CrosscheckStatus.MATCH
        assert calls_a == []
        assert calls_b == []
    finally:
        store_a.close()
        store_b.close()


# ---------------------------------------------------------------------------
# 8. A raising SQL query is a loud mismatch, never a silent swallow.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_a_raising_sql_query_is_not_swallowed(
    tmp_path: Path,
) -> None:
    """A SQL query that raises is recorded as a mismatch, ledgered, alerted.

    `crosscheck_gates` itself must not raise: the failing metric's exception
    is caught and turned into a reported mismatch rather than propagating out
    of, or being silently absorbed by, the crosscheck.
    """
    from hedgekit.evaluation.crosscheck import CrosscheckStatus, crosscheck_gates

    inputs = _main_admitted_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    raising = _raising_computer("brier")
    try:
        result = crosscheck_gates(
            inputs, plan=plan, store=store, alert=hook, sql_path=raising
        )

        assert result.status is CrosscheckStatus.MISMATCH
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        assert by_name["brier"].python_value == 54_000
        # The SQL side never silently falls back to the correct value.
        assert by_name["brier"].sql_value != 54_000
        assert by_name["brier"].within_tolerance is False

        records = store.read_all()
        assert len(records) == 1
        assert records[-1].event_type == "GateComputationMismatch"

        assert len(calls) == 1
        assert calls[0][0] is AlertSeverity.CRITICAL
        assert "brier" in calls[0][1]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 9. A raising Python reference metric is a loud mismatch, never a propagated
#    exception -- the symmetric twin of section 8's raising SQL query.
# ---------------------------------------------------------------------------


def test_crosscheck_gates_a_raising_python_reference_metric_is_not_propagated(
    tmp_path: Path,
) -> None:
    """A degenerate-but-admitted input that makes the Python path raise is loud.

    All-equal-probability forecasts drive the reference `calibration_slope` and
    `calibration_intercept` to raise `ValueError` (zero forecast variance). The
    crosscheck must NOT let that exception propagate out (the pre-fix bug): it
    catches the raise, carries it as the `PYTHON_COMPUTE_FAILED` sentinel, and
    reports a loud, ledgered `MISMATCH` naming the offending metrics -- exactly
    symmetric to how a raising SQL query degrades to `SQL_QUERY_FAILED`. Because
    the SQL calibration UDFs also raise on zero variance, each offending metric
    is `PYTHON_COMPUTE_FAILED` (Python) vs `SQL_QUERY_FAILED` (SQL): two distinct
    failure sentinels that still disagree by identity, so the double failure is
    itself flagged rather than silently agreeing.
    """
    from hedgekit.evaluation.crosscheck import (
        PYTHON_COMPUTE_FAILED,
        CrosscheckStatus,
        crosscheck_gates,
    )

    inputs = _flat_probability_inputs()
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    calls, hook = _recording_alert_hook()
    try:
        result = crosscheck_gates(inputs, plan=plan, store=store, alert=hook)

        assert result.status is CrosscheckStatus.MISMATCH
        by_name = {comparison.name: comparison for comparison in result.comparisons}
        slope = by_name["calibration_slope"]
        intercept = by_name["calibration_intercept"]
        assert slope.python_value is PYTHON_COMPUTE_FAILED
        assert intercept.python_value is PYTHON_COMPUTE_FAILED
        assert slope.within_tolerance is False
        assert intercept.within_tolerance is False

        records = store.read_all()
        assert len(records) == 1
        assert records[-1].event_type == "GateComputationMismatch"

        envelope = json.loads(records[-1].payload_json)
        mismatches = {entry["name"]: entry for entry in envelope["data"]["mismatches"]}
        assert "calibration_slope" in mismatches
        assert "calibration_intercept" in mismatches
        # The Python raise is rendered by the sentinel's name, never blended away.
        assert mismatches["calibration_slope"]["python_value"] == "COMPUTE_FAILED"

        assert len(calls) == 1
        severity, message = calls[0]
        assert severity is AlertSeverity.CRITICAL
        assert "calibration_slope" in message
    finally:
        store.close()
