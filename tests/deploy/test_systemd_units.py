"""Failing-first tests for the systemd unit skeletons (issue #15, RED).

`deploy/systemd/` does not exist yet, so every parametrized case below fails
at test-body time with `FileNotFoundError` (surfaced by `configparser` as it
tries to read a nonexistent unit file). Once the four unit files land, these
tests pin their shared contract: each declares `[Unit]`, `[Service]`, and
`[Install]` sections, restarts `on-failure`, and its `ExecStart` invokes
`hedgekit run --process <token>` for that unit's process -- the same
underscored `order_gateway` token used by the CLI and the compose skeleton.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYSTEMD_DIR = _REPO_ROOT / "deploy" / "systemd"

#: Maps each expected unit filename to its `--process` CLI token.
_UNIT_TO_CLI_TOKEN = {
    "hedgekit-pipeline.service": "pipeline",
    "hedgekit-riskkernel.service": "riskkernel",
    "hedgekit-order-gateway.service": "order_gateway",
    "hedgekit-dashboard.service": "dashboard",
}


def _parse_unit(filename: str) -> configparser.ConfigParser:
    """Parse a systemd unit file, preserving its case-sensitive key names.

    Args:
        filename: The unit file's name under `deploy/systemd/`.

    Returns:
        A `ConfigParser` with `optionxform` disabled so `ExecStart` is not
        lowercased to `execstart` -- systemd unit keys are case-sensitive.

    Raises:
        FileNotFoundError: If the unit file does not exist under
            `deploy/systemd/` (the expected RED state before issue #15
            lands the deploy skeleton).
    """
    unit_path = _SYSTEMD_DIR / filename
    if not unit_path.is_file():
        raise FileNotFoundError(unit_path)
    parser = configparser.ConfigParser()
    parser.optionxform = str  # type: ignore[method-assign]
    parser.read(unit_path, encoding="utf-8")
    return parser


@pytest.mark.parametrize(("filename", "cli_token"), sorted(_UNIT_TO_CLI_TOKEN.items()))
def test_unit_declares_required_sections(filename: str, cli_token: str) -> None:
    """Every unit has the three sections systemd requires to be usable.

    `cli_token` is unused here (this test only checks section presence) but
    is threaded through so the parametrize id names the process, not just
    the filename.
    """
    del cli_token
    parser = _parse_unit(filename)

    assert parser.has_section("Unit")
    assert parser.has_section("Service")
    assert parser.has_section("Install")


@pytest.mark.parametrize(("filename", "cli_token"), sorted(_UNIT_TO_CLI_TOKEN.items()))
def test_unit_restarts_on_failure(filename: str, cli_token: str) -> None:
    """`Restart=on-failure` matches the compose skeleton's restart policy."""
    del cli_token
    parser = _parse_unit(filename)

    assert parser.get("Service", "Restart") == "on-failure"


@pytest.mark.parametrize(("filename", "cli_token"), sorted(_UNIT_TO_CLI_TOKEN.items()))
def test_unit_exec_start_runs_hedgekit_with_matching_process_token(
    filename: str, cli_token: str
) -> None:
    """`ExecStart` ends with `hedgekit run --process <token>` for this unit."""
    parser = _parse_unit(filename)

    exec_start = parser.get("Service", "ExecStart")
    assert exec_start.endswith(f"hedgekit run --process {cli_token}")
