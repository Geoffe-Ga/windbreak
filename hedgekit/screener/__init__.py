"""Shared component: market eligibility screening.

This package screens normalized markets for tradeability. It carries no
credentials and touches only public market metadata (SPEC S5.2). For issue #16
it ships a single :class:`StubScreener` whose only rule is jurisdiction: a
market whose ``jurisdiction_status`` is not ``"eligible"`` is blocked. The full
filter suite (liquidity, fees, resolution quality, and more) is issue #6's job.
Screening decisions derive from money-path values, so this package is guarded
against floats by ``scripts/lint_no_floats.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Literal

    from hedgekit.connector.models import NormalizedMarket

#: The jurisdiction verdict that permits trading a market.
_ELIGIBLE: str = "eligible"


@dataclass(frozen=True, slots=True)
class ScreenDecision:
    """The outcome of screening a single market.

    Attributes:
        ticker: The screened market's ticker.
        decision: Whether the market is ``"eligible"`` or ``"blocked"``.
        reason: Human-readable justification for the decision.
    """

    ticker: str
    decision: Literal["eligible", "blocked"]
    reason: str


class StubScreener:
    """A minimal screener that blocks only on non-eligible jurisdiction.

    Real filters (liquidity, fees, resolution quality) arrive in issue #6; this
    stub exists so the snapshot pipeline has an end-to-end verdict to record.
    """

    def screen(self, market: NormalizedMarket) -> ScreenDecision:
        """Screen a market on jurisdiction alone.

        Args:
            market: The market to screen.

        Returns:
            A ``"blocked"`` decision (with a jurisdiction-referencing reason)
            when the market's jurisdiction is not eligible, else an
            ``"eligible"`` decision.
        """
        if market.jurisdiction_status != _ELIGIBLE:
            return ScreenDecision(
                ticker=market.ticker,
                decision="blocked",
                reason=(
                    f"jurisdiction status is {market.jurisdiction_status!r}, "
                    "not eligible"
                ),
            )
        return ScreenDecision(
            ticker=market.ticker,
            decision="eligible",
            reason="jurisdiction eligible",
        )


__all__ = ["ScreenDecision", "StubScreener"]
