"""Failing-first tests wiring RiskKernel + KillIntegration into `windbreak run`
(issue #144, RED).

The kill switch (`KillSwitch`, `KillFileWatcher`, `ReconciliationMismatchMonitor`,
`KillIntegration` in `windbreak/riskkernel/kill.py`) is fully built and tested
(issue #35), but `windbreak/main.py` never composes it into the live
`windbreak run --process riskkernel` process -- that invocation still runs the
bare `run_loop` heartbeat, so a running deployment's `windbreak kill` writes a
`KILL` file no running kernel polls. `windbreak.main._build_risk_kernel` does
not exist yet, so every import below fails collection with
`ImportError: cannot import name '_build_risk_kernel' from 'windbreak.main'`
-- the expected Gate 1 RED state for issue #144.

Once `_build_risk_kernel` and its `_run_riskkernel` CLI routing land, this
file pins: a KILL file dropped into `ops.state_dir` actually halts a kernel
built by `_build_risk_kernel` (AC#2, no HTTP/dashboard involved); the real
`windbreak kill` CLI verb engages that same kernel end-to-end (AC#3); `windbreak
run --process riskkernel` routes to the real `RiskKernel` (proven by its
`heartbeat beat=N` log shape, distinct from `run_loop`'s `heartbeat seq=N`) and
its post-run shutdown line; a state directory that cannot be created fails
closed with a `FATAL` critical log and zero heartbeats (never entering the
kernel loop); `RiskConfig.kill_after_consecutive_mismatches` is threaded from
config into the wired `ReconciliationMismatchMonitor`, auto-killing the *shared*
mode machine both the kernel and the kill switch read; and the CLI's float
`--heartbeat-interval` is accepted and mapped onto the kernel's integer-seconds
`run(heartbeat_interval=...)` without raising.

Every test builds its own `WindbreakConfig` via `dataclasses.replace` over a
`tmp_path`-rooted `OpsConfig.state_dir`, and every CLI-level test that reads a
YAML `--config` file points `ops.state_dir` at a `tmp_path` too -- so no test
in this module ever touches the real `~/.local/share/windbreak`, where a KILL
file dropped by a previous manual run or a concurrent test process could
otherwise pollute this or a developer's environment.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest
import yaml

from tests.riskkernel.conftest import make_context, make_intent
from windbreak.config import OpsConfig, RiskConfig, WindbreakConfig
from windbreak.main import _build_risk_kernel, main
from windbreak.riskkernel.kill import KILL_FILENAME
from windbreak.riskkernel.modes import Mode
from windbreak.riskkernel.verification import VerificationOutcome

if TYPE_CHECKING:
    from pathlib import Path


def _config_with_state_dir(state_dir: Path, **overrides: object) -> WindbreakConfig:
    """Build a `WindbreakConfig` whose `ops.state_dir` is `state_dir`.

    Args:
        state_dir: The directory `ops.state_dir` is pointed at (a `tmp_path`
            in every caller, so no test ever touches the real state dir).
        **overrides: Additional top-level `WindbreakConfig` field overrides
            (e.g. `risk=RiskConfig(...)`).

    Returns:
        A `WindbreakConfig` built from defaults except `ops.state_dir` and
        any `**overrides`.
    """
    return dataclasses.replace(
        WindbreakConfig(), ops=OpsConfig(state_dir=str(state_dir)), **overrides
    )


def _write_state_dir_config(config_path: Path, state_dir: Path) -> None:
    """Write a minimal YAML config whose only key is `ops.state_dir`.

    Args:
        config_path: Where to write the YAML document.
        state_dir: The directory `ops.state_dir` is pointed at.
    """
    config_path.write_text(
        yaml.safe_dump({"ops": {"state_dir": str(state_dir)}}), encoding="utf-8"
    )


def _json_lines(stderr: str) -> list[dict[str, object]]:
    """Parse every non-blank line of captured stderr as a JSON log record.

    Args:
        stderr: The captured stderr text (one JSON object per line).

    Returns:
        The parsed JSON payloads, in emission order.
    """
    return [json.loads(line) for line in stderr.splitlines() if line]


# --- _build_risk_kernel: shape of the wired kernel/integration ------------------


def test_build_risk_kernel_returns_research_mode_kernel_wired_with_kill_integration(
    tmp_path: Path,
) -> None:
    """`_build_risk_kernel` returns a fresh-RESEARCH kernel with a full
    `KillIntegration` (switch, watcher, monitor all present), and creates
    `ops.state_dir` fail-closed up front.
    """
    state_dir = tmp_path / "state"
    config = _config_with_state_dir(state_dir)

    kernel, integration = _build_risk_kernel(config)

    assert kernel.mode is Mode.RESEARCH
    assert integration.switch is not None
    assert integration.watcher is not None
    assert integration.monitor is not None
    assert state_dir.is_dir()


# --- AC#2: a KILL file halts the kernel with no HTTP/dashboard involved ---------


@pytest.mark.timeout(30)
def test_kill_file_works_without_http(tmp_path: Path) -> None:
    """A `KILL` file dropped into `ops.state_dir` halts a kernel built by
    `_build_risk_kernel`, with zero HTTP server or dashboard object ever
    constructed: one beat with no file leaves the kernel un-killed and
    approving evaluation as usual; dropping the file and running one more
    beat kills it, and `evaluate_intent` is thereafter hard-vetoed.
    """
    config = _config_with_state_dir(tmp_path)
    kernel, _integration = _build_risk_kernel(config)

    kernel.run(max_beats=1, heartbeat_interval=0)
    assert kernel.mode is not Mode.KILLED

    (tmp_path / KILL_FILENAME).write_text("", encoding="utf-8")
    kernel.run(max_beats=1, heartbeat_interval=0)

    assert kernel.mode is Mode.KILLED
    decision = kernel.evaluate_intent(make_intent(), make_context())
    assert decision.reasons == ("KILLED",)


# --- AC#3: the real `windbreak kill` CLI verb halts the wired kernel ------------


@pytest.mark.timeout(30)
def test_kill_cli_verb_engages_the_wired_kernel_end_to_end(tmp_path: Path) -> None:
    """`windbreak kill --state-dir DIR` (the real CLI verb, via `main`) writes
    the `KILL` file a kernel built by `_build_risk_kernel` over the same `DIR`
    then picks up on its next beat, hard-vetoing every subsequent intent.
    """
    config = _config_with_state_dir(tmp_path)
    kernel, _integration = _build_risk_kernel(config)

    exit_code = main(["kill", "--state-dir", str(tmp_path)])

    assert exit_code == 0
    assert (tmp_path / KILL_FILENAME).exists()

    kernel.run(max_beats=1, heartbeat_interval=0)

    assert kernel.mode is Mode.KILLED
    decision = kernel.evaluate_intent(make_intent(), make_context())
    assert decision.reasons == ("KILLED",)


# --- Routing: `run --process riskkernel` drives the real RiskKernel ------------


@pytest.mark.timeout(30)
def test_run_process_riskkernel_routes_to_the_real_risk_kernel_not_run_loop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`windbreak run --process riskkernel` exits 0 and logs a `RiskKernel`
    heartbeat (`heartbeat beat=1`, `component=riskkernel`) followed by a
    `shutdown reason=max_beats` line -- never the bare `run_loop`'s
    `heartbeat seq=` shape, proving the routing divergence actually composes
    a real kernel rather than falling through to the pre-#144 heartbeat.
    """
    config_path = tmp_path / "config.yaml"
    _write_state_dir_config(config_path, tmp_path)

    exit_code = main(
        [
            "run",
            "--process",
            "riskkernel",
            "--max-beats",
            "1",
            "--heartbeat-interval",
            "0",
            "--config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    payloads = _json_lines(capsys.readouterr().err)
    assert not any("seq=" in str(payload.get("msg", "")) for payload in payloads)
    heartbeat_payload = next(
        payload
        for payload in payloads
        if "heartbeat beat=1" in str(payload.get("msg", ""))
    )
    assert heartbeat_payload["component"] == "riskkernel"
    shutdown_payload = next(
        payload
        for payload in payloads
        if str(payload.get("msg", "")) == "shutdown reason=max_beats"
    )
    assert shutdown_payload["component"] == "riskkernel"


# --- Fail-closed startup: an uncreatable state dir never enters the loop -------


@pytest.mark.timeout(30)
def test_run_process_riskkernel_fails_closed_when_state_dir_cannot_be_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When `ops.state_dir` resolves under an existing regular file (so the
    builder's `mkdir` raises), `run --process riskkernel` returns 1, logs a
    `FATAL` critical line, and never enters the kernel loop -- zero heartbeat
    lines are emitted.
    """
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("this is a file, not a directory", encoding="utf-8")
    unusable_state_dir = blocking_file / "state"
    config_path = tmp_path / "config.yaml"
    _write_state_dir_config(config_path, unusable_state_dir)

    exit_code = main(
        [
            "run",
            "--process",
            "riskkernel",
            "--max-beats",
            "1",
            "--heartbeat-interval",
            "0",
            "--config",
            str(config_path),
        ]
    )

    assert exit_code == 1
    payloads = _json_lines(capsys.readouterr().err)
    assert any(
        payload.get("level") == "CRITICAL" and "FATAL" in str(payload.get("msg", ""))
        for payload in payloads
    )
    assert not any("heartbeat" in str(payload.get("msg", "")) for payload in payloads)


# --- Threshold wiring: RiskConfig.kill_after_consecutive_mismatches ------------


def test_reconciliation_threshold_is_threaded_from_config_onto_the_shared_machine(
    tmp_path: Path,
) -> None:
    """A kernel built from a config with
    `risk.kill_after_consecutive_mismatches=2` auto-kills on exactly the 2nd
    consecutive `BREACH` observation -- not the 1st -- and the kill is visible
    on both the kill switch and the kernel, proving they share one mode
    machine rather than two independently-tracked ones.
    """
    config = _config_with_state_dir(
        tmp_path, risk=RiskConfig(kill_after_consecutive_mismatches=2)
    )
    kernel, integration = _build_risk_kernel(config)

    integration.monitor.observe(VerificationOutcome.BREACH)
    assert integration.switch.mode is not Mode.KILLED
    assert kernel.mode is not Mode.KILLED

    integration.monitor.observe(VerificationOutcome.BREACH)
    assert integration.switch.mode is Mode.KILLED
    assert kernel.mode is Mode.KILLED


# --- Interval mapping: the CLI's float seconds reach the kernel's int seconds --


@pytest.mark.timeout(30)
def test_run_process_riskkernel_accepts_a_fractional_heartbeat_interval(
    tmp_path: Path,
) -> None:
    """`--heartbeat-interval 0.5` (a float, the shared `run` flag's type) is
    accepted for `--process riskkernel` and exits 0 -- the CLI's float seconds
    must be mapped onto the kernel's integer-seconds `run(heartbeat_interval=)`
    without ever raising a `TypeError`/`ValueError` from the float/int seam.
    """
    config_path = tmp_path / "config.yaml"
    _write_state_dir_config(config_path, tmp_path)

    exit_code = main(
        [
            "run",
            "--process",
            "riskkernel",
            "--heartbeat-interval",
            "0.5",
            "--max-beats",
            "1",
            "--config",
            str(config_path),
        ]
    )

    assert exit_code == 0
