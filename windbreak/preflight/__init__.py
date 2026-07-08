"""Shared component: the ``windbreak preflight`` production-readiness checklist.

Grades a fixed set of go-live checks (SPEC S3.3) -- exchange reachability,
credential scope, trade-key leakage, jurisdiction eligibility, secrets-file
permissions, and LLM budgets -- into an immutable
:class:`PreflightReport` whose fail-closed exit code gates a live deploy.

Example:
    >>> from windbreak.preflight import run_preflight, render_table
    >>> report = run_preflight(  # doctest: +SKIP
    ...     connector=connector,
    ...     scope_prober=scope_prober,
    ...     leak_prober=leak_prober,
    ...     eligible_markets=markets,
    ...     alert_dispatcher=dispatcher,
    ...     secrets_paths=secrets,
    ...     config=config,
    ... )
    >>> print(render_table(report))  # doctest: +SKIP

The credential scope self-test seam (:class:`CredentialScopeProber`) is wired by
a successor issue (#57); until then callers inject an honest no-self-test prober
so scope-dependent checks SKIP rather than falsely pass.
"""

from windbreak.preflight.checks import (
    CredentialScopeProber,
    EnvTradeKeyLeakProber,
    KeyScopeProbe,
    TradeKeyLeakProber,
    check_credentials_llm_budgets_configured,
    check_credentials_no_withdrawal_scope,
    check_credentials_scope_verifiable,
    check_credentials_trade_key_not_leaked,
    check_exchange_reachable,
    check_jurisdiction_markets_eligible,
    check_secrets_files_not_world_readable,
)
from windbreak.preflight.models import CheckStatus, PreflightCheck, PreflightReport
from windbreak.preflight.runner import render_table, report_to_json, run_preflight

__all__ = [
    "CheckStatus",
    "CredentialScopeProber",
    "EnvTradeKeyLeakProber",
    "KeyScopeProbe",
    "PreflightCheck",
    "PreflightReport",
    "TradeKeyLeakProber",
    "check_credentials_llm_budgets_configured",
    "check_credentials_no_withdrawal_scope",
    "check_credentials_scope_verifiable",
    "check_credentials_trade_key_not_leaked",
    "check_exchange_reachable",
    "check_jurisdiction_markets_eligible",
    "check_secrets_files_not_world_readable",
    "render_table",
    "report_to_json",
    "run_preflight",
]
