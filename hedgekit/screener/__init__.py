"""Shared component: market eligibility screening.

This package screens normalized markets for tradeability. It carries no
credentials and touches only public market metadata (SPEC S5.2). Its centrepiece
is the real §16 :class:`Screener`, which runs the four pure
:mod:`hedgekit.screener.filters` filters (category blocklist plus the
fail-closed legally-risky-category path, 24h-volume floor, book-depth floor, and
whole-day resolution-horizon window) against a
:class:`~hedgekit.config.ScreenerConfig`, and ledgers exactly one
``SCREEN_DECISION`` event per market carrying each filter's ``passed`` verdict
and ``measured`` quantity. Legally-risky categories (e.g. ``sports``) fail closed
until an operator supplies a :class:`LegalRiskAcknowledgement`, which also emits
a ``LEGAL_RISK_ACK`` event.

:class:`StubScreener` and :class:`ScreenDecision` remain exported for the
snapshot task until the live wiring lands (a follow-up: no 24h-volume source
feeds :class:`~hedgekit.screener.filters.BookStats` yet). Screening decisions
derive from money-path values, so this package is guarded against floats by
``scripts/lint_no_floats.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hedgekit.screener.filters import (
    CATEGORY_BLOCKLIST,
    HORIZON_DAYS,
    LEGALLY_RISKY_CATEGORIES,
    MIN_DEPTH,
    MIN_VOLUME_24H,
    BookStats,
    FilterResult,
    category_filter,
    horizon_filter,
    min_depth_filter,
    min_volume_filter,
)
from hedgekit.screener.screener import (
    LEGAL_RISK_ACK_EVENT,
    LegalRiskAcknowledgement,
    Screener,
    ScreenResult,
)

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


__all__ = [
    "CATEGORY_BLOCKLIST",
    "HORIZON_DAYS",
    "LEGALLY_RISKY_CATEGORIES",
    "LEGAL_RISK_ACK_EVENT",
    "MIN_DEPTH",
    "MIN_VOLUME_24H",
    "BookStats",
    "FilterResult",
    "LegalRiskAcknowledgement",
    "ScreenDecision",
    "ScreenResult",
    "Screener",
    "StubScreener",
    "category_filter",
    "horizon_filter",
    "min_depth_filter",
    "min_volume_filter",
]
