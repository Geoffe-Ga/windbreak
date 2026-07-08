"""Failing-first tests for windbreak.preflight's report models (issue #56, RED).

`windbreak.preflight` does not exist yet, so the import below fails collection
with `ModuleNotFoundError: No module named 'windbreak.preflight'` -- the
expected Gate 1 RED state for issue #56.

Pins `PreflightCheck` (a frozen, slotted dataclass) and `PreflightReport`
(ordered checks, a fail-closed `exit_code`, id lookup via `__getitem__`, and a
JSON-safe `to_payload()`) exactly as EPIC_08_ISSUE_01 specifies them.
"""

from __future__ import annotations

import json

import pytest

from windbreak.preflight import CheckStatus, PreflightCheck, PreflightReport

#: Placeholder spec reference reused by every check built in this module;
#: the exact SPEC token itself is not under test here.
_SPEC_REF = "S7.2"


def _check(check_id: str, status: CheckStatus, detail: str = "ok") -> PreflightCheck:
    """Build a minimal `PreflightCheck` for a given id/status pair."""
    return PreflightCheck(
        check_id=check_id,
        description=f"{check_id} description",
        status=status,
        detail=detail,
        spec_ref=_SPEC_REF,
    )


def test_preflight_check_is_frozen() -> None:
    """`PreflightCheck` is immutable: mutating any field raises."""
    check = _check("a", CheckStatus.PASS)

    with pytest.raises(AttributeError):
        # setattr (rather than a static `check.status = ...` assignment) so no
        # `# type: ignore` suppression is needed for mypy's frozen-dataclass
        # read-only-attribute check -- the runtime `FrozenInstanceError` this
        # test pins is a plain `AttributeError` subclass either way.
        check.status = CheckStatus.FAIL


def test_report_preserves_the_order_checks_were_constructed_with() -> None:
    """`PreflightReport.checks` iterates in exactly the given construction order."""
    checks = (
        _check("a", CheckStatus.PASS),
        _check("b", CheckStatus.FAIL),
        _check("c", CheckStatus.SKIP),
    )

    report = PreflightReport(checks=checks)

    assert tuple(report.checks) == checks
    assert [check.check_id for check in report.checks] == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("statuses", "expected_exit_code"),
    [
        ((CheckStatus.PASS, CheckStatus.PASS), 0),
        ((CheckStatus.PASS, CheckStatus.FAIL), 1),
        ((CheckStatus.SKIP, CheckStatus.SKIP), 0),
        ((CheckStatus.PASS, CheckStatus.SKIP), 0),
        ((CheckStatus.SKIP, CheckStatus.FAIL), 1),
        ((CheckStatus.FAIL, CheckStatus.FAIL), 1),
    ],
    ids=[
        "all-pass-is-zero",
        "any-fail-is-one",
        "all-skip-is-zero",
        "pass-plus-skip-is-zero",
        "fail-among-skips-is-one",
        "all-fail-is-one",
    ],
)
def test_exit_code_truth_table(
    statuses: tuple[CheckStatus, ...], expected_exit_code: int
) -> None:
    """`exit_code` is 0 iff every non-SKIP check is PASS; SKIP never blocks,
    and an all-SKIP report is still a clean (0) exit.
    """
    checks = tuple(_check(f"check-{i}", status) for i, status in enumerate(statuses))

    report = PreflightReport(checks=checks)

    assert report.exit_code == expected_exit_code


def test_getitem_returns_the_matching_check_by_id() -> None:
    """`report[check_id]` returns the check whose `check_id` matches exactly."""
    target = _check("credentials.no_withdrawal_scope", CheckStatus.FAIL)
    report = PreflightReport(checks=(_check("other", CheckStatus.PASS), target))

    assert report["credentials.no_withdrawal_scope"] is target


def test_getitem_miss_raises_key_error() -> None:
    """`report[check_id]` raises `KeyError` for an id no check carries."""
    report = PreflightReport(checks=(_check("a", CheckStatus.PASS),))

    with pytest.raises(KeyError):
        report["not-a-real-check-id"]


def _assert_no_float_leaf(value: object) -> None:
    """Recursively assert no `float` appears anywhere in a JSON-safe structure."""
    if isinstance(value, float):
        raise AssertionError(f"found a float leaf: {value!r}")
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_float_leaf(key)
            _assert_no_float_leaf(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_float_leaf(item)


def test_to_payload_round_trips_through_json_with_status_as_name_string() -> None:
    """`to_payload()` is JSON-safe: `json.dumps` succeeds, every check's status
    is rendered as its `.name` string (not the bare enum object), and the
    whole payload contains no float leaf anywhere.
    """
    report = PreflightReport(
        checks=(
            _check("a", CheckStatus.PASS),
            _check("b", CheckStatus.FAIL, detail="boom"),
            _check("c", CheckStatus.SKIP),
        )
    )

    payload = report.to_payload()
    round_tripped = json.loads(json.dumps(payload))

    assert round_tripped == payload
    _assert_no_float_leaf(payload)
    assert payload["exit_code"] == report.exit_code

    statuses_by_id = {entry["check_id"]: entry["status"] for entry in payload["checks"]}
    assert statuses_by_id == {"a": "PASS", "b": "FAIL", "c": "SKIP"}
    for entry in payload["checks"]:
        assert isinstance(entry["status"], str)
