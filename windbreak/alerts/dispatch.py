"""The alert dispatcher: fan one alert out to many sinks, isolating failures.

:class:`AlertDispatcher` sends an alert to every configured sink, converting
each sink's success or failure into a :class:`SinkOutcome`. A broken sink can
never take down another sink, the caller, or the ledger writer. When no sink
succeeds (including the empty-sink-list edge case), a fallback sink -- a
:class:`~windbreak.alerts.sinks.LogOnlySink` by default -- fires so an alert is
never silently lost.

Ledger persistence of the resulting :class:`AlertEmitted` (issue #13) is wired
through the :class:`LedgerWriter` protocol; this module ships a
:class:`LoggingLedgerWriter` that only logs, with no ``windbreak.ledger``
dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from windbreak.alerts.registry import get_registration
from windbreak.alerts.sinks import LogOnlySink

if TYPE_CHECKING:
    from collections.abc import Sequence

    from windbreak.alerts.registry import AlertSeverity, AlertType
    from windbreak.alerts.sinks import AlertSink

_LOGGER = logging.getLogger("windbreak.alerts")


@dataclass(frozen=True)
class SinkOutcome:
    """The result of attempting to deliver an alert through one sink.

    Attributes:
        sink: The sink's ``name``.
        ok: Whether delivery succeeded.
        detail: Failure detail when ``ok`` is False, else None.
    """

    sink: str
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class AlertEmitted:
    """A record of one dispatched alert and every sink's outcome.

    Attributes:
        alert_type: The dispatched alert type.
        severity: The alert's severity.
        message: The alert body.
        outcomes: One outcome per attempted sink, in order.
        ts: ISO-8601 UTC timestamp of dispatch.
    """

    alert_type: AlertType
    severity: AlertSeverity
    message: str
    outcomes: tuple[SinkOutcome, ...]
    ts: str


class LedgerWriter(Protocol):
    """The seam through which an emitted alert is persisted (issue #13)."""

    def record(self, event: AlertEmitted) -> None:
        """Persist an emitted-alert event.

        Args:
            event: The event to persist.
        """
        ...


class LoggingLedgerWriter:
    """A :class:`LedgerWriter` that logs events instead of persisting them.

    Stands in until the real ledger (issue #13) provides a persisting
    :class:`LedgerWriter`; it emits on the module ``windbreak.alerts`` logger.
    """

    def record(self, event: AlertEmitted) -> None:
        """Log the emitted-alert event as a single structured line.

        Args:
            event: The event to log.
        """
        summary = ", ".join(
            f"{outcome.sink}=ok:{outcome.ok}" for outcome in event.outcomes
        )
        _LOGGER.info(
            "alert emitted type=%s severity=%s message=%s",
            event.alert_type.value,
            event.severity.value,
            event.message,
            extra={
                "component": "alerts",
                "event": "AlertEmitted",
                "alert_type": event.alert_type.value,
                "severity": event.severity.value,
                "outcomes": summary,
            },
        )


def _utc_now_iso() -> str:
    """Return the current UTC time as ISO-8601 with a trailing ``Z``.

    Returns:
        A string like ``2026-07-04T12:00:00.000000Z``.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class AlertDispatcher:
    """Fan an alert out to every sink, isolating and recording each outcome."""

    def __init__(
        self,
        sinks: Sequence[AlertSink],
        *,
        ledger_writer: LedgerWriter,
        fallback: AlertSink | None = None,
    ) -> None:
        """Initialize the dispatcher.

        Args:
            sinks: The sinks to attempt for each alert, in order.
            ledger_writer: The writer that records each emitted event.
            fallback: The sink to fire when no primary sink succeeds.
                Defaults to a :class:`~windbreak.alerts.sinks.LogOnlySink`.
        """
        self._sinks = sinks
        self._ledger_writer = ledger_writer
        self._fallback: AlertSink = fallback if fallback is not None else LogOnlySink()

    def dispatch(self, alert_type: AlertType, message: str) -> AlertEmitted:
        """Send an alert to every sink, firing the fallback if none succeed.

        Args:
            alert_type: The alert type to dispatch.
            message: The alert body.

        Returns:
            The :class:`AlertEmitted` event describing every sink outcome.
        """
        severity = get_registration(alert_type).severity
        outcomes = [
            self._attempt(sink, alert_type, severity, message) for sink in self._sinks
        ]
        if not any(outcome.ok for outcome in outcomes):
            outcomes.append(
                self._attempt(self._fallback, alert_type, severity, message)
            )
        event = AlertEmitted(
            alert_type=alert_type,
            severity=severity,
            message=message,
            outcomes=tuple(outcomes),
            ts=_utc_now_iso(),
        )
        self._record(event)
        return event

    def _attempt(
        self,
        sink: AlertSink,
        alert_type: AlertType,
        severity: AlertSeverity,
        message: str,
    ) -> SinkOutcome:
        """Attempt one sink, converting any exception into a failed outcome.

        Args:
            sink: The sink to send through.
            alert_type: The alert type to dispatch.
            severity: The alert's severity.
            message: The alert body.

        Returns:
            An ok :class:`SinkOutcome` on success, or a failed one (carrying
            the exception detail) when the sink raises.
        """
        try:
            sink.send(alert_type, severity, message)
        except Exception as exc:
            _LOGGER.warning(
                "alert sink %r failed: %s",
                sink.name,
                exc,
                extra={"component": "alerts", "sink": sink.name},
            )
            return SinkOutcome(sink=sink.name, ok=False, detail=str(exc))
        return SinkOutcome(sink=sink.name, ok=True)

    def _record(self, event: AlertEmitted) -> None:
        """Record an event via the ledger writer, never letting it raise.

        Args:
            event: The event to record.
        """
        try:
            self._ledger_writer.record(event)
        except Exception as exc:
            _LOGGER.warning(
                "ledger writer failed to record alert: %s",
                exc,
                extra={"component": "alerts"},
            )
