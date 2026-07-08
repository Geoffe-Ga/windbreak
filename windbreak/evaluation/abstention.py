"""Counterfactual abstention-wisdom scoring (SPEC-EPIC_07, #53).

When the strategy skips a forecast it *could* have traded, was that wise? This
module answers counterfactually: it reconstructs the trade the forecast's own
edge implied, prices it against the market's realised outcome, and calls the
abstention ``WISE`` when that phantom trade would have lost money (or broken
even) and ``UNWISE`` when it would have profited. The aggregate
:class:`AbstentionSummary` reports how much profit the strategy left on the
table (``forgone_pnl_pips``) by summing only the *positive* counterfactual PnLs.

Every value is exact integer pip arithmetic -- no floats on the money path. The
full binary payout (:data:`~windbreak.evaluation.metrics.PAYOUT_PIPS`) and the
ppm-per-pip conversion (:data:`~windbreak.evaluation.metrics.BASELINE_PPM_PER_PIP`)
are imported from :mod:`windbreak.evaluation.metrics` rather than re-declared, and
:class:`~windbreak.evaluation.resolution.ResolutionOutcome` is the one other
runtime import; registry types are referenced only under
:data:`typing.TYPE_CHECKING`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from windbreak.evaluation.metrics import BASELINE_PPM_PER_PIP, PAYOUT_PIPS
from windbreak.evaluation.resolution import ResolutionOutcome

if TYPE_CHECKING:
    from windbreak.evaluation.registry import EvaluationInputs, FixtureForecast

#: PnL at or below which a counterfactual trade is judged a wise abstention: a
#: phantom trade that would have lost money or broken even.
_WISE_PNL_CEILING_PIPS = 0


class AbstentionVerdict(enum.Enum):
    """Whether skipping a forecast was, counterfactually, the right call.

    ``WISE`` when the implied trade would have lost money or broken even
    (counterfactual PnL ``<= 0``); ``UNWISE`` when it would have profited.
    """

    WISE = "wise"
    UNWISE = "unwise"


@dataclass(frozen=True, slots=True)
class AbstentionScore:
    """One skipped forecast's counterfactual trade outcome.

    Attributes:
        forecast_id: Stable identifier of the abstained forecast.
        market_ticker: The market the forecast named.
        abstention_reason: Why the forecast was skipped.
        counterfactual_pnl_pips: The PnL, in pips, of the trade the forecast's
            edge implied, priced against the realised outcome (may be negative).
        verdict: The :class:`AbstentionVerdict` for this abstention.
    """

    forecast_id: str
    market_ticker: str
    abstention_reason: str
    counterfactual_pnl_pips: int
    verdict: AbstentionVerdict


@dataclass(frozen=True, slots=True)
class AbstentionSummary:
    """Aggregate wisdom of a run's abstentions.

    Attributes:
        total: Number of scored abstentions.
        wise_count: How many abstentions were :attr:`AbstentionVerdict.WISE`.
        unwise_count: How many were :attr:`AbstentionVerdict.UNWISE`.
        forgone_pnl_pips: Sum of the strictly-positive counterfactual PnLs -- the
            profit skipped by the ``UNWISE`` abstentions, in pips.
    """

    total: int
    wise_count: int
    unwise_count: int
    forgone_pnl_pips: int


def _counterfactual_pnl_pips(
    forecast: FixtureForecast, outcome: ResolutionOutcome
) -> int:
    """Return the PnL, in pips, of the trade the forecast's edge implied.

    The implied direction is ``LONG_YES`` when the forecast's probability
    exceeds the executable-price baseline, ``LONG_NO`` when it is below, and no
    trade at all when they are equal (PnL exactly ``0``). A ``LONG_YES`` buys
    one yes contract at the ask (``baseline_pips``) and collects the full payout
    on a ``YES`` outcome; a ``LONG_NO`` buys one no contract at
    ``PAYOUT_PIPS - baseline_pips`` and collects the full payout on a ``NO``.

    Args:
        forecast: The abstained forecast whose implied trade is priced.
        outcome: The realised outcome of the forecast's market.

    Returns:
        The counterfactual PnL, in pips (positive means the trade would profit).
    """
    baseline_pips = forecast.baseline_executable_price_pips
    baseline_ppm = baseline_pips * BASELINE_PPM_PER_PIP
    probability_ppm = forecast.probability_ppm.value
    outcome_is_yes = outcome is ResolutionOutcome.YES
    if probability_ppm > baseline_ppm:
        payout = PAYOUT_PIPS if outcome_is_yes else 0
        return payout - baseline_pips
    if probability_ppm < baseline_ppm:
        payout = 0 if outcome_is_yes else PAYOUT_PIPS
        return payout - (PAYOUT_PIPS - baseline_pips)
    return 0


def _verdict(pnl_pips: int) -> AbstentionVerdict:
    """Return the wisdom verdict for a counterfactual PnL.

    Args:
        pnl_pips: The counterfactual trade PnL, in pips.

    Returns:
        :attr:`AbstentionVerdict.WISE` iff ``pnl_pips`` is at or below the wise
        ceiling, else :attr:`AbstentionVerdict.UNWISE`.
    """
    if pnl_pips <= _WISE_PNL_CEILING_PIPS:
        return AbstentionVerdict.WISE
    return AbstentionVerdict.UNWISE


def score_abstentions(inputs: EvaluationInputs) -> tuple[AbstentionScore, ...]:
    """Score every scoreable abstention in ``inputs``, in fixture order.

    Only records that are untraded, carry an ``abstention_reason``, and name a
    resolved market are scored: traded and unresolved records never enter.

    Args:
        inputs: The admitted evaluation inputs to score.

    Returns:
        One :class:`AbstentionScore` per scoreable abstention, in fixture order.
    """
    scores: list[AbstentionScore] = []
    for forecast in inputs.forecasts:
        reason = forecast.abstention_reason
        if forecast.traded or reason is None:
            continue
        outcome = inputs.resolutions.get(forecast.market_ticker)
        if outcome is None:
            continue
        pnl = _counterfactual_pnl_pips(forecast, outcome)
        scores.append(
            AbstentionScore(
                forecast_id=forecast.forecast_id,
                market_ticker=forecast.market_ticker,
                abstention_reason=reason,
                counterfactual_pnl_pips=pnl,
                verdict=_verdict(pnl),
            )
        )
    return tuple(scores)


def summarize_abstentions(
    source: EvaluationInputs | tuple[AbstentionScore, ...],
) -> AbstentionSummary:
    """Summarise abstentions from raw inputs or precomputed scores.

    Args:
        source: Either the admitted :class:`EvaluationInputs` (scored here) or an
            already-computed tuple of :class:`AbstentionScore`.

    Returns:
        The aggregate :class:`AbstentionSummary`; ``forgone_pnl_pips`` sums only
        the strictly-positive counterfactual PnLs.
    """
    scores = source if isinstance(source, tuple) else score_abstentions(source)
    wise_count = 0
    unwise_count = 0
    forgone_pnl_pips = 0
    for score in scores:
        if score.verdict is AbstentionVerdict.WISE:
            wise_count += 1
        else:
            unwise_count += 1
        if score.counterfactual_pnl_pips > 0:
            forgone_pnl_pips += score.counterfactual_pnl_pips
    return AbstentionSummary(
        total=len(scores),
        wise_count=wise_count,
        unwise_count=unwise_count,
        forgone_pnl_pips=forgone_pnl_pips,
    )
