"""Failing-first tests for the `windbreak preflight` CLI (issue #56, RED).

`windbreak.preflight` does not exist yet, so `windbreak.main` fails to wire the
`preflight` subcommand and these tests fail collection or at call time with an
error tied to the missing package/subcommand -- the expected Gate 1 RED state
for issue #56.

CLI surface pinned here: `windbreak preflight --fixture-dir DIR [--json]
[--config PATH] [--secrets-file PATH ...]`, printing its report to stdout
(never stderr -- that is reserved for the JSON structured logging pipeline)
and exiting 0 only when every non-SKIP check is PASS.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

import pytest

from windbreak.main import main

if TYPE_CHECKING:
    from pathlib import Path

#: The trade-key environment variable the leak check inspects; cleared for every
#: CLI test below so a value inherited from the developer's or CI's real
#: environment never spuriously trips (or masks) the check.
_TRADE_KEY_VAR = "WINDBREAK_TRADE_KEY"

#: The seven check ids every `windbreak preflight` invocation must report,
#: regardless of which pass/fail/skip each individually lands on.
_EXPECTED_CHECK_IDS = frozenset(
    {
        "exchange.reachable_readonly",
        "credentials.no_withdrawal_scope",
        "credentials.scope_verifiable",
        "credentials.trade_key_not_leaked",
        "jurisdiction.markets_eligible",
        "secrets.files_not_world_readable",
        "credentials.llm_budgets_configured",
    }
)


@pytest.fixture(autouse=True)
def _clear_trade_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the trade-key variable is absent from the environment.

    The `windbreak preflight` CLI wires the trade-key-leak check to the real
    process environment, so without this every CLI test's exit code would
    depend on whether ``WINDBREAK_TRADE_KEY`` happens to be exported on the
    host. Each test that wants the leak *present* re-sets it explicitly.

    Args:
        monkeypatch: The pytest environment patcher.
    """
    monkeypatch.delenv(_TRADE_KEY_VAR, raising=False)


def test_cli_all_eligible_fixture_exits_zero_with_no_fail_rows(
    all_eligible_fixture_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`windbreak preflight --fixture-dir <all-eligible fixture>` (no
    world-readable secrets configured) exits 0, prints at least one PASS
    row, and never prints a FAIL row or a FAILED summary.
    """
    exit_code = main(["preflight", "--fixture-dir", str(all_eligible_fixture_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "PASS" in captured.out
    assert "FAIL" not in captured.out
    assert "FAILED" not in captured.out


def test_cli_world_readable_secrets_file_exits_one_with_a_fail_row(
    all_eligible_fixture_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `--secrets-file` at a world-readable mode (`0o644`) fails the
    `secrets.files_not_world_readable` check, driving the whole run to a
    nonzero exit and a `FAIL` row naming that check.
    """
    secret = tmp_path / "trade_key.pem"
    secret.write_text("secret material", encoding="utf-8")
    os.chmod(secret, 0o644)

    exit_code = main(
        [
            "preflight",
            "--fixture-dir",
            str(all_eligible_fixture_dir),
            "--secrets-file",
            str(secret),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    fail_lines = [
        line
        for line in captured.out.splitlines()
        if "secrets.files_not_world_readable" in line
    ]
    assert len(fail_lines) == 1
    assert "FAIL" in fail_lines[0]
    assert re.search(r"preflight FAILED \(\d+ failure", captured.out)


def test_cli_multiple_locked_down_secrets_files_all_pass(
    all_eligible_fixture_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--secrets-file` is repeatable: two properly-locked-down (`0o600`)
    secrets files still exit 0.
    """
    first = tmp_path / "trade_key.pem"
    first.write_text("secret material", encoding="utf-8")
    os.chmod(first, 0o600)
    second = tmp_path / "llm_key.pem"
    second.write_text("other secret material", encoding="utf-8")
    os.chmod(second, 0o600)

    exit_code = main(
        [
            "preflight",
            "--fixture-dir",
            str(all_eligible_fixture_dir),
            "--secrets-file",
            str(first),
            "--secrets-file",
            str(second),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "FAIL" not in captured.out


def test_cli_json_flag_emits_a_payload_naming_all_seven_checks(
    all_eligible_fixture_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` prints a single JSON document to stdout whose `checks` entries
    name exactly the seven documented check ids and whose `exit_code` matches
    the process's own exit code.
    """
    exit_code = main(
        ["preflight", "--fixture-dir", str(all_eligible_fixture_dir), "--json"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert {entry["check_id"] for entry in payload["checks"]} == _EXPECTED_CHECK_IDS
    assert payload["exit_code"] == exit_code == 0


def test_cli_trade_key_in_environment_fails_and_never_echoes_its_value(
    all_eligible_fixture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A trade key present in the process environment trips the leak check.

    The run exits nonzero with a `FAIL` row for
    `credentials.trade_key_not_leaked`, and -- critically -- the key's secret
    *value* never appears in the printed report (SPEC S5.2).
    """
    planted_value = "fake-trade-cred-not-real"
    monkeypatch.setenv(_TRADE_KEY_VAR, planted_value)

    exit_code = main(["preflight", "--fixture-dir", str(all_eligible_fixture_dir)])

    captured = capsys.readouterr()
    assert exit_code == 1
    fail_lines = [
        line
        for line in captured.out.splitlines()
        if "credentials.trade_key_not_leaked" in line
    ]
    assert len(fail_lines) == 1
    assert "FAIL" in fail_lines[0]
    assert planted_value not in captured.out
