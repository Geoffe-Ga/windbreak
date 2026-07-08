"""Compose and render the preflight checklist (SPEC S3.3).

:func:`run_preflight` runs the seven checks in their documented order and
bundles them into a :class:`~windbreak.preflight.models.PreflightReport`; every
check always runs, so one fail-closed seam never truncates the checklist. The
two render helpers project a report for a human (:func:`render_table`) or a
machine (:func:`report_to_json`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from windbreak.preflight.checks import (
    check_credentials_llm_budgets_configured,
    check_credentials_no_withdrawal_scope,
    check_credentials_scope_verifiable,
    check_credentials_trade_key_not_leaked,
    check_exchange_reachable,
    check_jurisdiction_markets_eligible,
    check_secrets_files_not_world_readable,
)
from windbreak.preflight.models import CheckStatus, PreflightCheck, PreflightReport

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from windbreak.alerts.dispatch import AlertDispatcher
    from windbreak.config import WindbreakConfig
    from windbreak.connector.models import NormalizedMarket
    from windbreak.preflight.checks import (
        CredentialScopeProber,
        ReadOnlyExchangeProbe,
        TradeKeyLeakProber,
    )

#: Width the status token is padded to so the check-id column aligns.
_STATUS_COLUMN_WIDTH = 4


def run_preflight(
    *,
    connector: ReadOnlyExchangeProbe,
    scope_prober: CredentialScopeProber,
    leak_prober: TradeKeyLeakProber,
    eligible_markets: Sequence[NormalizedMarket],
    alert_dispatcher: AlertDispatcher,
    secrets_paths: Sequence[Path],
    config: WindbreakConfig,
) -> PreflightReport:
    """Run the seven preflight checks in order and bundle the report (S3.3).

    Every check runs unconditionally: a fail-closed verdict on an earlier seam
    never stops a later check from running or appearing in the report.

    Args:
        connector: The read-only exchange seam for the reachability check.
        scope_prober: The credential scope self-test seam.
        leak_prober: The trade-key environment-inspection seam.
        eligible_markets: The cached markets to judge for jurisdiction.
        alert_dispatcher: The dispatcher jurisdiction alerts fan out through.
        secrets_paths: The secrets files to inspect for world-readability.
        config: The loaded configuration for the LLM budget check.

    Returns:
        The assembled :class:`PreflightReport`.
    """
    checks = (
        check_exchange_reachable(connector),
        check_credentials_no_withdrawal_scope(scope_prober),
        check_credentials_scope_verifiable(scope_prober),
        check_credentials_trade_key_not_leaked(leak_prober),
        check_jurisdiction_markets_eligible(eligible_markets, alert_dispatcher),
        check_secrets_files_not_world_readable(secrets_paths),
        check_credentials_llm_budgets_configured(config),
    )
    return PreflightReport(checks=checks)


def _render_row(check: PreflightCheck) -> str:
    """Render one check as a single aligned table row.

    Args:
        check: The graded check to render.

    Returns:
        A row like ``PASS  exchange.reachable_readonly  <detail> (§7.2)``.
    """
    status = check.status.name.ljust(_STATUS_COLUMN_WIDTH)
    return f"{status}  {check.check_id}  {check.detail} ({check.spec_ref})"


def _render_summary(report: PreflightReport) -> str:
    """Render the closing summary line for a report.

    Args:
        report: The report to summarize.

    Returns:
        A ``preflight FAILED (<N> failure...)`` line when any check failed, else
        a passed summary that never contains the substring ``FAILED``.
    """
    failures = sum(1 for check in report.checks if check.status is CheckStatus.FAIL)
    if failures:
        plural = "" if failures == 1 else "s"
        return f"→ preflight FAILED ({failures} failure{plural})"
    passed = sum(1 for check in report.checks if check.status is CheckStatus.PASS)
    skipped = sum(1 for check in report.checks if check.status is CheckStatus.SKIP)
    return f"→ preflight passed ({passed} passed, {skipped} skipped)"


def render_table(report: PreflightReport) -> str:
    """Render a report as a human-readable table with a summary line.

    Args:
        report: The report to render.

    Returns:
        One row per check followed by a summary line, newline-joined.
    """
    rows = [_render_row(check) for check in report.checks]
    rows.append(_render_summary(report))
    return "\n".join(rows)


def report_to_json(report: PreflightReport) -> str:
    """Render a report as a single JSON document.

    Args:
        report: The report to render.

    Returns:
        The ``json.dumps`` of :meth:`PreflightReport.to_payload`.
    """
    return json.dumps(report.to_payload())
