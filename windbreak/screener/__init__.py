"""Shared component: market eligibility screening.

This package screens normalized markets for tradeability. It carries no
credentials and touches only public market metadata (SPEC S5.2). Its centrepiece
is the real §16 :class:`Screener`, which runs the four pure
:mod:`windbreak.screener.filters` filters (category blocklist plus the
fail-closed legally-risky-category path, 24h-volume floor, book-depth floor, and
whole-day resolution-horizon window) against a
:class:`~windbreak.config.ScreenerConfig`, and ledgers exactly one
``SCREEN_DECISION`` event per market carrying each filter's ``passed`` verdict
and ``measured`` quantity. Legally-risky categories (e.g. ``sports``) fail closed
until an operator supplies a :class:`LegalRiskAcknowledgement`, which also emits
a ``LEGAL_RISK_ACK`` event.

Screening decisions derive from money-path values, so this package is guarded
against floats by ``scripts/lint_no_floats.py``.
"""

from __future__ import annotations

from windbreak.screener.filters import (
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
from windbreak.screener.screener import (
    LEGAL_RISK_ACK_EVENT,
    LegalRiskAcknowledgement,
    Screener,
    ScreenResult,
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
    "ScreenResult",
    "Screener",
    "category_filter",
    "horizon_filter",
    "min_depth_filter",
    "min_volume_filter",
]
