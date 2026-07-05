"""Tests for the `hedgekit run` CLI (issue #10).

Covers argument parsing (`build_parser`) in isolation, plus one
end-to-end smoke test through `main()` that pins the observable
contract: heartbeats are logged to stderr and the process returns 0
after a bounded number of beats.
"""

from __future__ import annotations

import pytest

from hedgekit.main import build_parser, main


def test_build_parser_parses_run_with_defaults() -> None:
    """`run` with no flags uses the documented defaults."""
    args = build_parser().parse_args(["run"])

    assert args.command == "run"
    assert args.heartbeat_interval == 5.0
    assert args.max_beats is None


def test_build_parser_parses_custom_heartbeat_interval() -> None:
    """`--heartbeat-interval 0.5` overrides the 5.0s default."""
    args = build_parser().parse_args(["run", "--heartbeat-interval", "0.5"])

    assert args.heartbeat_interval == 0.5


def test_build_parser_parses_max_beats() -> None:
    """`--max-beats` accepts an integer bound on the number of heartbeats."""
    args = build_parser().parse_args(["run", "--max-beats", "2"])

    assert args.max_beats == 2


def test_build_parser_rejects_negative_heartbeat_interval() -> None:
    """A negative interval is not a valid heartbeat cadence.

    argparse reports usage errors via SystemExit(2), not a Python
    exception the caller could accidentally swallow.
    """
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run", "--heartbeat-interval", "-1"])

    assert exc_info.value.code == 2


def test_build_parser_rejects_negative_max_beats() -> None:
    """A negative beat budget is meaningless, so it is a usage error.

    Validated for parity with ``--heartbeat-interval``; a silently accepted
    negative would behave like ``0`` and stop before the first beat.
    """
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run", "--max-beats", "-1"])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf"])
def test_build_parser_rejects_non_finite_heartbeat_interval(bad_value: str) -> None:
    """Non-finite intervals cannot define a cadence, so they are rejected.

    ``stop_event.wait(nan)`` is ill-defined, so ``nan``/``inf`` must fail at
    parse time via SystemExit(2) rather than reaching the loop.
    """
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run", "--heartbeat-interval", bad_value])

    assert exc_info.value.code == 2


def test_build_parser_requires_a_command() -> None:
    """Invoking the CLI with no subcommand is a usage error, not a no-op."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([])

    assert exc_info.value.code == 2


def test_build_parser_rejects_unknown_command() -> None:
    """An unrecognized subcommand is a usage error."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["fly-to-the-moon"])

    assert exc_info.value.code == 2


def test_main_run_emits_heartbeats_and_max_beats_shutdown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: `main` drives the loop to completion and returns 0.

    Interval 0 keeps the test fast; max-beats 2 bounds it deterministically.
    """
    exit_code = main(["run", "--heartbeat-interval", "0", "--max-beats", "2"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "seq=1" in captured.err
    assert "seq=2" in captured.err
    assert "shutdown reason=max_beats" in captured.err
