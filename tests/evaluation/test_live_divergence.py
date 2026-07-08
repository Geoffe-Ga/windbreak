"""Failing-first tests for `windbreak.evaluation.live_divergence` (issue #58, RED).

`windbreak.evaluation.live_divergence` does not exist yet, so every test below
imports its new symbols (`monitor_live_divergence`, `LiveDivergenceSampled`,
`LiveDivergenceBreached`) as the FIRST statement inside the test body --
matching this package's established RED convention in `test_dual_path.py` /
`test_preregistration.py` -- so each test collects and fails independently on
its own
`ModuleNotFoundError: No module named 'windbreak.evaluation.live_divergence'`.
Symbols from already-existing modules (`windbreak.alerts.registry`,
`windbreak.config.schema`, `windbreak.evaluation.preregistration`,
`windbreak.evaluation.registry`, `windbreak.evaluation.resolution`,
`windbreak.evaluation.temporal`, `windbreak.ledger.store`) are imported at
module scope. `windbreak.riskkernel.demotion.DemotionTrigger` already exists
(issue #33) and is imported at module scope too.

Pins issue #58's per-run monitor (SPEC §10.9 / §10.10):

- Every call appends exactly ONE `LiveDivergenceSampled` regardless of outcome.
- Each of the two series (`live_slippage_ratio`, `live_brier_degradation`) that
  breaches its threshold appends exactly ONE `LiveDivergenceBreached`, fires
  exactly ONE `AlertSeverity.CRITICAL` alert, and calls `fire_trigger` with the
  matching `DemotionTrigger` (`LIVE_PAPER_SLIPPAGE_DIVERGENCE` for slippage,
  `ROLLING_BRIER_DEGRADATION` for the Brier band).
- A PAPER-only run (no LIVE forecasts, no execution records) yields both
  series `UNDEFINED`, no breach, no alert, no trigger firing, and never raises
  -- an ordinary early-deployment state.
- A model-version mismatch between a recorded execution-quality fill and the
  plan's `paper_fill_model_version` fails closed (`ValueError`) before
  anything is ledgered.

ASSUMPTION this file pins (the architecture plan's prose is not a literal
signature): `fire_trigger` is a plain one-argument callable
(`Callable[[DemotionTrigger], Mode | None]`) invoked as `fire_trigger(trigger)`
-- the headline kernel-wiring test (`test_live_divergence_kernel_wiring.py`)
passes `kernel.fire_demotion_trigger` (a bound method) directly for this
parameter, which only type-checks if `fire_trigger` is an ordinary callable
rather than an object exposing a differently-named method. The
`LiveDivergenceSampled`/`LiveDivergenceBreached` payload key names asserted
below (`live_slippage_ratio_ppm`, `live_brier_degradation_ppm`,
`live_slippage_ratio_limit_ppm`, `live_brier_degradation_band_ppm`,
`live_rolling_window_size`, `execution_record_count`, `live_forecast_count`,
`paper_forecast_count`, `plan_hash`, `trigger`) are this suite's best-effort
naming, not architect-confirmed literal strings; a mismatch is a design point
to reconcile, not a signal to silently rename the assertions to match whatever
lands first.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from windbreak.alerts.registry import AlertSeverity
from windbreak.config.schema import EvaluationConfig
from windbreak.evaluation.preregistration import build_gate_plan
from windbreak.evaluation.registry import EvaluationInputs, FixtureForecast
from windbreak.evaluation.resolution import ResolutionOutcome
from windbreak.evaluation.temporal import TemporalContext
from windbreak.ledger.store import SqliteLedgerStore
from windbreak.numeric.types import ProbabilityPpm
from windbreak.riskkernel.demotion import DemotionTrigger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from windbreak.evaluation.preregistration import GatePlan

    _AlertHook = Callable[[AlertSeverity, str], None]

#: Fixed paper fill-model version shared by every plan this suite builds.
_PFM_VERSION = "pfm-live-divergence-test"


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


def _recording_fire_trigger() -> tuple[list[DemotionTrigger], object]:
    """Build a recording fake for the `fire_trigger` callable parameter.

    Returns:
        A `(calls, fire_trigger)` pair: `calls` accumulates every
        `DemotionTrigger` the monitor fired, in call order; `fire_trigger` is
        the plain callable to pass through.
    """
    calls: list[DemotionTrigger] = []

    def _fire(trigger: DemotionTrigger) -> None:
        """Record one fired trigger."""
        calls.append(trigger)

    return calls, _fire


def _built_plan() -> GatePlan:
    """Build the shared `GatePlan` this suite's monitor calls are scored under.

    Returns:
        The plan produced by `build_gate_plan`, carrying the confirmed live
        thresholds off a stock `EvaluationConfig` (`live_rolling_window_size`,
        `live_slippage_ratio_limit_ppm`, `live_brier_degradation_band_ppm`).
    """
    return build_gate_plan(EvaluationConfig(), paper_fill_model_version=_PFM_VERSION)


def _paper_forecast(
    forecast_id: str,
    market_ticker: str,
    probability_ppm: int,
    *,
    created_sequence: int,
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


def _live_forecast(
    forecast_id: str,
    market_ticker: str,
    probability_ppm: int,
    *,
    created_sequence: int,
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
        market_ticker="MKT-EXEC",
        side="YES",
        filled_centis=100,
        actual_cost_micros=actual_cost_micros,
        modeled_cost_micros=modeled_cost_micros,
        model_version=_PFM_VERSION,
        created_sequence=sequence,
    )


def _paper_only_inputs() -> EvaluationInputs:
    """Build a 2-forecast, all-PAPER, all-resolved fixture with no fills.

    Returns:
        The raw (temporally-admittable) `EvaluationInputs`: no LIVE forecast
        and no execution record anywhere, so both series are genuinely
        undefined.
    """
    forecasts = (
        _paper_forecast("fc-p1", "MKT-P1", 600_000, created_sequence=10),
        _paper_forecast("fc-p2", "MKT-P2", 400_000, created_sequence=11),
    )
    resolutions = {"MKT-P1": ResolutionOutcome.YES, "MKT-P2": ResolutionOutcome.NO}
    temporal = TemporalContext(
        deployment_sequence=0,
        resolution_sequences={"MKT-P1": 100, "MKT-P2": 100},
    )
    return EvaluationInputs(
        forecasts=forecasts,
        resolutions=resolutions,
        temporal=temporal,
        execution_records=(),
    )


def _slippage_breach_only_inputs() -> EvaluationInputs:
    """Build inputs whose slippage ratio breaches but whose Brier does not.

    Two execution-quality records: `sum(actual) = 4_000_000`,
    `sum(modeled) = 2_000_000`; `ratio = 2_000_000` ppm, over the confirmed
    `1_500_000` limit. No LIVE forecast exists, so `live_brier_degradation` is
    `UNDEFINED` (empty LIVE cohort) rather than breaching.

    Returns:
        The raw `EvaluationInputs`.
    """
    forecasts = (_paper_forecast("fc-p1", "MKT-P1", 600_000, created_sequence=10),)
    resolutions = {"MKT-P1": ResolutionOutcome.YES}
    temporal = TemporalContext(
        deployment_sequence=0, resolution_sequences={"MKT-P1": 100}
    )
    execution_records = (
        _execution_record(
            "F-1",
            actual_cost_micros=3_000_000,
            modeled_cost_micros=1_500_000,
            sequence=1,
        ),
        _execution_record(
            "F-2", actual_cost_micros=1_000_000, modeled_cost_micros=500_000, sequence=2
        ),
    )
    return EvaluationInputs(
        forecasts=forecasts,
        resolutions=resolutions,
        temporal=temporal,
        execution_records=execution_records,
    )


def _brier_breach_only_inputs() -> EvaluationInputs:
    """Build inputs whose Brier degradation breaches but whose slippage does not.

    3 LIVE forecasts (`p=500_000`, outcome YES) each score Brier term
    `(500_000-1_000_000)^2 = 250_000_000_000`; mean = `250_000` ppm exactly.
    3 PAPER forecasts (`p=700_000`, outcome YES) each score
    `(700_000-1_000_000)^2 = 90_000_000_000`; mean = `90_000` ppm exactly.
    `degradation = 250_000 - 90_000 = 160_000` ppm, over the confirmed
    `50_000` band. No execution record exists, so `live_slippage_ratio` is
    `UNDEFINED` rather than breaching.

    Returns:
        The raw `EvaluationInputs`.
    """
    live_forecasts = tuple(
        _live_forecast(f"fc-l{i}", f"MKT-L{i}", 500_000, created_sequence=10 + i)
        for i in range(3)
    )
    paper_forecasts = tuple(
        _paper_forecast(f"fc-p{i}", f"MKT-P{i}", 700_000, created_sequence=20 + i)
        for i in range(3)
    )
    forecasts = live_forecasts + paper_forecasts
    resolutions = {
        forecast.market_ticker: ResolutionOutcome.YES for forecast in forecasts
    }
    temporal = TemporalContext(
        deployment_sequence=0,
        resolution_sequences=dict.fromkeys(resolutions, 100),
    )
    return EvaluationInputs(
        forecasts=forecasts,
        resolutions=resolutions,
        temporal=temporal,
        execution_records=(),
    )


def _both_breach_inputs() -> EvaluationInputs:
    """Build inputs where both series breach their thresholds simultaneously.

    Combines `_slippage_breach_only_inputs`'s execution records with
    `_brier_breach_only_inputs`'s LIVE/PAPER forecast split.

    Returns:
        The raw `EvaluationInputs`.
    """
    slippage = _slippage_breach_only_inputs()
    brier = _brier_breach_only_inputs()
    forecasts = slippage.forecasts + brier.forecasts
    resolutions = {**slippage.resolutions, **brier.resolutions}
    temporal = TemporalContext(
        deployment_sequence=0,
        resolution_sequences=dict.fromkeys(resolutions, 100),
    )
    return EvaluationInputs(
        forecasts=forecasts,
        resolutions=resolutions,
        temporal=temporal,
        execution_records=slippage.execution_records,
    )


# ---------------------------------------------------------------------------
# 1. PAPER-only: both series UNDEFINED, one sampled event, no breach.
# ---------------------------------------------------------------------------


def test_monitor_live_divergence_paper_only_is_undefined_and_silent(
    tmp_path: Path,
) -> None:
    """A PAPER-only run (no LIVE forecast, no execution record) never raises,
    ledgers exactly one `LiveDivergenceSampled` with both series `UNDEFINED`,
    and appends no breach event, fires no alert, and calls no trigger.
    """
    from windbreak.evaluation.live_divergence import (
        LiveDivergenceSampled,
        monitor_live_divergence,
    )
    from windbreak.evaluation.registry import gate_evaluation_inputs

    inputs, _rejections = gate_evaluation_inputs(_paper_only_inputs())
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    alert_calls, alert_hook = _recording_alert_hook()
    trigger_calls, fire_trigger = _recording_fire_trigger()
    try:
        monitor_live_divergence(
            inputs,
            plan=plan,
            store=store,
            alert=alert_hook,
            fire_trigger=fire_trigger,
            component="evaluation",
        )

        records = store.read_all()
        sampled = [r for r in records if r.event_type == "LiveDivergenceSampled"]
        breached = [r for r in records if r.event_type == "LiveDivergenceBreached"]
        assert len(sampled) == 1
        assert breached == []
        assert alert_calls == []
        assert trigger_calls == []

        envelope = json.loads(sampled[0].payload_json)
        payload = envelope["data"]
        assert payload["live_slippage_ratio_ppm"] == "UNDEFINED"
        assert payload["live_brier_degradation_ppm"] == "UNDEFINED"
        assert payload["plan_hash"] == plan.plan_hash

        assert LiveDivergenceSampled  # symbol exists; imported above
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. Model-version mismatch fails closed, before anything is ledgered.
# ---------------------------------------------------------------------------


def test_monitor_live_divergence_model_version_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    """An execution record whose `model_version` disagrees with the plan's
    `paper_fill_model_version` raises `ValueError`; nothing is ledgered.
    """
    from windbreak.evaluation.execution_quality import ExecutionQualityRecord
    from windbreak.evaluation.live_divergence import monitor_live_divergence

    mismatched_record = ExecutionQualityRecord(
        fill_id="F-mismatch",
        market_ticker="MKT-EXEC",
        side="YES",
        filled_centis=100,
        actual_cost_micros=1_000_000,
        modeled_cost_micros=900_000,
        model_version="pfm-a-completely-different-version",
        created_sequence=1,
    )
    inputs = EvaluationInputs(
        forecasts=(),
        resolutions={},
        temporal=TemporalContext(deployment_sequence=0, resolution_sequences={}),
        execution_records=(mismatched_record,),
    )
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    _alert_calls, alert_hook = _recording_alert_hook()
    _trigger_calls, fire_trigger = _recording_fire_trigger()
    try:
        with pytest.raises(ValueError, match="model_version"):
            monitor_live_divergence(
                inputs,
                plan=plan,
                store=store,
                alert=alert_hook,
                fire_trigger=fire_trigger,
                component="evaluation",
            )

        assert store.read_all() == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. Single-threshold breaches: slippage-only and Brier-only.
# ---------------------------------------------------------------------------


def test_monitor_live_divergence_slippage_only_breach(tmp_path: Path) -> None:
    """A slippage-only breach ledgers one sampled + one breached event, fires
    one CRITICAL alert, and calls `fire_trigger` with
    `LIVE_PAPER_SLIPPAGE_DIVERGENCE` exactly once; the Brier series stays
    `UNDEFINED` and triggers nothing.
    """
    from windbreak.evaluation.live_divergence import monitor_live_divergence
    from windbreak.evaluation.registry import gate_evaluation_inputs

    inputs, _rejections = gate_evaluation_inputs(_slippage_breach_only_inputs())
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    alert_calls, alert_hook = _recording_alert_hook()
    trigger_calls, fire_trigger = _recording_fire_trigger()
    try:
        monitor_live_divergence(
            inputs,
            plan=plan,
            store=store,
            alert=alert_hook,
            fire_trigger=fire_trigger,
            component="evaluation",
        )

        records = store.read_all()
        sampled = [r for r in records if r.event_type == "LiveDivergenceSampled"]
        breached = [r for r in records if r.event_type == "LiveDivergenceBreached"]
        assert len(sampled) == 1
        assert len(breached) == 1

        payload = json.loads(breached[0].payload_json)["data"]
        assert payload["trigger"] == "LIVE_PAPER_SLIPPAGE_DIVERGENCE"
        assert payload["live_slippage_ratio_ppm"] == 2_000_000
        assert payload["plan_hash"] == plan.plan_hash

        assert len(alert_calls) == 1
        assert alert_calls[0][0] is AlertSeverity.CRITICAL
        assert trigger_calls == [DemotionTrigger.LIVE_PAPER_SLIPPAGE_DIVERGENCE]
    finally:
        store.close()


def test_monitor_live_divergence_brier_only_breach(tmp_path: Path) -> None:
    """A Brier-degradation-only breach fires `ROLLING_BRIER_DEGRADATION`
    exactly once; the slippage series stays `UNDEFINED` and triggers nothing.
    """
    from windbreak.evaluation.live_divergence import monitor_live_divergence
    from windbreak.evaluation.registry import gate_evaluation_inputs

    inputs, _rejections = gate_evaluation_inputs(_brier_breach_only_inputs())
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    alert_calls, alert_hook = _recording_alert_hook()
    trigger_calls, fire_trigger = _recording_fire_trigger()
    try:
        monitor_live_divergence(
            inputs,
            plan=plan,
            store=store,
            alert=alert_hook,
            fire_trigger=fire_trigger,
            component="evaluation",
        )

        records = store.read_all()
        breached = [r for r in records if r.event_type == "LiveDivergenceBreached"]
        assert len(breached) == 1

        payload = json.loads(breached[0].payload_json)["data"]
        assert payload["trigger"] == "ROLLING_BRIER_DEGRADATION"
        assert payload["live_brier_degradation_ppm"] == 160_000

        assert len(alert_calls) == 1
        assert trigger_calls == [DemotionTrigger.ROLLING_BRIER_DEGRADATION]
    finally:
        store.close()


def test_monitor_live_divergence_both_breach_fires_both_triggers(
    tmp_path: Path,
) -> None:
    """When both series breach simultaneously, two distinct `LiveDivergenceBreached`
    events are ledgered (one per trigger name), two CRITICAL alerts fire, and
    `fire_trigger` is called once per trigger -- the fail-safe "double
    one-rung demotion" reading, never a single conflated event.
    """
    from windbreak.evaluation.live_divergence import monitor_live_divergence
    from windbreak.evaluation.registry import gate_evaluation_inputs

    inputs, _rejections = gate_evaluation_inputs(_both_breach_inputs())
    plan = _built_plan()
    store = _ledger_store(tmp_path)
    alert_calls, alert_hook = _recording_alert_hook()
    trigger_calls, fire_trigger = _recording_fire_trigger()
    try:
        monitor_live_divergence(
            inputs,
            plan=plan,
            store=store,
            alert=alert_hook,
            fire_trigger=fire_trigger,
            component="evaluation",
        )

        records = store.read_all()
        sampled = [r for r in records if r.event_type == "LiveDivergenceSampled"]
        breached = [r for r in records if r.event_type == "LiveDivergenceBreached"]
        assert len(sampled) == 1
        assert len(breached) == 2

        triggers = {
            json.loads(record.payload_json)["data"]["trigger"] for record in breached
        }
        assert triggers == {
            "LIVE_PAPER_SLIPPAGE_DIVERGENCE",
            "ROLLING_BRIER_DEGRADATION",
        }

        assert len(alert_calls) == 2
        assert set(trigger_calls) == {
            DemotionTrigger.LIVE_PAPER_SLIPPAGE_DIVERGENCE,
            DemotionTrigger.ROLLING_BRIER_DEGRADATION,
        }
        assert len(trigger_calls) == 2
    finally:
        store.close()
