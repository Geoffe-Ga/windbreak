"""Failing-first tests for the `--process` CLI flag (issue #15, RED).

Issue #15 gives `hedgekit run` a `--process` flag selecting which of the
four SPEC processes (pipeline, riskkernel, order_gateway, dashboard) this
invocation represents, and threads that choice through to `run_loop` as a
keyword-only `component` parameter so every heartbeat and shutdown log line
carries `extra={"component": component}`.

None of this exists yet:

- `hedgekit.main.PROCESS_CHOICES` is undefined, so the import below fails
  the whole module at collection with `ImportError`.
- The `run` subparser has no `--process` option.
- `run_loop` accepts no `component` keyword.

Once the import above is satisfied by the implementation specialist, the
remaining tests are expected to fail with `TypeError` (unexpected keyword
`component`), `SystemExit` code mismatches, or `AssertionError` (the
`component` field missing/wrong on log records) until the flag is fully
wired through `main()`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pytest

from hedgekit.main import PROCESS_CHOICES, build_parser, main, run_loop


def test_process_choices_constant_matches_the_four_spec_processes() -> None:
    """PROCESS_CHOICES lists exactly the four SPEC processes, in this order."""
    assert PROCESS_CHOICES == ("pipeline", "riskkernel", "order_gateway", "dashboard")


@pytest.mark.parametrize("process", PROCESS_CHOICES)
def test_build_parser_accepts_each_process_choice(process: str) -> None:
    """`--process <choice>` parses for each of the four registered processes."""
    args = build_parser().parse_args(["run", "--process", process])

    assert args.process == process


def test_build_parser_run_process_defaults_to_pipeline() -> None:
    """`run` with no `--process` flag defaults to the "pipeline" process."""
    args = build_parser().parse_args(["run"])

    assert args.process == "pipeline"


def test_build_parser_rejects_hyphenated_order_gateway() -> None:
    """The gateway token is underscore-separated; the hyphen spelling is invalid.

    `order-gateway` is not in `PROCESS_CHOICES` -- only `order_gateway` is --
    so argparse must reject it as a usage error, not silently normalize it.
    """
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run", "--process", "order-gateway"])

    assert exc_info.value.code == 2


def test_build_parser_rejects_unknown_process() -> None:
    """An unregistered process name is a usage error, not silently accepted."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["run", "--process", "foo"])

    assert exc_info.value.code == 2


def test_main_run_still_emits_byte_identical_heartbeat_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The human-readable heartbeat message is unchanged by the --process flag.

    Adding `--process`/`component` must not alter the rendered message text
    that issue #10 pinned -- only the structured `extra` fields change. Read
    through `capsys` (not `caplog`) because `main()` calls
    `configure_logging(force=True)`, which detaches caplog's handler; the
    real observable contract is the JSON line on stderr (as in `test_cli.py`).
    """
    exit_code = main(["run", "--heartbeat-interval", "0", "--max-beats", "1"])

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.err.splitlines() if line]
    heartbeat_messages = [
        payload["msg"] for payload in payloads if "heartbeat" in payload["msg"]
    ]
    assert exit_code == 0
    assert heartbeat_messages == ["mode=RESEARCH heartbeat seq=1"]


@pytest.mark.parametrize("component", PROCESS_CHOICES)
def test_run_loop_stamps_heartbeat_and_shutdown_with_component(
    component: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Both the heartbeat and shutdown records carry the requested component.

    Asserting both call sites (not just the heartbeat) kills mutants that
    thread `component` through one log call but drop it from the other.
    """
    caplog.set_level(logging.INFO)

    run_loop(0, max_beats=1, component=component)

    heartbeat_records = [
        record for record in caplog.records if "heartbeat" in record.message
    ]
    shutdown_records = [
        record
        for record in caplog.records
        if record.message.startswith("shutdown reason=")
    ]
    assert len(heartbeat_records) == 1
    assert len(shutdown_records) == 1
    assert heartbeat_records[0].component == component
    assert shutdown_records[0].component == component


def test_run_loop_component_defaults_to_pipeline() -> None:
    """Calling `run_loop` with no `component` keyword defaults to "pipeline"."""
    caplog_records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            caplog_records.append(record)

    logger = logging.getLogger("hedgekit")
    handler = _Collector()
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        run_loop(0, max_beats=1)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)

    heartbeat_records = [
        record for record in caplog_records if "heartbeat" in record.getMessage()
    ]
    assert len(heartbeat_records) == 1
    assert heartbeat_records[0].component == "pipeline"


def test_main_run_default_process_stamps_pipeline_component_in_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no --process flag, JSON log lines carry component == "pipeline".

    `main()` routes logging through `configure_logging`'s `JsonFormatter`,
    which surfaces the `component` extra as a top-level field, so this
    asserts the end-to-end wiring rather than just `run_loop` in isolation.
    """
    exit_code = main(["run", "--heartbeat-interval", "0", "--max-beats", "1"])

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line]
    payloads = [json.loads(line) for line in lines]

    assert exit_code == 0
    heartbeat_payload = next(
        payload for payload in payloads if "seq=1" in payload["msg"]
    )
    assert heartbeat_payload["component"] == "pipeline"


@pytest.mark.parametrize("process", PROCESS_CHOICES)
def test_main_run_with_process_flag_stamps_matching_component_in_json(
    process: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--process <name>` end-to-end stamps every log line with that component."""
    exit_code = main(
        [
            "run",
            "--process",
            process,
            "--heartbeat-interval",
            "0",
            "--max-beats",
            "1",
        ]
    )

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line]
    payloads = [json.loads(line) for line in lines]

    assert exit_code == 0
    heartbeat_payload = next(
        payload for payload in payloads if "seq=1" in payload["msg"]
    )
    shutdown_payload = next(
        payload for payload in payloads if "shutdown reason=" in payload["msg"]
    )
    assert heartbeat_payload["component"] == process
    assert shutdown_payload["component"] == process


@pytest.mark.timeout(30)
def test_main_process_riskkernel_smoke_via_subprocess() -> None:
    """`python -m hedgekit run --process riskkernel` exits 0 and logs the component.

    Bounded via `--max-beats 1` and `--heartbeat-interval 0` -- no signal
    races, no unbounded loop. This is the sole subprocess-level test in this
    module; every other assertion above runs in-process for speed.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hedgekit",
            "run",
            "--process",
            "riskkernel",
            "--max-beats",
            "1",
            "--heartbeat-interval",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    lines = [line for line in result.stderr.splitlines() if line]
    payloads = [json.loads(line) for line in lines]
    assert any(payload.get("component") == "riskkernel" for payload in payloads)
