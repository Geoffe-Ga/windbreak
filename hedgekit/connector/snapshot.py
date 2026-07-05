"""The market-snapshot task and its event-ledger persistence seam.

:class:`MarketSnapshotTask` walks every market a connector offers and records,
per market, a ``MARKET_SNAPSHOT`` event (the market projected via
:func:`~hedgekit.connector.models.market_to_payload`) and a ``SCREEN_DECISION``
event (the screener's eligible/blocked verdict). Events flow through the
:class:`EventLedgerWriter` protocol -- a dependency-injection seam with no
``hedgekit.ledger`` dependency, mirroring
:class:`hedgekit.alerts.dispatch.LedgerWriter`. A broken writer is isolated:
its failure is logged and swallowed so one bad record can never abort a run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from hedgekit.connector.models import market_to_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hedgekit.connector.interface import MarketConnector
    from hedgekit.connector.models import NormalizedMarket
    from hedgekit.screener import ScreenDecision

#: Event type recorded for each market's normalized snapshot.
MARKET_SNAPSHOT_EVENT: Final = "MARKET_SNAPSHOT"

#: Event type recorded for each market's screening verdict.
SCREEN_DECISION_EVENT: Final = "SCREEN_DECISION"

_LOGGER = logging.getLogger("hedgekit.connector")


def _utc_now_iso() -> str:
    """Return the current UTC time as ISO-8601 with a trailing ``Z``.

    Returns:
        A string like ``2026-07-04T12:00:00.000000Z``.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


@dataclass(frozen=True, slots=True)
class ConnectorEvent:
    """One recorded connector event.

    Attributes:
        event_type: The event kind (e.g. ``MARKET_SNAPSHOT``).
        payload: The JSON-safe event body.
        ts: ISO-8601 UTC timestamp of when the event was created.
    """

    event_type: str
    payload: Mapping[str, object]
    ts: str


class EventLedgerWriter(Protocol):
    """The seam through which a connector event is persisted."""

    def record(self, event: ConnectorEvent) -> None:
        """Persist a connector event.

        Args:
            event: The event to persist.
        """
        ...


class MarketScreener(Protocol):
    """The minimal screening contract :class:`MarketSnapshotTask` depends on."""

    def screen(self, market: NormalizedMarket) -> ScreenDecision:
        """Return the eligibility verdict for a market.

        Args:
            market: The market to screen.

        Returns:
            The screening decision.
        """
        ...


class LoggingEventLedgerWriter:
    """An :class:`EventLedgerWriter` that logs events instead of persisting them.

    Stands in until a real ledger provides a persisting writer; it emits on the
    module ``hedgekit.connector`` logger with the event type in the message so
    operators (and the snapshot wiring) can see each event.
    """

    def record(self, event: ConnectorEvent) -> None:
        """Log a connector event as a single structured line.

        Args:
            event: The event to log.
        """
        _LOGGER.info(
            "connector event recorded event_type=%s ts=%s",
            event.event_type,
            event.ts,
            extra={
                "component": "connector",
                "event_type": event.event_type,
                "ts": event.ts,
            },
        )


class InMemoryEventLedgerWriter:
    """An :class:`EventLedgerWriter` that retains events in memory for tests."""

    def __init__(self) -> None:
        """Initialize with an empty event log."""
        self._events: list[ConnectorEvent] = []

    def record(self, event: ConnectorEvent) -> None:
        """Append a connector event to the in-memory log.

        Args:
            event: The event to retain.
        """
        self._events.append(event)

    def events_by_type(self, event_type: str) -> tuple[ConnectorEvent, ...]:
        """Return every retained event of a given type, in record order.

        Args:
            event_type: The event kind to filter by.

        Returns:
            The matching events.
        """
        return tuple(event for event in self._events if event.event_type == event_type)


class MarketSnapshotTask:
    """Record a snapshot and screening decision for every offered market."""

    def __init__(
        self,
        connector: MarketConnector,
        screener: MarketScreener,
        writer: EventLedgerWriter,
    ) -> None:
        """Initialize the task.

        Args:
            connector: The source of markets to snapshot.
            screener: The screener producing each market's verdict.
            writer: The event-ledger writer that records each event.
        """
        self._connector = connector
        self._screener = screener
        self._writer = writer

    def run_once(self) -> None:
        """Record a snapshot and a screening decision for each market."""
        for market in self._connector.list_markets():
            self._record_snapshot(market)
            self._record_decision(market)

    def _record_snapshot(self, market: NormalizedMarket) -> None:
        """Record one market's normalized snapshot event.

        Args:
            market: The market to snapshot.
        """
        event = ConnectorEvent(
            event_type=MARKET_SNAPSHOT_EVENT,
            payload=market_to_payload(market),
            ts=_utc_now_iso(),
        )
        self._record(event)

    def _record_decision(self, market: NormalizedMarket) -> None:
        """Record one market's screening-decision event.

        Args:
            market: The market to screen and record.
        """
        decision = self._screener.screen(market)
        event = ConnectorEvent(
            event_type=SCREEN_DECISION_EVENT,
            payload={
                "ticker": decision.ticker,
                "decision": decision.decision,
                "reason": decision.reason,
            },
            ts=_utc_now_iso(),
        )
        self._record(event)

    def _record(self, event: ConnectorEvent) -> None:
        """Record an event via the writer, isolating any failure.

        Args:
            event: The event to record. A raising writer is logged and
                swallowed so one bad record never aborts the run.
        """
        try:
            self._writer.record(event)
        except Exception as exc:
            _LOGGER.warning(
                "event ledger writer failed to record %s event: %s",
                event.event_type,
                exc,
                extra={"component": "connector"},
            )
