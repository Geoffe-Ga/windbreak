"""Tests for hedgekit.alerts.registry (issue #14): the SPEC S14 alert catalog.

`AlertType` must carry exactly the 14 verbatim alert strings from SPEC
Section 14, `ALERT_REGISTRY` must give each one a severity and a
human-readable description, and `cli_token` must give each one a unique,
shell-safe identifier for the `alert-test` CLI subcommand.

None of `hedgekit.alerts.registry`'s public names exist yet, so importing
this module fails at collection with `ModuleNotFoundError` -- the expected
RED state for issue #14's Gate 1.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from hedgekit.alerts.registry import (
    ALERT_REGISTRY,
    AlertRegistration,
    AlertSeverity,
    AlertType,
    cli_token,
    get_registration,
)

#: SPEC Section 14's 14 alert strings, verbatim, independent of this module's
#: implementation -- the source of truth this test suite pins `AlertType`
#: against.
SPEC_SECTION_14_ALERTS = frozenset(
    {
        "mode change",
        "halt/kill",
        "veto",
        "reconciliation mismatch",
        "schema anomaly",
        "floor-change request",
        "daily-loss pause",
        "drawdown demotion",
        "fee model unavailable",
        "jurisdiction unknown",
        "canary drift",
        "profit-sweep advisory",
        "backup failure",
        "disk halt",
    }
)

_MEMBER_NAME_TO_VALUE = {
    "MODE_CHANGE": "mode change",
    "HALT_KILL": "halt/kill",
    "VETO": "veto",
    "RECONCILIATION_MISMATCH": "reconciliation mismatch",
    "SCHEMA_ANOMALY": "schema anomaly",
    "FLOOR_CHANGE_REQUEST": "floor-change request",
    "DAILY_LOSS_PAUSE": "daily-loss pause",
    "DRAWDOWN_DEMOTION": "drawdown demotion",
    "FEE_MODEL_UNAVAILABLE": "fee model unavailable",
    "JURISDICTION_UNKNOWN": "jurisdiction unknown",
    "CANARY_DRIFT": "canary drift",
    "PROFIT_SWEEP_ADVISORY": "profit-sweep advisory",
    "BACKUP_FAILURE": "backup failure",
    "DISK_HALT": "disk halt",
}


def test_alert_type_has_exactly_14_members() -> None:
    """SPEC S14 defines exactly 14 alert types -- no more, no fewer."""
    assert len(AlertType) == 14


def test_alert_type_values_match_spec_section_14_verbatim() -> None:
    """Every `AlertType` value is one of SPEC S14's strings, and vice versa."""
    assert {member.value for member in AlertType} == SPEC_SECTION_14_ALERTS


@pytest.mark.parametrize(
    ("member_name", "expected_value"), sorted(_MEMBER_NAME_TO_VALUE.items())
)
def test_alert_type_member_name_maps_to_spec_value(
    member_name: str, expected_value: str
) -> None:
    """Each documented member name resolves to its exact SPEC S14 string."""
    assert AlertType[member_name].value == expected_value


def test_alert_registry_covers_every_alert_type() -> None:
    """`ALERT_REGISTRY` has one entry per `AlertType` member -- no gaps."""
    assert set(ALERT_REGISTRY.keys()) == set(AlertType)


@pytest.mark.parametrize("alert_type", list(AlertType))
def test_alert_registry_entry_has_severity_and_nonempty_description(
    alert_type: AlertType,
) -> None:
    """Every registration carries a real `AlertSeverity` and prose description."""
    registration = ALERT_REGISTRY[alert_type]

    assert isinstance(registration.severity, AlertSeverity)
    assert registration.description.strip() != ""


@pytest.mark.parametrize("alert_type", list(AlertType))
def test_get_registration_round_trips_with_the_registry(
    alert_type: AlertType,
) -> None:
    """`get_registration` returns the same entry stored in `ALERT_REGISTRY`."""
    assert get_registration(alert_type) == ALERT_REGISTRY[alert_type]


def test_alert_registration_is_frozen() -> None:
    """`AlertRegistration` instances cannot be mutated after construction."""
    registration = AlertRegistration(severity=AlertSeverity.INFO, description="x")

    with pytest.raises(dataclasses.FrozenInstanceError):
        registration.severity = AlertSeverity.CRITICAL  # type: ignore[misc]


def test_cli_token_hyphenates_multi_word_member_names() -> None:
    """`cli_token` lowercases and hyphenates the enum member name."""
    assert cli_token(AlertType.HALT_KILL) == "halt-kill"
    assert cli_token(AlertType.MODE_CHANGE) == "mode-change"


@pytest.mark.parametrize("alert_type", list(AlertType))
def test_cli_token_has_no_spaces_or_slashes(alert_type: AlertType) -> None:
    """A CLI token must be a single shell-safe word."""
    token = cli_token(alert_type)

    assert " " not in token
    assert "/" not in token


def test_cli_token_is_unique_across_all_14_alert_types() -> None:
    """No two alert types collide on the same CLI token."""
    tokens = [cli_token(alert_type) for alert_type in AlertType]

    assert len(tokens) == len(set(tokens))


@pytest.mark.parametrize(
    ("severity", "expected_level"),
    [
        (AlertSeverity.INFO, logging.INFO),
        (AlertSeverity.WARNING, logging.WARNING),
        (AlertSeverity.CRITICAL, logging.CRITICAL),
    ],
)
def test_alert_severity_to_log_level_maps_correctly(
    severity: AlertSeverity, expected_level: int
) -> None:
    """Each severity maps to its corresponding stdlib `logging` level."""
    assert severity.to_log_level() == expected_level
