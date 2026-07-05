"""Tests for the `hedgekit run --config` flag (issue #11).

Exercises config loading through the CLI's `run` subcommand: the
tracer invariant (no `--config` still heartbeats on defaults), a valid
file (loads, logs its hash, still heartbeats), a missing file (fatal,
no heartbeat), and a file naming an unknown key (fatal, names the
path). Also pins `build_parser`'s `--config` flag in isolation.

The checked-in SPEC S16 example lives under `tests/config/`; this
module is at `tests/` root (matching the flat CLI-test placement of
its siblings) so it reaches into that directory by path rather than
via the `tests/config/conftest.py` fixtures, which are scoped to that
subtree only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from hedgekit.main import build_parser, main

if TYPE_CHECKING:
    import pytest

#: The checked-in SPEC S16 example, shared with tests/config/conftest.py.
_SPEC16_PATH = Path(__file__).parent / "config" / "spec16_example.yaml"

#: Matches the lowercase hex SHA-256 config_hash logged on a valid load.
_HEX64_PATTERN = re.compile(r"[0-9a-f]{64}")


def test_run_without_config_still_heartbeats_on_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Tracer invariant: `run` with no --config still heartbeats on defaults."""
    exit_code = main(["run", "--heartbeat-interval", "0", "--max-beats", "2"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "seq=1" in captured.err
    assert "seq=2" in captured.err


def test_run_with_valid_config_loads_and_heartbeats(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid --config path loads, logs its hash, and still heartbeats."""
    exit_code = main(
        [
            "run",
            "--config",
            str(_SPEC16_PATH),
            "--heartbeat-interval",
            "0",
            "--max-beats",
            "1",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "config loaded" in captured.err
    assert _HEX64_PATTERN.search(captured.err) is not None
    assert "seq=1" in captured.err


def test_run_with_missing_config_exits_1_with_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A nonexistent --config path is fatal and never reaches a heartbeat."""
    missing_path = tmp_path / "nope.yaml"

    exit_code = main(["run", "--config", str(missing_path), "--max-beats", "1"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "FATAL:" in captured.err
    assert "seq=" not in captured.err


def test_run_with_unknown_key_config_exits_1_naming_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown key in --config is fatal and names the offending path."""
    mapping = yaml.safe_load(_SPEC16_PATH.read_text(encoding="utf-8"))
    mapping["risk"]["max_leverage"] = 5
    bad_config_path = tmp_path / "bad_config.yaml"
    bad_config_path.write_text(yaml.safe_dump(mapping), encoding="utf-8")

    exit_code = main(["run", "--config", str(bad_config_path), "--max-beats", "1"])

    captured = capsys.readouterr()
    # The fatal diagnostic is a JSON log record (issue #14's structured
    # logging), so decode each line's ``msg`` rather than raw-matching the
    # stderr text: ``json.dumps`` ASCII-escapes the section sign on the wire
    # (as a ``\\u00a7`` sequence), and ``json.loads`` restores it here.
    messages = "\n".join(
        str(json.loads(line).get("msg", ""))
        for line in captured.err.splitlines()
        if line
    )

    assert exit_code == 1
    assert "FATAL:" in messages
    assert "risk.max_leverage" in messages
    assert "unknown keys are fatal per SPEC §16" in messages
    assert "seq=" not in captured.err


def test_run_with_config_emits_structured_json_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run --config X` routes its config diagnostics through JSON logging.

    Pins the composition of the SPEC §16 loader (issue #11) with structured
    logging (issue #14): the ``config loaded`` diagnostic is a JSON record,
    not a plain-text line -- it parses, carries ``level == "INFO"``, and
    embeds the config hash inside its ``msg`` field.
    """
    exit_code = main(
        [
            "run",
            "--config",
            str(_SPEC16_PATH),
            "--heartbeat-interval",
            "0",
            "--max-beats",
            "1",
        ]
    )

    captured = capsys.readouterr()
    payloads = [json.loads(line) for line in captured.err.splitlines() if line]

    assert exit_code == 0
    config_line = next(
        payload for payload in payloads if "config loaded" in str(payload.get("msg"))
    )
    assert config_line["level"] == "INFO"
    assert _HEX64_PATTERN.search(config_line["msg"]) is not None


def test_build_parser_accepts_config_flag() -> None:
    """`--config` parses to a pathlib.Path, defaulting to None when omitted."""
    args = build_parser().parse_args(["run", "--config", "x.yaml"])

    assert isinstance(args.config, Path)
    assert args.config == Path("x.yaml")

    default_args = build_parser().parse_args(["run"])
    assert default_args.config is None
