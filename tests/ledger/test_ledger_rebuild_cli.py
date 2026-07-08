"""Tests for the `windbreak rebuild` CLI subcommand (issue #13).

Pins the CLI-level contract: `rebuild` requires `--ledger-path` and
`--output-dir` Path arguments, `main()` dispatches to `rebuild_command`
and surfaces its exit code (0 clean, 1 on `ChainIntegrityError` with the
offending `sequence_number=<n>` on stderr), and the pre-existing `run`
subcommand's parsing is unaffected by this addition.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from windbreak.ledger.events import ConfigLoaded
from windbreak.ledger.store import SqliteLedgerStore
from windbreak.main import build_parser, main

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime


def test_build_parser_parses_rebuild_with_required_args() -> None:
    """`rebuild --ledger-path X --output-dir Y` parses both as Path objects."""
    parser = build_parser()

    args = parser.parse_args(
        ["rebuild", "--ledger-path", "ledger.db", "--output-dir", "out"]
    )

    assert args.command == "rebuild"
    assert args.ledger_path == Path("ledger.db")
    assert args.output_dir == Path("out")


def test_build_parser_rebuild_requires_ledger_path() -> None:
    """Omitting `--ledger-path` is a usage error, reported via SystemExit(2)."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["rebuild", "--output-dir", "out"])

    assert exc_info.value.code == 2


def test_build_parser_rebuild_requires_output_dir() -> None:
    """Omitting `--output-dir` is a usage error, reported via SystemExit(2)."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["rebuild", "--ledger-path", "ledger.db"])

    assert exc_info.value.code == 2


def test_build_parser_rebuild_requires_both_args_when_neither_is_given() -> None:
    """`rebuild` with no flags at all is a usage error, reported via SystemExit(2)."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["rebuild"])

    assert exc_info.value.code == 2


def test_main_rebuild_returns_zero_on_clean_ledger(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """`main` returns 0 and produces both read models for an untampered ledger."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))
    store.close()

    exit_code = main(
        ["rebuild", "--ledger-path", str(db_path), "--output-dir", str(output_dir)]
    )

    assert exit_code == 0
    assert (output_dir / "config_versions.json").exists()
    assert (output_dir / "mode_history.json").exists()


def test_main_rebuild_returns_one_on_tampered_ledger_and_reports_sequence_number(
    tmp_path: Path,
    deterministic_clock: Callable[[], datetime],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main` returns 1 and prints `sequence_number=<n>` to stderr on corruption."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE ledger SET event_hash = ? WHERE sequence_number = 1",
            ("0" * 64,),
        )
        conn.commit()
    finally:
        conn.close()

    exit_code = main(
        ["rebuild", "--ledger-path", str(db_path), "--output-dir", str(output_dir)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "sequence_number=1" in captured.err


def test_build_parser_run_subcommand_parsing_is_unchanged() -> None:
    """Adding `rebuild` must not disturb the pre-existing `run` subcommand parsing."""
    args = build_parser().parse_args(["run"])

    assert args.command == "run"
    assert args.heartbeat_interval == 5.0
    assert args.max_beats is None


def test_build_parser_run_subcommand_still_rejects_negative_heartbeat_interval() -> (
    None
):
    """Regression guard: `run`'s validation behavior is untouched by `rebuild`."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run", "--heartbeat-interval", "-1"])

    assert exc_info.value.code == 2
