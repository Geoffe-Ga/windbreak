"""Tests for the on_beat snapshot wiring in hedgekit.main (issue #16).

`run_loop` gains a keyword-only `on_beat: Callable[[int], None] | None = None`
hook invoked once per beat with the 1-based sequence number; `main`'s `run`
subcommand gains `--snapshot-fixture-dir` (default None = off) that wires a
`FakeExchange` + `StubScreener` + `LoggingEventLedgerWriter` into that hook.
Kept out of `test_main.py`/`test_run_loop.py` to avoid cross-lane conflicts
with issue #16's connector work.

`run_loop` does not accept `on_beat` yet (TypeError: unexpected keyword
argument) and `--snapshot-fixture-dir` is not a recognized CLI flag yet
(argparse SystemExit(2)) -- both are the expected Gate 1 RED failures for
issue #16, in addition to `hedgekit.connector`/`hedgekit.screener` not
existing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from hedgekit.main import main, run_loop

if TYPE_CHECKING:
    import pytest

#: The shared exchange fixtures, resolved relative to this file so the test
#: does not depend on the pytest invocation's current working directory.
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "exchange"


def test_run_loop_invokes_on_beat_once_per_beat_with_1_based_seq() -> None:
    """`on_beat` is called once per beat, in order, with a 1-based sequence."""
    seen: list[int] = []

    run_loop(0, max_beats=3, on_beat=seen.append)

    assert seen == [1, 2, 3]


def test_run_loop_default_on_beat_is_none_and_behavior_is_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Omitting `on_beat` leaves the existing heartbeat behavior unchanged."""
    caplog.set_level(logging.INFO)

    run_loop(0, max_beats=1)

    heartbeat_lines = [
        record.message for record in caplog.records if "heartbeat" in record.message
    ]
    assert heartbeat_lines == ["mode=RESEARCH heartbeat seq=1"]


def test_run_with_snapshot_fixture_dir_emits_snapshot_and_decision_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--snapshot-fixture-dir` wires the fake exchange into the beat loop."""
    exit_code = main(
        [
            "run",
            "--max-beats",
            "1",
            "--heartbeat-interval",
            "0",
            "--snapshot-fixture-dir",
            str(_FIXTURE_DIR),
        ]
    )

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line]
    payloads = [json.loads(line) for line in lines]
    joined = "\n".join(json.dumps(payload) for payload in payloads)

    assert exit_code == 0
    assert "MARKET_SNAPSHOT" in joined
    assert "SCREEN_DECISION" in joined


def test_run_without_snapshot_fixture_dir_emits_no_snapshot_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting the flag (the default) means snapshotting stays off."""
    exit_code = main(["run", "--max-beats", "1", "--heartbeat-interval", "0"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "MARKET_SNAPSHOT" not in captured.err
    assert "SCREEN_DECISION" not in captured.err
