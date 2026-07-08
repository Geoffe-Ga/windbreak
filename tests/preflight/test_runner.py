"""Failing-first tests for windbreak.preflight.runner (issue #56, RED).

`windbreak.preflight` does not exist yet, so the imports below fail collection
with `ModuleNotFoundError: No module named 'windbreak.preflight'` -- the
expected Gate 1 RED state for issue #56.

Pins `run_preflight`'s composition (exactly the seven named checks, in the
issue's documented order, every check present even when an earlier one
fails-closed) and the two render helpers `render_table` / `report_to_json`.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from windbreak.alerts import AlertDispatcher
from windbreak.config import load_default_config
from windbreak.preflight import (
    CheckStatus,
    EnvTradeKeyLeakProber,
    PreflightReport,
    run_preflight,
)
from windbreak.preflight.runner import render_table, report_to_json

if TYPE_CHECKING:
    from tests.preflight.conftest import (
        FakeScopeProber,
        RaisingConnector,
        RecordingLedgerWriter,
    )
    from windbreak.connector.fake import FakeExchange

#: The seven check ids, in the exact order EPIC_08_ISSUE_01 documents them.
_EXPECTED_CHECK_IDS = (
    "exchange.reachable_readonly",
    "credentials.no_withdrawal_scope",
    "credentials.scope_verifiable",
    "credentials.trade_key_not_leaked",
    "jurisdiction.markets_eligible",
    "secrets.files_not_world_readable",
    "credentials.llm_budgets_configured",
)


def _run(
    *,
    connector: object,
    scope_prober: FakeScopeProber,
    recording_ledger_writer: RecordingLedgerWriter,
) -> PreflightReport:
    """Run `run_preflight` with a mostly-SKIP/PASS collaborator set.

    Every collaborator not under test in a given test (leak prober, markets,
    secrets paths, config) is wired to its most permissive/empty value so the
    only variation between call sites is the one seam a test cares about.
    """
    return run_preflight(
        connector=connector,
        scope_prober=scope_prober,
        leak_prober=EnvTradeKeyLeakProber(environ={}, var="WINDBREAK_TRADE_KEY"),
        eligible_markets=(),
        alert_dispatcher=AlertDispatcher([], ledger_writer=recording_ledger_writer),
        secrets_paths=(),
        config=load_default_config(),
    )


def test_run_preflight_emits_exactly_the_seven_checks_in_documented_order(
    fake_exchange: FakeExchange,
    no_self_test_prober: FakeScopeProber,
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """`run_preflight` composes exactly the seven named checks, in order."""
    report = _run(
        connector=fake_exchange,
        scope_prober=no_self_test_prober,
        recording_ledger_writer=recording_ledger_writer,
    )

    assert tuple(check.check_id for check in report.checks) == _EXPECTED_CHECK_IDS


def test_a_failing_seam_does_not_stop_later_checks_from_running(
    raising_connector: RaisingConnector,
    no_self_test_prober: FakeScopeProber,
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """A raising connector fails check 1, but every one of the later six
    checks still runs and appears in the report -- one bad seam must never
    truncate the checklist.
    """
    report = _run(
        connector=raising_connector,
        scope_prober=no_self_test_prober,
        recording_ledger_writer=recording_ledger_writer,
    )

    assert report["exchange.reachable_readonly"].status is CheckStatus.FAIL
    assert tuple(check.check_id for check in report.checks) == _EXPECTED_CHECK_IDS


def test_render_table_shows_status_rows_and_a_failure_summary_line(
    raising_connector: RaisingConnector,
    no_self_test_prober: FakeScopeProber,
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """`render_table` prints at least one PASS, one FAIL, and one SKIP row,
    plus a `preflight FAILED (<N> failure...)` summary line naming the
    failure count when at least one check failed.
    """
    report = _run(
        connector=raising_connector,
        scope_prober=no_self_test_prober,
        recording_ledger_writer=recording_ledger_writer,
    )
    assert report.exit_code == 1

    table = render_table(report)

    assert "PASS" in table
    assert "FAIL" in table
    assert "SKIP" in table
    assert re.search(r"preflight FAILED \(\d+ failure", table)


def test_render_table_shows_no_failed_summary_when_nothing_failed(
    fake_exchange: FakeExchange,
    no_self_test_prober: FakeScopeProber,
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """With no FAIL among the seven checks, the summary line never says
    FAILED.
    """
    report = _run(
        connector=fake_exchange,
        scope_prober=no_self_test_prober,
        recording_ledger_writer=recording_ledger_writer,
    )
    assert report.exit_code == 0

    table = render_table(report)

    assert "FAILED" not in table


def test_withdrawal_capable_key_fails_preflight_and_blocks_the_whole_exit_code(
    fake_exchange: FakeExchange,
    withdrawal_capable_prober: FakeScopeProber,
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """EPIC_08_ISSUE_01's verbatim example, reproduced through the real
    `run_preflight` composition: a withdrawal-capable key fails
    `credentials.no_withdrawal_scope` specifically, and that single FAIL is
    enough to fail-close the *whole* report's exit code (SPEC S1.1-3).
    """
    report = _run(
        connector=fake_exchange,
        scope_prober=withdrawal_capable_prober,
        recording_ledger_writer=recording_ledger_writer,
    )

    check = report["credentials.no_withdrawal_scope"]
    assert check.status is CheckStatus.FAIL
    assert report.exit_code == 1  # fail-closed: any FAIL blocks live modes


def test_report_to_json_parses_to_the_same_shape_as_to_payload(
    fake_exchange: FakeExchange,
    no_self_test_prober: FakeScopeProber,
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """`report_to_json` is a JSON string whose parsed value equals
    `report.to_payload()` exactly.
    """
    report = _run(
        connector=fake_exchange,
        scope_prober=no_self_test_prober,
        recording_ledger_writer=recording_ledger_writer,
    )

    rendered = report_to_json(report)

    assert json.loads(rendered) == report.to_payload()
