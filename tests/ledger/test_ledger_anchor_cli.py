"""Tests for the `windbreak anchor` / `windbreak verify` CLI subcommands (issue #75).

Mirrors `tests/ledger/test_ledger_rebuild_cli.py`'s style: pins the CLI-level
contract that `anchor` and `verify` each require `--ledger-path` and
`--anchor-path` Path arguments, that `main()` dispatches to
`anchor_command`/`verify_command` and surfaces their exit codes (0 clean, 1
on failure with the offending detail on stderr), and that the pre-existing
`run`/`rebuild` subcommands' parsing is unaffected by this addition.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from windbreak.ledger.anchor import anchor_head
from windbreak.ledger.events import ModeHeartbeat
from windbreak.ledger.store import SqliteLedgerStore
from windbreak.main import build_parser, main

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime


def test_build_parser_parses_anchor_with_required_args() -> None:
    """`anchor --ledger-path X --anchor-path Y` parses both as Path objects."""
    parser = build_parser()

    args = parser.parse_args(
        ["anchor", "--ledger-path", "ledger.db", "--anchor-path", "anchors.jsonl"]
    )

    assert args.command == "anchor"
    assert args.ledger_path == Path("ledger.db")
    assert args.anchor_path == Path("anchors.jsonl")


def test_build_parser_anchor_requires_ledger_path() -> None:
    """Omitting `--ledger-path` on `anchor` is a usage error (SystemExit(2))."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["anchor", "--anchor-path", "anchors.jsonl"])

    assert exc_info.value.code == 2


def test_build_parser_anchor_requires_anchor_path() -> None:
    """Omitting `--anchor-path` on `anchor` is a usage error (SystemExit(2))."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["anchor", "--ledger-path", "ledger.db"])

    assert exc_info.value.code == 2


def test_build_parser_anchor_requires_both_args_when_neither_is_given() -> None:
    """`anchor` with no flags at all is a usage error (SystemExit(2))."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["anchor"])

    assert exc_info.value.code == 2


def test_build_parser_parses_verify_with_required_args() -> None:
    """`verify --ledger-path X --anchor-path Y` parses both as Path objects."""
    parser = build_parser()

    args = parser.parse_args(
        ["verify", "--ledger-path", "ledger.db", "--anchor-path", "anchors.jsonl"]
    )

    assert args.command == "verify"
    assert args.ledger_path == Path("ledger.db")
    assert args.anchor_path == Path("anchors.jsonl")


def test_build_parser_verify_requires_ledger_path() -> None:
    """Omitting `--ledger-path` on `verify` is a usage error (SystemExit(2))."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["verify", "--anchor-path", "anchors.jsonl"])

    assert exc_info.value.code == 2


def test_build_parser_verify_requires_anchor_path() -> None:
    """Omitting `--anchor-path` on `verify` is a usage error (SystemExit(2))."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["verify", "--ledger-path", "ledger.db"])

    assert exc_info.value.code == 2


def test_build_parser_verify_requires_both_args_when_neither_is_given() -> None:
    """`verify` with no flags at all is a usage error (SystemExit(2))."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["verify"])

    assert exc_info.value.code == 2


def test_main_anchor_returns_zero_and_writes_one_anchor_line(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """`main(["anchor", ...])` returns 0 and appends exactly one anchor line."""
    db_path = tmp_path / "ledger.db"
    anchor_path = tmp_path / "anchors.jsonl"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.close()

    exit_code = main(
        ["anchor", "--ledger-path", str(db_path), "--anchor-path", str(anchor_path)]
    )

    assert exit_code == 0
    assert len(anchor_path.read_text(encoding="utf-8").splitlines()) == 1


def test_main_anchor_returns_one_on_tampered_ledger_and_reports_sequence_number(
    tmp_path: Path,
    deterministic_clock: Callable[[], datetime],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["anchor", ...])` returns 1 and reports the tampered sequence position."""
    db_path = tmp_path / "ledger.db"
    anchor_path = tmp_path / "anchors.jsonl"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE ledger SET event_hash = ? WHERE sequence_number = 1", ("0" * 64,)
        )
        conn.commit()
    finally:
        conn.close()

    exit_code = main(
        ["anchor", "--ledger-path", str(db_path), "--anchor-path", str(anchor_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "sequence_number=1" in captured.err
    assert not anchor_path.exists()


def test_main_verify_returns_zero_on_clean_anchored_chain(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """`main(["verify", ...])` returns 0 for an untampered, anchored chain."""
    db_path = tmp_path / "ledger.db"
    anchor_path = tmp_path / "anchors.jsonl"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.close()
    anchor_head(db_path, anchor_path)

    exit_code = main(
        ["verify", "--ledger-path", str(db_path), "--anchor-path", str(anchor_path)]
    )

    assert exit_code == 0


def test_main_verify_returns_one_on_tampered_chain_and_reports_sequence_number(
    tmp_path: Path,
    deterministic_clock: Callable[[], datetime],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["verify", ...])` returns 1 and prints `sequence_number=<n>` on a
    plain chain-integrity break (unrelated to anchoring: verify_chain fails first).
    """
    db_path = tmp_path / "ledger.db"
    anchor_path = tmp_path / "anchors.jsonl"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.close()
    anchor_head(db_path, anchor_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE ledger SET event_hash = ? WHERE sequence_number = 1", ("0" * 64,)
        )
        conn.commit()
    finally:
        conn.close()

    exit_code = main(
        ["verify", "--ledger-path", str(db_path), "--anchor-path", str(anchor_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "sequence_number=1" in captured.err


def test_main_verify_returns_one_on_mismatched_anchor_and_reports_sequence_number(
    tmp_path: Path,
    deterministic_clock: Callable[[], datetime],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["verify", ...])` returns 1 and reports the mismatched anchor's
    sequence_number on a tail-rewrite a bare chain check alone would miss.
    """
    db_path = tmp_path / "ledger.db"
    anchor_path = tmp_path / "anchors.jsonl"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    for beat in range(1, 4):
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=beat))
    store.close()
    anchor_head(db_path, anchor_path)  # anchors seq=3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM ledger WHERE sequence_number >= 2")
        conn.commit()
    finally:
        conn.close()

    exit_code = main(
        ["verify", "--ledger-path", str(db_path), "--anchor-path", str(anchor_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "sequence_number=3" in captured.err


def test_main_verify_returns_one_on_missing_anchor_file(
    tmp_path: Path,
    deterministic_clock: Callable[[], datetime],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["verify", ...])` returns 1 with a stderr message when the anchor
    file itself is missing (fail closed: no anchors is never "verified").
    """
    db_path = tmp_path / "ledger.db"
    anchor_path = tmp_path / "does_not_exist.jsonl"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.close()

    exit_code = main(
        ["verify", "--ledger-path", str(db_path), "--anchor-path", str(anchor_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.strip() != ""


def test_build_parser_run_and_rebuild_subcommand_parsing_is_unchanged() -> None:
    """Adding `anchor`/`verify` must not disturb the pre-existing subcommands."""
    run_args = build_parser().parse_args(["run"])
    rebuild_args = build_parser().parse_args(
        ["rebuild", "--ledger-path", "ledger.db", "--output-dir", "out"]
    )

    assert run_args.command == "run"
    assert run_args.heartbeat_interval == 5.0
    assert run_args.max_beats is None
    assert rebuild_args.command == "rebuild"
    assert rebuild_args.ledger_path == Path("ledger.db")
