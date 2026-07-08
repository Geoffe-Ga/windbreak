"""The seven production-readiness checks (SPEC S3.3 preflight checklist).

Each check is a free function taking exactly the collaborator(s) it inspects
and returning one :class:`~windbreak.preflight.models.PreflightCheck`. Every
check that reads a raising-capable seam runs its probe through
:func:`_fail_closed`, so a collaborator that raises is graded FAIL (naming the
exception) rather than crashing the run or -- worse -- being silently treated
as a pass (fail-closed, SPEC S3.3).

The seams are injected as narrow structural protocols so the checks never
depend on a concrete transport: :class:`ReadOnlyExchangeProbe` (the read-only
venue surface), :class:`CredentialScopeProber` (a key self-test),
:class:`TradeKeyLeakProber` (an environment inspection), and the concrete
:class:`EnvTradeKeyLeakProber` that reads an injected environment mapping --
never the real :data:`os.environ` -- imitating the env-mapping seam pattern of
:class:`windbreak.riskkernel.signing.SigningKeyHandle` without importing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from windbreak.alerts.registry import AlertType
from windbreak.preflight.models import CheckStatus, PreflightCheck

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from windbreak.alerts.dispatch import AlertDispatcher
    from windbreak.config import WindbreakConfig
    from windbreak.connector.models import NormalizedMarket

#: Permission bits (group + other) that make a secrets file world-readable; a
#: locked-down key must have none of them set (SPEC S15).
_GROUP_OTHER_PERMISSION_MASK = 0o077

#: The low nine permission bits, isolated for human-readable octal reporting.
_PERMISSION_BITS_MASK = 0o777

#: The jurisdiction verdicts the eligibility check reacts to (SPEC S6.2).
_JURISDICTION_UNKNOWN = "unknown"
_JURISDICTION_INELIGIBLE = "ineligible"


@dataclass(frozen=True, slots=True)
class _CheckMeta:
    """The fixed identity of one check, shared across its verdict branches.

    Attributes:
        check_id: The check's stable dotted identifier.
        description: A short human-readable label for the check.
        spec_ref: The SPEC section the check enforces.
    """

    check_id: str
    description: str
    spec_ref: str


_EXCHANGE_META = _CheckMeta(
    "exchange.reachable_readonly", "Exchange reachable read-only", "§7.2"
)
_NO_WITHDRAWAL_META = _CheckMeta(
    "credentials.no_withdrawal_scope", "Trading key lacks withdrawal scope", "§1.1-3"
)
_SCOPE_VERIFIABLE_META = _CheckMeta(
    "credentials.scope_verifiable", "Credential scope self-test verifiable", "§15"
)
_TRADE_KEY_META = _CheckMeta(
    "credentials.trade_key_not_leaked", "Trade key not leaked to environment", "§5.2"
)
_JURISDICTION_META = _CheckMeta(
    "jurisdiction.markets_eligible", "Cached markets jurisdiction-eligible", "§6.2"
)
_SECRETS_META = _CheckMeta(
    "secrets.files_not_world_readable", "Secrets files not world-readable", "§15"
)
_BUDGETS_META = _CheckMeta(
    "credentials.llm_budgets_configured", "LLM spend budgets configured", "§5.2"
)


@dataclass(frozen=True, slots=True)
class KeyScopeProbe:
    """The result of self-testing an API key's capabilities (SPEC S1.1-3, S15).

    Attributes:
        self_test_supported: Whether the venue offers a scope self-test at all;
            when False, scope-dependent checks can only honestly SKIP.
        scope_verified: Whether the self-test positively verified the key's
            scope (meaningful only when ``self_test_supported``).
        withdrawal_capable: Whether the key can move funds off the venue; a
            trade-only key must report False.
    """

    self_test_supported: bool
    scope_verified: bool
    withdrawal_capable: bool


class ReadOnlyExchangeProbe(Protocol):
    """The read-only venue surface the reachability check probes (SPEC S7.2)."""

    def get_exchange_status(self) -> object:
        """Return the venue's current trading status (value unused here)."""
        ...

    def get_balances(self) -> object:
        """Return the account's balances (value unused here)."""
        ...


class CredentialScopeProber(Protocol):
    """The seam that self-tests an API key's scope (SPEC S1.1-3, S15)."""

    def probe(self) -> KeyScopeProbe:
        """Return the key's self-test result.

        Returns:
            The :class:`KeyScopeProbe` describing the key's capabilities.
        """
        ...


class TradeKeyLeakProber(Protocol):
    """The seam that reports whether the trade key leaked into the env (S5.2)."""

    def trade_key_visible(self) -> bool:
        """Return whether the trade-key variable is visible in the environment.

        Returns:
            ``True`` if the trade key is present in the inspected environment.
        """
        ...


@dataclass(frozen=True, slots=True)
class EnvTradeKeyLeakProber:
    """A :class:`TradeKeyLeakProber` reading an injected environment mapping.

    It inspects exactly the mapping it is constructed with -- never the real
    :data:`os.environ` -- mirroring the env-mapping seam pattern of
    :class:`windbreak.riskkernel.signing.SigningKeyHandle` so tests can drive a
    plain dict and never touch the process environment (SPEC S5.2).

    Attributes:
        environ: The environment mapping to inspect. Excluded from the
            auto-generated ``repr`` (``repr=False``): in production the CLI
            injects the real :data:`os.environ`, so reprinting it -- in a log
            line, a traceback frame, or an error message -- would echo every
            secret in the environment, including the trade key this check
            exists to protect (SPEC S5.2). Only membership is ever read.
        var: The trade-key variable name whose presence signals a leak.
    """

    environ: Mapping[str, str] = field(repr=False)
    var: str

    def trade_key_visible(self) -> bool:
        """Return whether ``var`` is a key of the injected mapping.

        Returns:
            ``True`` iff ``self.var`` is present in ``self.environ``.
        """
        return self.var in self.environ


def _result(meta: _CheckMeta, status: CheckStatus, detail: str) -> PreflightCheck:
    """Build a :class:`PreflightCheck` from a check's identity and a verdict.

    Args:
        meta: The check's fixed identity.
        status: The graded verdict.
        detail: The result-specific explanation.

    Returns:
        The assembled :class:`PreflightCheck`.
    """
    return PreflightCheck(
        check_id=meta.check_id,
        description=meta.description,
        status=status,
        detail=detail,
        spec_ref=meta.spec_ref,
    )


def _fail_closed(
    check_id: str,
    description: str,
    spec_ref: str,
    probe: Callable[[], PreflightCheck],
) -> PreflightCheck:
    """Run ``probe``, converting any exception into a FAIL verdict (SPEC S3.3).

    A go-live check must never be silently passed or skipped because a
    collaborator raised: an errored probe is graded FAIL, with the exception's
    type and message named in the detail for diagnosis.

    Args:
        check_id: The check's dotted identifier.
        description: The check's short label.
        spec_ref: The SPEC section the check enforces.
        probe: The zero-argument callable that performs the check.

    Returns:
        The probe's own :class:`PreflightCheck`, or a FAIL check naming the
        exception if the probe raised.
    """
    try:
        return probe()
    except Exception as exc:  # fail-closed: any error is a FAIL, never a pass
        return PreflightCheck(
            check_id=check_id,
            description=description,
            status=CheckStatus.FAIL,
            detail=f"failed closed: {type(exc).__name__}: {exc}",
            spec_ref=spec_ref,
        )


def check_exchange_reachable(connector: ReadOnlyExchangeProbe) -> PreflightCheck:
    """Verify the venue answers read-only status and balance calls (SPEC S7.2).

    Args:
        connector: The read-only exchange seam to probe.

    Returns:
        PASS if both calls succeed; FAIL (fail-closed) if either raises.
    """

    def _probe() -> PreflightCheck:
        """Read status then balances; PASS only if both calls return."""
        connector.get_exchange_status()
        connector.get_balances()
        return _result(
            _EXCHANGE_META, CheckStatus.PASS, "exchange status ok, balances fetched"
        )

    return _fail_closed(
        _EXCHANGE_META.check_id,
        _EXCHANGE_META.description,
        _EXCHANGE_META.spec_ref,
        _probe,
    )


def check_credentials_no_withdrawal_scope(
    prober: CredentialScopeProber,
) -> PreflightCheck:
    """Reject a trading key that can withdraw funds (SPEC S1.1-3).

    A withdrawal-capable key hard-fails regardless of any other flag; a venue
    with no self-test can only SKIP (capability is unknowable); an unverified
    but non-withdrawal key passes here (verifiability is a separate check).

    Args:
        prober: The credential scope self-test seam.

    Returns:
        FAIL if the key can withdraw (or the prober raised), SKIP if no
        self-test is supported, else PASS.
    """

    def _probe() -> PreflightCheck:
        """Grade the self-test: withdrawal FAILs, no self-test SKIPs, else PASS."""
        result = prober.probe()
        if result.withdrawal_capable:
            return _result(
                _NO_WITHDRAWAL_META,
                CheckStatus.FAIL,
                "trading key can withdraw funds; a trade-only key is required",
            )
        if not result.self_test_supported:
            return _result(
                _NO_WITHDRAWAL_META,
                CheckStatus.SKIP,
                "venue offers no credential scope self-test",
            )
        return _result(
            _NO_WITHDRAWAL_META,
            CheckStatus.PASS,
            "self-test confirms the key cannot withdraw funds",
        )

    return _fail_closed(
        _NO_WITHDRAWAL_META.check_id,
        _NO_WITHDRAWAL_META.description,
        _NO_WITHDRAWAL_META.spec_ref,
        _probe,
    )


def check_credentials_scope_verifiable(
    prober: CredentialScopeProber,
) -> PreflightCheck:
    """Verify the key's scope could actually be self-tested (SPEC S15).

    A venue with no self-test SKIPs; a self-test that ran but could not verify
    the scope FAILs; a self-test that verified it passes.

    Args:
        prober: The credential scope self-test seam.

    Returns:
        SKIP if no self-test is supported, FAIL if it ran but could not verify
        (or the prober raised), else PASS.
    """

    def _probe() -> PreflightCheck:
        """Grade the self-test: no self-test SKIPs, unverified FAILs, else PASS."""
        result = prober.probe()
        if not result.self_test_supported:
            return _result(
                _SCOPE_VERIFIABLE_META,
                CheckStatus.SKIP,
                "venue offers no credential scope self-test",
            )
        if not result.scope_verified:
            return _result(
                _SCOPE_VERIFIABLE_META,
                CheckStatus.FAIL,
                "self-test ran but could not verify the key's scope",
            )
        return _result(
            _SCOPE_VERIFIABLE_META,
            CheckStatus.PASS,
            "self-test verified the key's scope",
        )

    return _fail_closed(
        _SCOPE_VERIFIABLE_META.check_id,
        _SCOPE_VERIFIABLE_META.description,
        _SCOPE_VERIFIABLE_META.spec_ref,
        _probe,
    )


def check_credentials_trade_key_not_leaked(
    prober: TradeKeyLeakProber,
) -> PreflightCheck:
    """Verify the trade key is not exposed in the environment (SPEC S5.2).

    SECURITY: the FAIL detail names only that the variable is visible, never
    the key's value -- preflight diagnoses a leak without ever echoing secret
    material.

    Args:
        prober: The environment-inspection seam.

    Returns:
        FAIL if the trade-key variable is visible (or the prober raised), else
        PASS.
    """

    def _probe() -> PreflightCheck:
        """FAIL if the trade key is visible (naming no value), else PASS."""
        if prober.trade_key_visible():
            return _result(
                _TRADE_KEY_META,
                CheckStatus.FAIL,
                "the trade-key environment variable is visible in this process",
            )
        return _result(
            _TRADE_KEY_META,
            CheckStatus.PASS,
            "no trade-key environment variable is present",
        )

    return _fail_closed(
        _TRADE_KEY_META.check_id,
        _TRADE_KEY_META.description,
        _TRADE_KEY_META.spec_ref,
        _probe,
    )


def check_jurisdiction_markets_eligible(
    eligible_markets: Sequence[NormalizedMarket],
    alert_dispatcher: AlertDispatcher,
) -> PreflightCheck:
    """Verify every cached market is jurisdiction-eligible (SPEC S6.2).

    With no cached markets there is nothing to judge, so the check SKIPs. Each
    unknown-jurisdiction market fires exactly one ``JURISDICTION_UNKNOWN``
    alert; an ``ineligible`` market is a definite verdict and fires no alert.
    Any unknown or ineligible market fails the check.

    Args:
        eligible_markets: The cached markets to judge.
        alert_dispatcher: The dispatcher unknown-jurisdiction alerts fan out
            through.

    Returns:
        SKIP if there are no markets, FAIL if any is unknown or ineligible,
        else PASS.
    """
    if not eligible_markets:
        return _result(
            _JURISDICTION_META, CheckStatus.SKIP, "no cached markets to judge yet"
        )
    unknown = [
        market
        for market in eligible_markets
        if market.jurisdiction_status == _JURISDICTION_UNKNOWN
    ]
    ineligible = [
        market
        for market in eligible_markets
        if market.jurisdiction_status == _JURISDICTION_INELIGIBLE
    ]
    for market in unknown:
        alert_dispatcher.dispatch(
            AlertType.JURISDICTION_UNKNOWN,
            f"cached market {market.ticker} has unknown jurisdiction status",
        )
    if unknown or ineligible:
        detail = (
            f"{len(unknown)} unknown, {len(ineligible)} ineligible "
            f"of {len(eligible_markets)} cached markets"
        )
        return _result(_JURISDICTION_META, CheckStatus.FAIL, detail)
    return _result(
        _JURISDICTION_META,
        CheckStatus.PASS,
        f"all {len(eligible_markets)} cached markets are jurisdiction-eligible",
    )


def _world_readable_offenders(secrets_paths: Sequence[Path]) -> list[str]:
    """Return a description of each secrets file with group/other bits set.

    Args:
        secrets_paths: The secrets files to stat and inspect.

    Returns:
        One ``"<path> (<octal-mode>)"`` string per world-readable file, empty
        when every file is owner-only.

    Raises:
        OSError: If a path cannot be stat'd (missing or unreadable); the caller
            fails the check closed on this (SPEC S3.3).
    """
    offenders: list[str] = []
    for path in secrets_paths:
        mode = path.stat().st_mode & _PERMISSION_BITS_MASK
        if mode & _GROUP_OTHER_PERMISSION_MASK:
            offenders.append(f"{path} ({mode:#o})")
    return offenders


def check_secrets_files_not_world_readable(
    secrets_paths: Sequence[Path],
) -> PreflightCheck:
    """Verify no configured secrets file is group/other readable (SPEC S15).

    With no configured paths the check SKIPs. A missing or unreadable path
    fails the check closed rather than skipping it.

    Args:
        secrets_paths: The secrets files to inspect.

    Returns:
        SKIP if no paths are configured, FAIL if any file is world-readable or
        cannot be stat'd, else PASS.
    """
    if not secrets_paths:
        return _result(_SECRETS_META, CheckStatus.SKIP, "no secrets files configured")

    def _probe() -> PreflightCheck:
        """FAIL if any file is world-readable (a stat error also FAILs), else PASS."""
        offenders = _world_readable_offenders(secrets_paths)
        if offenders:
            return _result(
                _SECRETS_META,
                CheckStatus.FAIL,
                f"world-readable secrets: {', '.join(offenders)}",
            )
        return _result(
            _SECRETS_META,
            CheckStatus.PASS,
            f"all {len(secrets_paths)} secrets files are owner-only",
        )

    return _fail_closed(
        _SECRETS_META.check_id,
        _SECRETS_META.description,
        _SECRETS_META.spec_ref,
        _probe,
    )


def check_credentials_llm_budgets_configured(
    config: WindbreakConfig,
) -> PreflightCheck:
    """Verify the LLM research spend budgets are positive (SPEC S5.2).

    Args:
        config: The loaded configuration whose forecast budget is inspected.

    Returns:
        PASS iff both ``per_forecast_micros`` and ``per_day_micros`` are
        strictly positive, else FAIL.
    """
    budget = config.forecast.budget
    if budget.per_forecast_micros > 0 and budget.per_day_micros > 0:
        return _result(
            _BUDGETS_META,
            CheckStatus.PASS,
            "per-forecast and per-day LLM budgets are positive",
        )
    return _result(
        _BUDGETS_META,
        CheckStatus.FAIL,
        "per-forecast and per-day LLM budgets must both be positive",
    )
