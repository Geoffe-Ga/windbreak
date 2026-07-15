"""SPEC Section 14 alert catalog: types, severities, and CLI tokens.

Defines :class:`AlertType` (the 14 verbatim SPEC S14 alert strings plus one
internal SPEC T12 crosscheck addition, ``GATE_COMPUTATION_MISMATCH``, which is
not part of the closed S14 set), :class:`AlertSeverity`, the
:data:`ALERT_REGISTRY` mapping each type to a severity and human-readable
description, and :func:`cli_token`, which derives a shell-safe identifier used
by the hidden ``alert-test`` CLI subcommand.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping


class AlertSeverity(enum.Enum):
    """Operator-facing urgency of an alert."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    def to_log_level(self) -> int:
        """Map this severity to its stdlib :mod:`logging` level.

        Returns:
            ``logging.INFO``, ``logging.WARNING``, or ``logging.CRITICAL``.
        """
        return _SEVERITY_LOG_LEVELS[self]


#: Maps each severity to the stdlib logging level a sink should emit it at.
_SEVERITY_LOG_LEVELS: Final[Mapping[AlertSeverity, int]] = MappingProxyType(
    {
        AlertSeverity.INFO: logging.INFO,
        AlertSeverity.WARNING: logging.WARNING,
        AlertSeverity.CRITICAL: logging.CRITICAL,
    }
)


class AlertType(enum.Enum):
    """Operator alert types: the 14 verbatim SPEC S14 members plus one T12 add.

    The first 14 members are the alert strings defined verbatim in SPEC
    Section 14. ``GATE_COMPUTATION_MISMATCH`` is an internal SPEC T12 crosscheck
    addition (issue #186), not part of the closed S14 set.
    """

    MODE_CHANGE = "mode change"
    HALT_KILL = "halt/kill"
    VETO = "veto"
    RECONCILIATION_MISMATCH = "reconciliation mismatch"
    SCHEMA_ANOMALY = "schema anomaly"
    FLOOR_CHANGE_REQUEST = "floor-change request"
    DAILY_LOSS_PAUSE = "daily-loss pause"
    DRAWDOWN_DEMOTION = "drawdown demotion"
    FEE_MODEL_UNAVAILABLE = "fee model unavailable"
    JURISDICTION_UNKNOWN = "jurisdiction unknown"
    CANARY_DRIFT = "canary drift"
    PROFIT_SWEEP_ADVISORY = "profit-sweep advisory"
    BACKUP_FAILURE = "backup failure"
    DISK_HALT = "disk halt"
    # Internal SPEC T12 crosscheck addition (issue #186), NOT one of the 14
    # verbatim SPEC S14 members.
    GATE_COMPUTATION_MISMATCH = "gate computation mismatch"


@dataclass(frozen=True)
class AlertRegistration:
    """A registered alert's severity and human-readable description.

    Attributes:
        severity: How urgently an operator should respond.
        description: One-line prose describing when this alert fires.
    """

    severity: AlertSeverity
    description: str


#: The catalog of every alert type paired with its severity and description.
ALERT_REGISTRY: Final[Mapping[AlertType, AlertRegistration]] = MappingProxyType(
    {
        AlertType.MODE_CHANGE: AlertRegistration(
            AlertSeverity.WARNING,
            "The operating mode transitioned between RESEARCH, PAPER, or LIVE.",
        ),
        AlertType.HALT_KILL: AlertRegistration(
            AlertSeverity.CRITICAL,
            "Trading was halted or the kill switch was engaged.",
        ),
        AlertType.VETO: AlertRegistration(
            AlertSeverity.WARNING,
            "A proposed order was vetoed by the risk kernel.",
        ),
        AlertType.RECONCILIATION_MISMATCH: AlertRegistration(
            AlertSeverity.CRITICAL,
            "Ledger and venue positions disagreed during reconciliation.",
        ),
        AlertType.SCHEMA_ANOMALY: AlertRegistration(
            AlertSeverity.WARNING,
            "An incoming payload failed schema validation.",
        ),
        AlertType.FLOOR_CHANGE_REQUEST: AlertRegistration(
            AlertSeverity.INFO,
            "An operator requested a change to a configured risk floor.",
        ),
        AlertType.DAILY_LOSS_PAUSE: AlertRegistration(
            AlertSeverity.CRITICAL,
            "The daily loss limit was reached and trading paused for the day.",
        ),
        AlertType.DRAWDOWN_DEMOTION: AlertRegistration(
            AlertSeverity.CRITICAL,
            "Sustained drawdown demoted the strategy to a lower risk tier.",
        ),
        AlertType.FEE_MODEL_UNAVAILABLE: AlertRegistration(
            AlertSeverity.WARNING,
            "The fee model could not be loaded, blocking fee-aware sizing.",
        ),
        AlertType.JURISDICTION_UNKNOWN: AlertRegistration(
            AlertSeverity.WARNING,
            "A market's jurisdiction could not be determined.",
        ),
        AlertType.CANARY_DRIFT: AlertRegistration(
            AlertSeverity.WARNING,
            "A canary check drifted beyond its configured tolerance.",
        ),
        AlertType.PROFIT_SWEEP_ADVISORY: AlertRegistration(
            AlertSeverity.INFO,
            "Accumulated profit is eligible for an advisory sweep.",
        ),
        AlertType.BACKUP_FAILURE: AlertRegistration(
            AlertSeverity.WARNING,
            "A scheduled state backup failed to complete.",
        ),
        AlertType.DISK_HALT: AlertRegistration(
            AlertSeverity.CRITICAL,
            "Free disk space fell below the safe threshold; trading halted.",
        ),
        AlertType.GATE_COMPUTATION_MISMATCH: AlertRegistration(
            AlertSeverity.CRITICAL,
            "The SQL and Python dual-path gate computations disagreed on at "
            "least one metric (SPEC T12).",
        ),
    }
)


def get_registration(alert_type: AlertType) -> AlertRegistration:
    """Return the registration for an alert type.

    Args:
        alert_type: The alert type to look up.

    Returns:
        The :class:`AlertRegistration` stored in :data:`ALERT_REGISTRY`.
    """
    return ALERT_REGISTRY[alert_type]


def cli_token(alert_type: AlertType) -> str:
    """Derive a shell-safe CLI token from an alert type's member name.

    Args:
        alert_type: The alert type to tokenize.

    Returns:
        The member name lowercased with underscores replaced by hyphens (for
        example ``HALT_KILL`` becomes ``halt-kill``).
    """
    return alert_type.name.lower().replace("_", "-")
