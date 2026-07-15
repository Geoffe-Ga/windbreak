"""Shared component: operator alerting primitives.

Defines the alert catalog (:class:`AlertType`, :class:`AlertSeverity`,
:data:`ALERT_REGISTRY`), the delivery channels (:class:`NtfySink`,
:class:`WebhookSink`, :class:`SmtpSink`, :class:`DesktopSink`,
:class:`LogOnlySink`), and the :class:`AlertDispatcher` that fans an alert out
to those channels while isolating failures. :func:`dispatch_hook` binds a
dispatcher to the crosscheck's ``(severity, message) -> None`` alert seam.

Example:
    >>> from windbreak.alerts import AlertDispatcher, AlertType, LoggingLedgerWriter
    >>> dispatcher = AlertDispatcher(sinks=[], ledger_writer=LoggingLedgerWriter())
    >>> event = dispatcher.dispatch(AlertType.MODE_CHANGE, "switched to PAPER")

Two dependency-injection seams are wired by successor issues: config-driven
sink construction (issue #11) supplies the concrete ``*Sink`` instances from
the ``*SinkConfig`` dataclasses, and the ledger (issue #13) provides a real
:class:`LedgerWriter` that persists each :class:`AlertEmitted`.
"""

from windbreak.alerts.dispatch import (
    AlertDispatcher,
    AlertEmitted,
    LedgerWriter,
    LoggingLedgerWriter,
    SinkOutcome,
    dispatch_hook,
)
from windbreak.alerts.registry import (
    ALERT_REGISTRY,
    AlertRegistration,
    AlertSeverity,
    AlertType,
    cli_token,
    get_registration,
)
from windbreak.alerts.sinks import (
    DesktopSink,
    LogOnlySink,
    NtfySink,
    NtfySinkConfig,
    SinkSendError,
    SmtpSink,
    SmtpSinkConfig,
    WebhookSink,
    WebhookSinkConfig,
)

__all__ = [
    "ALERT_REGISTRY",
    "AlertDispatcher",
    "AlertEmitted",
    "AlertRegistration",
    "AlertSeverity",
    "AlertType",
    "DesktopSink",
    "LedgerWriter",
    "LogOnlySink",
    "LoggingLedgerWriter",
    "NtfySink",
    "NtfySinkConfig",
    "SinkOutcome",
    "SinkSendError",
    "SmtpSink",
    "SmtpSinkConfig",
    "WebhookSink",
    "WebhookSinkConfig",
    "cli_token",
    "dispatch_hook",
    "get_registration",
]
