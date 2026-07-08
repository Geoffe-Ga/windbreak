"""Failing-first tests for each individual windbreak.preflight check (issue #56, RED).

`windbreak.preflight` does not exist yet, so the imports below fail collection
with `ModuleNotFoundError: No module named 'windbreak.preflight'` -- the
expected Gate 1 RED state for issue #56 (EPIC_08_ISSUE_01, the preflight
skeleton).

Every one of the seven checks EPIC_08_ISSUE_01 enumerates is exercised for
both its PASS and FAIL paths (plus SKIP where the issue specifies one):
exchange reachability (SPEC S7.2), withdrawal-scope rejection (S1.1-3),
scope-verifiability (S15), trade-key-leak detection (S5.2), jurisdiction
eligibility (S6.2), secrets-file permissions (S15), and LLM budget
configuration (S5.2). Fail-closed (S3.3) is pinned on every check that has a
raising-collaborator seam: an error must never be classified PASS or SKIP.

API-shape decision this file locks in for the implementer: each check is a
free function named `check_<check_id with dots replaced by underscores>`,
living in `windbreak.preflight.checks`, taking exactly the collaborator(s)
named in the issue and returning one `PreflightCheck`.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from tests.preflight.conftest import (
    FakeScopeProber,
    RaisingConnector,
    RaisingLeakProber,
    RecordingLedgerWriter,
    make_market,
)
from windbreak.alerts import AlertDispatcher, AlertType
from windbreak.config import load_default_config
from windbreak.preflight import CheckStatus, EnvTradeKeyLeakProber
from windbreak.preflight.checks import (
    check_credentials_llm_budgets_configured,
    check_credentials_no_withdrawal_scope,
    check_credentials_scope_verifiable,
    check_credentials_trade_key_not_leaked,
    check_exchange_reachable,
    check_jurisdiction_markets_eligible,
    check_secrets_files_not_world_readable,
)

if TYPE_CHECKING:
    from pathlib import Path

    from windbreak.connector.fake import FakeExchange

_TRADE_KEY_VAR = "WINDBREAK_TRADE_KEY"
#: Deliberately long and distinctive so the "detail never echoes the leaked
#: value" assertion can never accidentally collide with an ordinary English
#: word or phrasing choice in the implementer's own detail string.
_LEAKED_KEY_VALUE = "zzz-super-secret-trade-key-do-not-echo-zzz"


# --- 1. exchange.reachable_readonly (S7.2) --------------------------------------


def test_exchange_reachable_passes_when_status_and_balances_both_succeed(
    fake_exchange: FakeExchange,
) -> None:
    """A connector whose status and balance calls both succeed passes."""
    check = check_exchange_reachable(fake_exchange)

    assert check.check_id == "exchange.reachable_readonly"
    assert check.status is CheckStatus.PASS


def test_exchange_reachable_fails_closed_on_any_raise(
    raising_connector: RaisingConnector,
) -> None:
    """Any exception from `get_exchange_status` or `get_balances` fails
    closed (SPEC S3.3): an errored check is FAIL, never PASS or silent SKIP.
    """
    check = check_exchange_reachable(raising_connector)

    assert check.check_id == "exchange.reachable_readonly"
    assert check.status is CheckStatus.FAIL


# --- 2. credentials.no_withdrawal_scope (S1.1-3) --------------------------------


def test_no_withdrawal_scope_fails_on_a_withdrawal_capable_key(
    withdrawal_capable_prober: FakeScopeProber,
) -> None:
    """The issue's verbatim example: a withdrawal-capable key hard-fails,
    regardless of `self_test_supported` or `scope_verified`.
    """
    check = check_credentials_no_withdrawal_scope(withdrawal_capable_prober)

    assert check.check_id == "credentials.no_withdrawal_scope"
    assert check.status is CheckStatus.FAIL


def test_no_withdrawal_scope_passes_on_a_verified_non_withdrawal_key(
    read_only_verified_prober: FakeScopeProber,
) -> None:
    """A self-tested, verified, non-withdrawal-capable key passes."""
    check = check_credentials_no_withdrawal_scope(read_only_verified_prober)

    assert check.status is CheckStatus.PASS


def test_no_withdrawal_scope_skips_when_no_self_test_is_supported(
    no_self_test_prober: FakeScopeProber,
) -> None:
    """A venue offering no scope self-test skips: withdrawal capability is
    unknowable here, not a hard fail.
    """
    check = check_credentials_no_withdrawal_scope(no_self_test_prober)

    assert check.status is CheckStatus.SKIP


def test_no_withdrawal_scope_fails_closed_when_the_prober_raises(
    raising_scope_prober: FakeScopeProber,
) -> None:
    """A prober that raises fails closed rather than passing or skipping."""
    check = check_credentials_no_withdrawal_scope(raising_scope_prober)

    assert check.status is CheckStatus.FAIL


# --- 3. credentials.scope_verifiable (S15) --------------------------------------


def test_scope_verifiable_fails_when_self_test_ran_but_could_not_verify(
    unverified_scope_prober: FakeScopeProber,
) -> None:
    """A self-test that ran but could not verify scope fails."""
    check = check_credentials_scope_verifiable(unverified_scope_prober)

    assert check.check_id == "credentials.scope_verifiable"
    assert check.status is CheckStatus.FAIL


def test_scope_verifiable_passes_when_self_test_verified_the_scope(
    read_only_verified_prober: FakeScopeProber,
) -> None:
    """A self-test that ran and verified scope passes."""
    check = check_credentials_scope_verifiable(read_only_verified_prober)

    assert check.status is CheckStatus.PASS


def test_scope_verifiable_skips_when_no_self_test_is_supported(
    no_self_test_prober: FakeScopeProber,
) -> None:
    """No self-test support means verifiability is unknowable, so it skips."""
    check = check_credentials_scope_verifiable(no_self_test_prober)

    assert check.status is CheckStatus.SKIP


def test_scope_verifiable_fails_closed_when_the_prober_raises(
    raising_scope_prober: FakeScopeProber,
) -> None:
    """A prober that raises fails closed."""
    check = check_credentials_scope_verifiable(raising_scope_prober)

    assert check.status is CheckStatus.FAIL


# --- 4. credentials.trade_key_not_leaked (S5.2) ---------------------------------


def test_trade_key_not_leaked_fails_when_the_var_is_present_in_the_environment() -> (
    None
):
    """A trade-key env var visible in the (injected, never real) environment
    mapping fails.
    """
    prober = EnvTradeKeyLeakProber(
        environ={_TRADE_KEY_VAR: _LEAKED_KEY_VALUE}, var=_TRADE_KEY_VAR
    )

    check = check_credentials_trade_key_not_leaked(prober)

    assert check.check_id == "credentials.trade_key_not_leaked"
    assert check.status is CheckStatus.FAIL


def test_trade_key_not_leaked_fail_detail_never_echoes_the_key_value() -> None:
    """SECURITY: the FAIL detail must never contain the leaked key's value --
    preflight diagnoses a leak without ever repeating key material.
    """
    prober = EnvTradeKeyLeakProber(
        environ={_TRADE_KEY_VAR: _LEAKED_KEY_VALUE}, var=_TRADE_KEY_VAR
    )

    check = check_credentials_trade_key_not_leaked(prober)

    assert _LEAKED_KEY_VALUE not in check.detail


def test_trade_key_not_leaked_passes_when_the_var_is_absent() -> None:
    """A trade-key env var absent from the environment mapping passes."""
    prober = EnvTradeKeyLeakProber(environ={}, var=_TRADE_KEY_VAR)

    check = check_credentials_trade_key_not_leaked(prober)

    assert check.status is CheckStatus.PASS


def test_trade_key_not_leaked_fails_closed_when_the_prober_raises(
    raising_leak_prober: RaisingLeakProber,
) -> None:
    """A prober whose `trade_key_visible()` raises fails closed."""
    check = check_credentials_trade_key_not_leaked(raising_leak_prober)

    assert check.status is CheckStatus.FAIL


def test_env_trade_key_leak_prober_reads_only_its_injected_mapping() -> None:
    """`EnvTradeKeyLeakProber` reads whatever mapping it is given, never the
    real `os.environ` -- so a plain dict lacking the var reports invisible
    even when the real process environment happens to carry that var.
    """
    prober = EnvTradeKeyLeakProber(environ={}, var=_TRADE_KEY_VAR)

    assert prober.trade_key_visible() is False


def test_env_trade_key_leak_prober_reports_visible_when_var_is_in_the_mapping() -> None:
    """`trade_key_visible()` is `True` iff `var` is a key in the given mapping."""
    prober = EnvTradeKeyLeakProber(
        environ={_TRADE_KEY_VAR: _LEAKED_KEY_VALUE}, var=_TRADE_KEY_VAR
    )

    assert prober.trade_key_visible() is True


# --- 5. jurisdiction.markets_eligible (S6.2) ------------------------------------


def test_jurisdiction_markets_eligible_skips_on_an_empty_market_sequence(
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """No cached markets at all skips -- there is nothing yet to judge."""
    dispatcher = AlertDispatcher([], ledger_writer=recording_ledger_writer)

    check = check_jurisdiction_markets_eligible((), dispatcher)

    assert check.check_id == "jurisdiction.markets_eligible"
    assert check.status is CheckStatus.SKIP
    assert "no cached markets" in check.detail.lower()


def test_jurisdiction_markets_eligible_passes_when_every_market_is_eligible(
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """Every market eligible passes, and dispatches no alert at all."""
    dispatcher = AlertDispatcher([], ledger_writer=recording_ledger_writer)
    markets = (make_market("KXFED-24DEC", "eligible"),)

    check = check_jurisdiction_markets_eligible(markets, dispatcher)

    assert check.status is CheckStatus.PASS
    assert recording_ledger_writer.events == []


def test_jurisdiction_markets_eligible_fails_and_alerts_once_per_unknown_ticker(
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """An unknown-jurisdiction market fails AND dispatches exactly one
    `JURISDICTION_UNKNOWN` alert per offending ticker -- two unknown tickers
    here must produce exactly two such alerts, not one and not zero.
    """
    dispatcher = AlertDispatcher([], ledger_writer=recording_ledger_writer)
    markets = (
        make_market("KXFED-24DEC", "eligible"),
        make_market("KXWEA-24DEC", "unknown"),
        make_market("KXOTH-24DEC", "unknown"),
    )

    check = check_jurisdiction_markets_eligible(markets, dispatcher)

    assert check.status is CheckStatus.FAIL
    unknown_alerts = [
        event
        for event in recording_ledger_writer.events
        if event.alert_type is AlertType.JURISDICTION_UNKNOWN
    ]
    assert len(unknown_alerts) == 2


def test_jurisdiction_markets_eligible_fails_on_an_ineligible_market_with_no_alert(
    recording_ledger_writer: RecordingLedgerWriter,
) -> None:
    """An ineligible (not unknown) market fails, but dispatches no alert:
    only `"unknown"` jurisdiction is alert-worthy -- `"ineligible"` is already
    a definite, known verdict.
    """
    dispatcher = AlertDispatcher([], ledger_writer=recording_ledger_writer)
    markets = (make_market("KXBAN-24DEC", "ineligible"),)

    check = check_jurisdiction_markets_eligible(markets, dispatcher)

    assert check.status is CheckStatus.FAIL
    assert recording_ledger_writer.events == []


# --- 6. secrets.files_not_world_readable (S15) -----------------------------------


def test_secrets_files_not_world_readable_skips_on_an_empty_path_sequence() -> None:
    """No configured secrets files at all skips."""
    check = check_secrets_files_not_world_readable(())

    assert check.check_id == "secrets.files_not_world_readable"
    assert check.status is CheckStatus.SKIP


def test_secrets_files_not_world_readable_passes_at_mode_0600(tmp_path: Path) -> None:
    """A secrets file at owner-only `0o600` passes."""
    secret = tmp_path / "trade_key.pem"
    secret.write_text("secret material", encoding="utf-8")
    os.chmod(secret, 0o600)

    check = check_secrets_files_not_world_readable((secret,))

    assert check.status is CheckStatus.PASS


@pytest.mark.parametrize("world_readable_mode", [0o644, 0o640])
def test_secrets_files_not_world_readable_fails_when_group_or_other_bits_are_set(
    tmp_path: Path, world_readable_mode: int
) -> None:
    """Any group- or other-permission bit (`mode & 0o077`) fails."""
    secret = tmp_path / "trade_key.pem"
    secret.write_text("secret material", encoding="utf-8")
    os.chmod(secret, world_readable_mode)

    check = check_secrets_files_not_world_readable((secret,))

    assert check.status is CheckStatus.FAIL


def test_secrets_files_not_world_readable_fails_closed_on_a_missing_path(
    tmp_path: Path,
) -> None:
    """A configured path that does not exist fails closed -- never SKIP,
    never PASS.
    """
    missing = tmp_path / "does-not-exist.pem"
    assert not missing.exists()

    check = check_secrets_files_not_world_readable((missing,))

    assert check.status is CheckStatus.FAIL


def test_secrets_files_not_world_readable_fails_if_any_one_of_several_is_bad(
    tmp_path: Path,
) -> None:
    """With several configured paths, one world-readable file fails the whole
    check even though the others are properly locked down.
    """
    good = tmp_path / "good.pem"
    good.write_text("ok", encoding="utf-8")
    os.chmod(good, 0o600)
    bad = tmp_path / "bad.pem"
    bad.write_text("leaky", encoding="utf-8")
    os.chmod(bad, 0o644)

    check = check_secrets_files_not_world_readable((good, bad))

    assert check.status is CheckStatus.FAIL


# --- 7. credentials.llm_budgets_configured (S5.2) --------------------------------


def test_llm_budgets_configured_passes_on_the_spec_default_config() -> None:
    """The built-in SPEC S16 default budget (per_forecast_micros=3000000,
    per_day_micros=20000000) passes.
    """
    config = load_default_config()

    check = check_credentials_llm_budgets_configured(config)

    assert check.check_id == "credentials.llm_budgets_configured"
    assert check.status is CheckStatus.PASS


def test_llm_budgets_configured_fails_when_per_forecast_budget_is_zero() -> None:
    """A zeroed-out `per_forecast_micros` budget fails."""
    config = load_default_config()
    zero_budget = replace(config.forecast.budget, per_forecast_micros=0)
    config = replace(config, forecast=replace(config.forecast, budget=zero_budget))

    check = check_credentials_llm_budgets_configured(config)

    assert check.status is CheckStatus.FAIL


def test_llm_budgets_configured_fails_when_per_day_budget_is_zero() -> None:
    """A zeroed-out `per_day_micros` budget fails."""
    config = load_default_config()
    zero_budget = replace(config.forecast.budget, per_day_micros=0)
    config = replace(config, forecast=replace(config.forecast, budget=zero_budget))

    check = check_credentials_llm_budgets_configured(config)

    assert check.status is CheckStatus.FAIL
