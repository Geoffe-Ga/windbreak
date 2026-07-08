"""Research-cost meter for the evaluation harness (issue #55).

A forecast costs real money to produce (LLM calls, retrieval, verification), and
that spend must be weighed against what the forecasts earned. This module
aggregates the per-forecast ``research_cost_micros`` into a :class:`CostMeter`
that normalizes total spend against three denominators -- resolved forecasts,
profitable trades, and all trades -- with the house's conservative rounding: a
per-unit *cost* is ceiling-rounded (``OVERSTATE_COST``, never understate a cost)
and the cost-adjusted *expectancy* is floor-rounded (``UNDERSTATE_EQUITY``, never
overstate an equity-side figure). Each denominator-derived field is ``None``
exactly when its own denominator count is ``0``.

Every value stays on the integer money path: costs and PnL are micros ``int``s
and the derived per-unit figures are :class:`~hedgekit.numeric.types.MoneyMicros`,
so no float ever appears. ``research_cost_micros`` is validated here -- rejecting
a negative cost and a ``bool`` masquerading as an ``int`` -- because
:class:`~hedgekit.forecast.records.ForecastRecord` does not itself guard that
field; each ``trade_pnls_micros`` value is likewise ``bool``-guarded (a signed
PnL has no sign floor, but a ``True`` must never count as a profitable trade).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hedgekit.numeric.rounding import RoundingDirection, divide
from hedgekit.numeric.types import MoneyMicros

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from hedgekit.evaluation.resolution import ResolutionOutcome
    from hedgekit.forecast.records import ForecastRecord

#: The exclusive lower bound below which a research cost is illegal (negative).
_MIN_RESEARCH_COST_MICROS = 0


@dataclass(frozen=True, slots=True)
class CostMeter:
    """Aggregated research spend normalized against three denominators.

    Attributes:
        total_research_cost_micros: Sum of ``research_cost_micros`` over every
            record, both ``triage_only`` and ``full``, in micros.
        resolved_forecast_count: Number of records whose market resolved.
        profitable_trade_count: Number of trades with strictly positive PnL.
        trade_count: Number of trades taken.
        cost_per_resolved_forecast_micros: Ceiling of total cost over resolved
            forecasts, or ``None`` when none resolved.
        cost_per_profitable_trade_micros: Ceiling of total cost over profitable
            trades, or ``None`` when none were profitable.
        cost_adjusted_expectancy_micros: Floor of net (PnL minus cost) over all
            trades, or ``None`` when no trades were taken.
    """

    total_research_cost_micros: int
    resolved_forecast_count: int
    profitable_trade_count: int
    trade_count: int
    cost_per_resolved_forecast_micros: MoneyMicros | None
    cost_per_profitable_trade_micros: MoneyMicros | None
    cost_adjusted_expectancy_micros: MoneyMicros | None


def _validated_cost(record: ForecastRecord) -> int:
    """Return one record's research cost after guarding its type and sign.

    Args:
        record: The forecast record whose ``research_cost_micros`` is validated.

    Returns:
        The record's non-negative ``research_cost_micros``.

    Raises:
        TypeError: If ``research_cost_micros`` is a ``bool`` (an ``int`` subclass
            that must not masquerade as a cost); the message names the field.
        ValueError: If ``research_cost_micros`` is negative; the message names
            the field.
    """
    cost = record.research_cost_micros
    if isinstance(cost, bool):
        raise TypeError(
            f"research_cost_micros requires a non-bool int, got {type(cost).__name__}"
        )
    if cost < _MIN_RESEARCH_COST_MICROS:
        raise ValueError(f"research_cost_micros must be non-negative, got {cost}")
    return cost


def _validated_pnl(pnl: int) -> int:
    """Return one trade's PnL after rejecting a ``bool`` masquerading as an int.

    A signed PnL has no sign floor (a losing trade is legitimately negative), so
    this guards only the type -- mirroring :func:`_validated_cost`'s ``bool``
    check -- because a ``True`` must never be silently counted as a profitable
    ``1``-micro trade nor summed into total PnL.

    Args:
        pnl: The realized trade PnL, in micros.

    Returns:
        The PnL unchanged when it is a genuine (non-``bool``) ``int``.

    Raises:
        TypeError: If ``pnl`` is a ``bool`` (an ``int`` subclass that must not
            masquerade as a money figure); the message names the field.
    """
    if isinstance(pnl, bool):
        raise TypeError(
            f"trade_pnls_micros requires non-bool ints, got {type(pnl).__name__}"
        )
    return pnl


def _cost_per_unit(total_cost_micros: int, count: int) -> MoneyMicros | None:
    """Return the ceiling per-unit cost, or ``None`` for a zero denominator.

    Args:
        total_cost_micros: The total cost to spread, in micros.
        count: The denominator count; ``0`` yields ``None``.

    Returns:
        ``ceil(total_cost / count)`` as :class:`MoneyMicros` (``OVERSTATE_COST``),
        or ``None`` when ``count`` is ``0``.
    """
    if count == 0:
        return None
    return MoneyMicros(
        divide(total_cost_micros, count, rounding=RoundingDirection.OVERSTATE_COST)
    )


def _cost_adjusted_expectancy(
    total_pnl_micros: int, total_cost_micros: int, trade_count: int
) -> MoneyMicros | None:
    """Return the floor cost-adjusted expectancy, or ``None`` for zero trades.

    Args:
        total_pnl_micros: Sum of every trade's PnL, in micros.
        total_cost_micros: Total research cost, in micros.
        trade_count: The number of trades; ``0`` yields ``None``.

    Returns:
        ``floor((total_pnl - total_cost) / trade_count)`` as :class:`MoneyMicros`
        (``UNDERSTATE_EQUITY``), or ``None`` when ``trade_count`` is ``0``.
    """
    if trade_count == 0:
        return None
    return MoneyMicros(
        divide(
            total_pnl_micros - total_cost_micros,
            trade_count,
            rounding=RoundingDirection.UNDERSTATE_EQUITY,
        )
    )


def aggregate_research_costs(
    records: Sequence[ForecastRecord],
    *,
    resolutions: Mapping[str, ResolutionOutcome],
    trade_pnls_micros: Mapping[str, int],
) -> CostMeter:
    """Aggregate per-forecast research spend into a :class:`CostMeter`.

    The join key from a record to both mappings is its ``market_ticker`` (as with
    :class:`~hedgekit.evaluation.registry.EvaluationInputs.resolutions`). Trade
    figures come purely from ``trade_pnls_micros``: one trade per entry, a
    profitable trade per strictly-positive value.

    Args:
        records: The forecast records to aggregate; every ``research_cost_micros``
            counts, ``triage_only`` and ``full`` alike.
        resolutions: Ground-truth outcomes keyed by ``market_ticker``; a record
            is resolved iff its ticker is present.
        trade_pnls_micros: Realized trade PnLs, in micros, keyed by
            ``market_ticker``.

    Returns:
        The assembled :class:`CostMeter`; each per-unit field is ``None`` exactly
        when its denominator count is ``0``.

    Raises:
        TypeError: If any ``research_cost_micros`` or any ``trade_pnls_micros``
            value is a ``bool``.
        ValueError: If any ``research_cost_micros`` is negative.
    """
    total_cost = sum(_validated_cost(record) for record in records)
    resolved_count = sum(1 for record in records if record.market_ticker in resolutions)
    pnls = [_validated_pnl(pnl) for pnl in trade_pnls_micros.values()]
    trade_count = len(pnls)
    profitable_count = sum(1 for pnl in pnls if pnl > 0)
    total_pnl = sum(pnls)
    return CostMeter(
        total_research_cost_micros=total_cost,
        resolved_forecast_count=resolved_count,
        profitable_trade_count=profitable_count,
        trade_count=trade_count,
        cost_per_resolved_forecast_micros=_cost_per_unit(total_cost, resolved_count),
        cost_per_profitable_trade_micros=_cost_per_unit(total_cost, profitable_count),
        cost_adjusted_expectancy_micros=_cost_adjusted_expectancy(
            total_pnl, total_cost, trade_count
        ),
    )
