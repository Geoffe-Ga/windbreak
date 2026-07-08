"""Failing-first tests for `windbreak.evaluation.costs` (issue #55, RED).

`windbreak.evaluation.costs` does not exist yet, so every test below imports
its new symbols from that module as the FIRST statement inside the test body
(matching this package's established RED convention; see
`test_preregistration.py` / `test_cohorts.py`) so each test collects and fails
independently on its own
`ModuleNotFoundError: No module named 'windbreak.evaluation.costs'`.

Pins issue #55's research-cost meter:

- `CostMeter` (frozen, slotted): `total_research_cost_micros`,
  `resolved_forecast_count`, `profitable_trade_count`, `trade_count`, and
  three `MoneyMicros | None` fields (`cost_per_resolved_forecast_micros`,
  `cost_per_profitable_trade_micros`, `cost_adjusted_expectancy_micros`),
  each `None` exactly when its own denominator count is `0`.
- `aggregate_research_costs(records, *, resolutions, trade_pnls_micros)` sums
  `research_cost_micros` over **every** record regardless of `triage_stage`
  (a `triage_only` record's cost is real spend and counts); per-unit costs
  are ceiling-rounded (`OVERSTATE_COST` -- never understate a cost), and the
  cost-adjusted expectancy is floor-rounded (`UNDERSTATE_EQUITY` -- never
  overstate an equity-side figure):
  `cost_per_resolved_forecast_micros = ceil(total_cost / resolved_count)`,
  `cost_per_profitable_trade_micros = ceil(total_cost / profitable_count)`,
  `cost_adjusted_expectancy_micros = floor((sum(pnl) - total_cost) / trade_count)`.
- Rejects a negative or `bool`-masquerading-as-`int` `research_cost_micros`
  (per the repo-wide "no bool-as-int" rule; `windbreak.forecast.records.ForecastRecord`
  itself does not validate this field, so `aggregate_research_costs` must).

Resolved API detail this suite assumes and the implementer must honor: both
`resolutions: Mapping[str, ResolutionOutcome]` and
`trade_pnls_micros: Mapping[str, int]` are keyed by **`market_ticker`**, not
`forecast_id` -- mirroring `windbreak.evaluation.registry.EvaluationInputs.resolutions`
(itself keyed by `market_ticker`, per that class's own docstring) and every
other place this codebase associates a forecast with its market's ground
truth. A `ForecastRecord`'s own `market_ticker` field is therefore the join
key `aggregate_research_costs` must use to look each record up in both
mappings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from windbreak.evaluation.resolution import ResolutionOutcome
from windbreak.forecast.records import ForecastRecord
from windbreak.numeric.types import MoneyMicros

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Literal


def _forecast_record(
    *,
    forecast_id: str,
    market_ticker: str,
    research_cost_micros: int,
    triage_stage: Literal["triage_only", "full"] = "full",
    eligible_for_live: bool = True,
) -> ForecastRecord:
    """Build a minimal, valid `ForecastRecord` varying only the cost fields.

    Args:
        forecast_id: Stable identifier of the forecast record.
        market_ticker: Ticker of the market this forecast is about.
        research_cost_micros: Total research cost, in micros (the field these
            tests vary; deliberately unvalidated by `ForecastRecord` itself).
        triage_stage: `"triage_only"` or `"full"` (SPEC S8.4).
        eligible_for_live: Whether the record may back a live order; must be
            `False` when `triage_stage == "triage_only"`.

    Returns:
        The constructed `ForecastRecord`.
    """
    return ForecastRecord(
        forecast_id=forecast_id,
        market_ticker=market_ticker,
        normalized_question_hash=f"hash-{forecast_id}",
        probability_ppm=500_000,
        ci_low_ppm=400_000,
        ci_high_ppm=600_000,
        model_votes=(),
        vote_dispersion_ppm=0,
        rationale_markdown="because",
        citations=(),
        source_quality_notes=(),
        research_cost_micros=research_cost_micros,
        triage_stage=triage_stage,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        forecast_horizon_hours=24,
        market_price_baseline_pips=5_000,
        baseline_quote_snapshot_id="snap-1",
        coherence_group_sum_ppm=None,
        coherence_flag=False,
        abstention_reason=None,
        eligible_for_live=eligible_for_live,
    )


# ---------------------------------------------------------------------------
# 1. Primary hand-computed scenario (3 records, one triage_only, 2 resolved,
#    2 trades, 1 profitable).
# ---------------------------------------------------------------------------


def test_aggregate_research_costs_matches_hand_computation_including_triage_only() -> (
    None
):
    """Hand computation over 3 records (one `triage_only`), 2 resolved, 2 trades.

        MKT-A: full,        cost=3_000_000, resolved=YES, traded, pnl=+5_000_000
        MKT-B: full,        cost=2_000_000, resolved=NO,  traded, pnl=-1_000_000
        MKT-C: triage_only, cost=2_000_000, unresolved,   not traded

    total_research_cost_micros = 3_000_000 + 2_000_000 + 2_000_000 = 7_000_000
    (the `triage_only` record's cost counts -- it is real spend regardless of
    whether the record ever became live-eligible).
    resolved_forecast_count = 2 (MKT-A, MKT-B; MKT-C has no resolution entry).
    trade_count = 2, profitable_trade_count = 1 (only MKT-A's pnl is positive).

    cost_per_resolved_forecast_micros = ceil(7_000_000 / 2) = 3_500_000.
    cost_per_profitable_trade_micros = ceil(7_000_000 / 1) = 7_000_000.
    cost_adjusted_expectancy_micros =
        floor((5_000_000 + -1_000_000 - 7_000_000) / 2) = floor(-3_000_000 / 2)
        = -1_500_000 (exact; both directions agree here).
    """
    from windbreak.evaluation.costs import CostMeter, aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a", market_ticker="MKT-A", research_cost_micros=3_000_000
        ),
        _forecast_record(
            forecast_id="fc-b", market_ticker="MKT-B", research_cost_micros=2_000_000
        ),
        _forecast_record(
            forecast_id="fc-c",
            market_ticker="MKT-C",
            research_cost_micros=2_000_000,
            triage_stage="triage_only",
            eligible_for_live=False,
        ),
    )
    resolutions: Mapping[str, ResolutionOutcome] = {
        "MKT-A": ResolutionOutcome.YES,
        "MKT-B": ResolutionOutcome.NO,
    }
    trade_pnls_micros: Mapping[str, int] = {
        "MKT-A": 5_000_000,
        "MKT-B": -1_000_000,
    }

    meter = aggregate_research_costs(
        records, resolutions=resolutions, trade_pnls_micros=trade_pnls_micros
    )

    assert isinstance(meter, CostMeter)
    assert meter.total_research_cost_micros == 7_000_000
    assert meter.resolved_forecast_count == 2
    assert meter.profitable_trade_count == 1
    assert meter.trade_count == 2

    assert meter.cost_per_resolved_forecast_micros == MoneyMicros(3_500_000)
    assert meter.cost_per_profitable_trade_micros == MoneyMicros(7_000_000)
    assert meter.cost_adjusted_expectancy_micros == MoneyMicros(-1_500_000)

    for value in (
        meter.cost_per_resolved_forecast_micros,
        meter.cost_per_profitable_trade_micros,
        meter.cost_adjusted_expectancy_micros,
    ):
        assert isinstance(value, MoneyMicros)


# ---------------------------------------------------------------------------
# 2. Rounding directions, exercised with numbers that do not divide evenly.
# ---------------------------------------------------------------------------


def test_aggregate_research_costs_cost_per_resolved_ceilings_up_overstate_cost() -> (
    None
):
    """`cost_per_resolved_forecast_micros` rounds toward positive infinity.

    3 records, all resolved, total cost 10_000_000 micros:
    ceil(10_000_000 / 3) = ceil(3_333_333.33) = 3_333_334 (never 3_333_333,
    which would understate the true per-forecast cost).
    """
    from windbreak.evaluation.costs import aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a", market_ticker="MKT-A", research_cost_micros=4_000_000
        ),
        _forecast_record(
            forecast_id="fc-b", market_ticker="MKT-B", research_cost_micros=3_000_000
        ),
        _forecast_record(
            forecast_id="fc-c", market_ticker="MKT-C", research_cost_micros=3_000_000
        ),
    )
    resolutions: Mapping[str, ResolutionOutcome] = {
        "MKT-A": ResolutionOutcome.YES,
        "MKT-B": ResolutionOutcome.NO,
        "MKT-C": ResolutionOutcome.YES,
    }

    meter = aggregate_research_costs(
        records, resolutions=resolutions, trade_pnls_micros={}
    )

    assert meter.total_research_cost_micros == 10_000_000
    assert meter.resolved_forecast_count == 3
    assert meter.cost_per_resolved_forecast_micros == MoneyMicros(3_333_334)


def test_aggregate_research_costs_expectancy_floors_down_understate_equity() -> None:
    """`cost_adjusted_expectancy_micros` rounds toward negative infinity.

    2 records, total cost 5_000_000 micros; 3 trades with pnl summing to
    1_000_000 (one profitable, two not): diff = 1_000_000 - 5_000_000 =
    -4_000_000; floor(-4_000_000 / 3) = -1_333_334 (more negative than plain
    truncation's -1_333_333, which would overstate the equity-side figure).
    """
    from windbreak.evaluation.costs import aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a", market_ticker="MKT-A", research_cost_micros=2_500_000
        ),
        _forecast_record(
            forecast_id="fc-b", market_ticker="MKT-B", research_cost_micros=2_500_000
        ),
    )
    resolutions: Mapping[str, ResolutionOutcome] = {
        "MKT-A": ResolutionOutcome.YES,
        "MKT-B": ResolutionOutcome.NO,
    }
    trade_pnls_micros: Mapping[str, int] = {
        "MKT-X": 2_000_000,
        "MKT-Y": -500_000,
        "MKT-Z": -500_000,
    }

    meter = aggregate_research_costs(
        records, resolutions=resolutions, trade_pnls_micros=trade_pnls_micros
    )

    assert meter.trade_count == 3
    assert meter.profitable_trade_count == 1
    assert meter.cost_adjusted_expectancy_micros == MoneyMicros(-1_333_334)


# ---------------------------------------------------------------------------
# 3. Zero-denominator cases: the corresponding field is None.
# ---------------------------------------------------------------------------


def test_aggregate_research_costs_zero_resolved_yields_none_cost_per_resolved() -> None:
    """No resolved market -> `cost_per_resolved_forecast_micros` is `None`."""
    from windbreak.evaluation.costs import aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a", market_ticker="MKT-A", research_cost_micros=1_000_000
        ),
    )

    meter = aggregate_research_costs(records, resolutions={}, trade_pnls_micros={})

    assert meter.resolved_forecast_count == 0
    assert meter.cost_per_resolved_forecast_micros is None
    assert meter.total_research_cost_micros == 1_000_000


def test_aggregate_research_costs_zero_profitable_yields_none_per_profitable() -> None:
    """No profitable trade -> `cost_per_profitable_trade_micros` is `None`."""
    from windbreak.evaluation.costs import aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a", market_ticker="MKT-A", research_cost_micros=1_000_000
        ),
    )
    resolutions: Mapping[str, ResolutionOutcome] = {"MKT-A": ResolutionOutcome.YES}
    trade_pnls_micros: Mapping[str, int] = {"MKT-A": -500_000}

    meter = aggregate_research_costs(
        records, resolutions=resolutions, trade_pnls_micros=trade_pnls_micros
    )

    assert meter.trade_count == 1
    assert meter.profitable_trade_count == 0
    assert meter.cost_per_profitable_trade_micros is None
    # trade_count is nonzero, so the expectancy is still well-defined.
    assert meter.cost_adjusted_expectancy_micros == MoneyMicros(-1_500_000)


def test_aggregate_research_costs_zero_trades_yields_none_trade_fields() -> None:
    """No trades at all -> both trade-denominated fields are `None`."""
    from windbreak.evaluation.costs import aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a", market_ticker="MKT-A", research_cost_micros=1_000_000
        ),
    )
    resolutions: Mapping[str, ResolutionOutcome] = {"MKT-A": ResolutionOutcome.YES}

    meter = aggregate_research_costs(
        records, resolutions=resolutions, trade_pnls_micros={}
    )

    assert meter.trade_count == 0
    assert meter.profitable_trade_count == 0
    assert meter.cost_per_profitable_trade_micros is None
    assert meter.cost_adjusted_expectancy_micros is None
    # The resolved-forecast side is unaffected by there being zero trades.
    assert meter.resolved_forecast_count == 1
    assert meter.cost_per_resolved_forecast_micros == MoneyMicros(1_000_000)


# ---------------------------------------------------------------------------
# 4. Input validation: negative and bool-as-int research costs are rejected.
# ---------------------------------------------------------------------------


def test_aggregate_research_costs_rejects_negative_research_cost() -> None:
    """A negative `research_cost_micros` raises `ValueError`."""
    from windbreak.evaluation.costs import aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a", market_ticker="MKT-A", research_cost_micros=-1
        ),
    )

    with pytest.raises(ValueError, match="research_cost_micros"):
        aggregate_research_costs(records, resolutions={}, trade_pnls_micros={})


def test_aggregate_research_costs_rejects_bool_masquerading_as_research_cost() -> None:
    """A `bool` `research_cost_micros` (an `int` subclass) raises `TypeError`."""
    from windbreak.evaluation.costs import aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a",
            market_ticker="MKT-A",
            research_cost_micros=cast("int", True),
        ),
    )

    with pytest.raises(TypeError, match="research_cost_micros"):
        aggregate_research_costs(records, resolutions={}, trade_pnls_micros={})


def test_aggregate_research_costs_rejects_bool_masquerading_as_trade_pnl() -> None:
    """A `bool` `trade_pnls_micros` value (an `int` subclass) raises `TypeError`.

    A `True` must never be silently counted as a profitable `1`-micro trade nor
    summed into total PnL, mirroring the `research_cost_micros` bool guard.
    """
    from windbreak.evaluation.costs import aggregate_research_costs

    records = (
        _forecast_record(
            forecast_id="fc-a", market_ticker="MKT-A", research_cost_micros=10
        ),
    )
    trade_pnls_micros: Mapping[str, int] = {"MKT-A": cast("int", True)}

    with pytest.raises(TypeError, match="trade_pnls_micros"):
        aggregate_research_costs(
            records, resolutions={}, trade_pnls_micros=trade_pnls_micros
        )


def test_aggregate_research_costs_of_no_records_is_all_zero_and_none() -> None:
    """An empty record sequence yields a zeroed, all-`None` `CostMeter`."""
    from windbreak.evaluation.costs import aggregate_research_costs

    meter = aggregate_research_costs((), resolutions={}, trade_pnls_micros={})

    assert meter.total_research_cost_micros == 0
    assert meter.resolved_forecast_count == 0
    assert meter.profitable_trade_count == 0
    assert meter.trade_count == 0
    assert meter.cost_per_resolved_forecast_micros is None
    assert meter.cost_per_profitable_trade_micros is None
    assert meter.cost_adjusted_expectancy_micros is None
