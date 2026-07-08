"""The real §16 :class:`Screener`: filter aggregation and decision ledgering.

:class:`Screener` runs the four :mod:`windbreak.screener.filters` filters against
an injected :class:`~windbreak.config.ScreenerConfig` and clock, aggregates their
verdicts into a :class:`ScreenResult`, and appends exactly one
``SCREEN_DECISION`` event -- with per-filter ``passed``/``measured`` detail --
to the injected event ledger per :meth:`Screener.screen` call. At construction,
one ``LEGAL_RISK_ACK`` event is emitted per supplied
:class:`LegalRiskAcknowledgement`, and those acknowledged categories lift the
fail-closed block on the legally-risky categories.

Every ledgered value is a plain ``int``, string, bool, list, or dict -- never a
float -- keeping this package clean under ``scripts/lint_no_floats.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from windbreak.connector.snapshot import (
    SCREEN_DECISION_EVENT,
    ConnectorEvent,
    utc_now_iso,
)
from windbreak.screener.filters import (
    CATEGORY_BLOCKLIST,
    HORIZON_DAYS,
    MIN_DEPTH,
    MIN_VOLUME_24H,
    category_filter,
    horizon_filter,
    min_depth_filter,
    min_volume_filter,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from windbreak.config import ScreenerConfig
    from windbreak.connector.models import NormalizedMarket
    from windbreak.connector.snapshot import EventLedgerWriter
    from windbreak.screener.filters import BookStats, FilterResult

#: Event type recorded once per acknowledged legally-risky category.
LEGAL_RISK_ACK_EVENT: Final = "LEGAL_RISK_ACK"

#: The order the four filters run in and that ``blocked_by`` respects.
_CANONICAL_ORDER: Final = (CATEGORY_BLOCKLIST, MIN_VOLUME_24H, MIN_DEPTH, HORIZON_DAYS)


@dataclass(frozen=True, slots=True)
class LegalRiskAcknowledgement:
    """An operator's explicit acceptance of a legally-risky category.

    Attributes:
        category: The legally-risky category being acknowledged.
        reason: The operator's justification for accepting the risk.
    """

    category: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """The aggregate outcome of screening one market.

    Attributes:
        ticker: The screened market's ticker.
        eligible: Whether every filter passed.
        blocked_by: The failing filter names, in canonical order.
        filters: Each canonical filter name mapped to its
            :class:`~windbreak.screener.filters.FilterResult`.
    """

    ticker: str
    eligible: bool
    blocked_by: tuple[str, ...]
    filters: Mapping[str, FilterResult]


class Screener:
    """Aggregate the §16 filters and ledger a decision per screened market."""

    def __init__(
        self,
        config: ScreenerConfig,
        writer: EventLedgerWriter,
        *,
        clock: Callable[[], datetime],
        acknowledgements: tuple[LegalRiskAcknowledgement, ...] = (),
    ) -> None:
        """Initialize the screener and emit one ack event per acknowledgement.

        Args:
            config: The screening thresholds and blocklist to enforce.
            writer: The event-ledger writer that records each event.
            clock: A zero-argument callable returning the current ``datetime``.
            acknowledgements: Operator acknowledgements of legally-risky
                categories; each emits one ``LEGAL_RISK_ACK`` event and lifts
                that category's fail-closed block.
        """
        self._config = config
        self._writer = writer
        self._clock = clock
        self._acknowledged_categories = tuple(ack.category for ack in acknowledgements)
        self._emit_acknowledgements(acknowledgements)

    def _emit_acknowledgements(
        self, acknowledgements: tuple[LegalRiskAcknowledgement, ...]
    ) -> None:
        """Record one ``LEGAL_RISK_ACK`` event per acknowledgement, in order.

        Args:
            acknowledgements: The operator acknowledgements to ledger.
        """
        for ack in acknowledgements:
            event = ConnectorEvent(
                event_type=LEGAL_RISK_ACK_EVENT,
                payload={"category": ack.category, "reason": ack.reason},
                ts=utc_now_iso(self._clock()),
            )
            self._writer.record(event)

    def screen(self, market: NormalizedMarket, stats: BookStats) -> ScreenResult:
        """Screen a market, ledger the decision, and return the result.

        Runs the four filters in canonical order, aggregates their verdicts,
        appends exactly one ``SCREEN_DECISION`` event, and returns the
        :class:`ScreenResult`.

        Args:
            market: The market to screen.
            stats: The market's order-book statistics.

        Returns:
            The aggregate screening result.
        """
        now = self._clock()
        results = self._run_filters(market, stats, now=now)
        blocked_by = tuple(
            name for name in _CANONICAL_ORDER if not results[name].passed
        )
        eligible = not blocked_by
        self._record_decision(
            market, eligible=eligible, blocked_by=blocked_by, results=results, now=now
        )
        return ScreenResult(
            ticker=market.ticker,
            eligible=eligible,
            blocked_by=blocked_by,
            filters=results,
        )

    def _run_filters(
        self, market: NormalizedMarket, stats: BookStats, *, now: datetime
    ) -> dict[str, FilterResult]:
        """Run every filter and return the results keyed in canonical order.

        Args:
            market: The market to screen.
            stats: The market's order-book statistics.
            now: The reference instant used for the horizon measurement.

        Returns:
            Each canonical filter name mapped to its ``FilterResult``.
        """
        config = self._config
        return {
            CATEGORY_BLOCKLIST: category_filter(
                market,
                blocklist=config.category_blocklist,
                acknowledged_categories=self._acknowledged_categories,
            ),
            MIN_VOLUME_24H: min_volume_filter(
                stats, threshold_micros=config.min_volume_24h_micros
            ),
            MIN_DEPTH: min_depth_filter(
                stats, threshold_centis=config.min_depth_contract_centis
            ),
            HORIZON_DAYS: horizon_filter(
                market,
                now=now,
                min_days=config.horizon_days.min,
                max_days=config.horizon_days.max,
            ),
        }

    def _record_decision(
        self,
        market: NormalizedMarket,
        *,
        eligible: bool,
        blocked_by: tuple[str, ...],
        results: Mapping[str, FilterResult],
        now: datetime,
    ) -> None:
        """Append one JSON-safe ``SCREEN_DECISION`` event for a screened market.

        The ledgered ``blocked_by`` is the exact tuple already computed by
        :meth:`screen`, so the audit trail can never diverge from the returned
        :class:`ScreenResult`.

        Args:
            market: The screened market.
            eligible: Whether every filter passed.
            blocked_by: The failing filter names in canonical order.
            results: The per-filter results to serialize.
            now: The reference instant the decision was made at (the same one
                used to measure the horizon), stamped as the event timestamp.
        """
        filters_payload: dict[str, dict[str, object]] = {
            name: {"passed": result.passed, "measured": result.measured}
            for name, result in results.items()
        }
        payload: dict[str, object] = {
            "ticker": market.ticker,
            "eligible": eligible,
            "blocked_by": list(blocked_by),
            "filters": filters_payload,
        }
        event = ConnectorEvent(
            event_type=SCREEN_DECISION_EVENT,
            payload=payload,
            ts=utc_now_iso(now),
        )
        self._writer.record(event)
