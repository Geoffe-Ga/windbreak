"""Tests for windbreak.alerts.registry (issues #14, #186): the alert catalog.

`AlertType` carries the 14 verbatim alert strings from SPEC Section 14 as a
strict subset, plus one deliberate internal SPEC T12 addition,
`GATE_COMPUTATION_MISMATCH` (issue #186), for a total of 15 members.
`ALERT_REGISTRY` must give each one a severity and a human-readable
description, and `cli_token` must give each one a unique, shell-safe
identifier for the `alert-test` CLI subcommand.

`AlertType.GATE_COMPUTATION_MISMATCH` does not exist yet, so the tests that
reference it fail at collection or assertion time -- the expected RED state
for issue #186's Gate 1.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from windbreak.alerts.registry import (
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
    "GATE_COMPUTATION_MISMATCH": "gate computation mismatch",
}

#: The single SPEC T12 addition beyond the closed SPEC S14 set (issue #186).
_SPEC_T12_EXTRA_ALERTS = frozenset({"gate computation mismatch"})


def test_alert_type_has_exactly_15_members() -> None:
    """`AlertType` holds the 14 SPEC S14 members plus 1 SPEC T12 addition."""
    assert len(AlertType) == 15


def test_alert_type_values_are_a_strict_superset_of_spec_section_14() -> None:
    """Every SPEC S14 string is still present in `AlertType`, verbatim."""
    assert {member.value for member in AlertType} > SPEC_SECTION_14_ALERTS


def test_alert_type_extras_beyond_spec_s14_are_exactly_the_t12_addition() -> None:
    """The only member beyond the closed SPEC S14 set is the T12 addition.

    Pins the extra precisely so a future stray `AlertType` member (added
    without updating this fence) trips this test.
    """
    extras = {member.value for member in AlertType} - SPEC_SECTION_14_ALERTS

    assert extras == _SPEC_T12_EXTRA_ALERTS


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


def test_gate_computation_mismatch_is_registered_as_critical() -> None:
    """`GATE_COMPUTATION_MISMATCH` (SPEC T12, issue #186) is a CRITICAL alert.

    Pins the dual-path gate crosscheck's alert seam: the SQL and Python
    reference computations disagreeing is always operator-critical.
    """
    registration = get_registration(AlertType.GATE_COMPUTATION_MISMATCH)

    assert registration.severity == AlertSeverity.CRITICAL
    assert registration.description.strip() != ""


def test_gate_computation_mismatch_cli_token_is_hyphenated() -> None:
    """`cli_token` renders the T12 addition as a shell-safe hyphenated word."""
    assert cli_token(AlertType.GATE_COMPUTATION_MISMATCH) == "gate-computation-mismatch"


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


def test_cli_token_is_unique_across_all_alert_types() -> None:
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
