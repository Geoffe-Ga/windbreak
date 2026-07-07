# feat(main): wire RiskKernel + KillIntegration/KillFileWatcher into `hedgekit run`

**Follows up:** #35 (PR #134) · **Refs:** #5 (epic: Risk Kernel M3)
**Priority:** safety-critical

## Problem

Issue #35 / PR #134 landed the full Risk Kernel kill switch component
(`KillSwitch`, `KillFileWatcher`, `ReconciliationMismatchMonitor`,
`DashboardKillStub`, `KillIntegration`) with extensive tests, but **none of its
four triggers is composed into the live `hedgekit run` process**, so
`hedgekit kill` / `hedgekit rearm` are inert against a running deployment today.

The Claude reviewer traced every production construction site:

- `hedgekit/main.py`'s `_run_heartbeat` / `run_loop` (the only console script
  registered in `pyproject.toml` `[project.scripts]`) never constructs a
  `RiskKernel`, `KillIntegration`, or `KillFileWatcher` — it is a standalone
  heartbeat loop. `main.py` imports only `KILL_FILENAME` / `REARM_FILENAME`
  from `hedgekit.riskkernel.kill`, never `KillSwitch` / `KillIntegration` /
  `KillFileWatcher`.
- `hedgekit/riskkernel/process.py` `main()` is the only place a `RiskKernel(` is
  constructed outside tests, and it does **not** pass `kill_integration=`.
- `grep -rn "kill_integration=" hedgekit/ tests/` turns up exactly two hits,
  both inside `tests/riskkernel/test_kill.py`.

Concretely: `hedgekit kill --state-dir DIR` writes a `KILL` file that no running
process ever polls, because no `KillFileWatcher` is attached to a live
`RiskKernel`. Likewise `RiskConfig.kill_after_consecutive_mismatches` (default 3)
is read only inside tests that hand-build a `ReconciliationMismatchMonitor`;
nothing in production wires that monitor from a loaded `RiskConfig`.

For a safety-critical trading kill switch this is worse than no kill switch: an
operator (or the SPEC's own reconciliation-mismatch auto-trigger) can reasonably
believe the system is halted when it isn't.

## Why deferred (not fixed in PR #134)

This repo builds tracer-code style, and `RiskKernel` is **not yet composed into
`main.py` at all** — the `verifier` param is likewise never passed by
`process.main()` today. Wiring the kill path into the run loop is a genuine scope
expansion (it needs the whole `RiskKernel` composition seam in `main.py`), so
per the reviewer's explicit accepted alternative it is tracked here instead of
expanding PR #134.

## Acceptance criteria

- [ ] `process.main()` (and/or `main.py`'s run loop) builds a `KillIntegration`:
      a `KillSwitch` + `KillFileWatcher` over `ops.state_dir` +
      `ReconciliationMismatchMonitor(threshold=config.kill_after_consecutive_mismatches)`,
      and passes it as `kill_integration=` to the `RiskKernel` it runs.
- [ ] An integration test builds a `RiskKernel` **the way the production
      entrypoint builds it** and asserts it reacts to a `KILL` file dropped on
      disk during normal operation (the `test_kill_file_works_without_http`
      scenario from issue #35's own acceptance list, exercised against the real
      composition rather than a hand-assembled `KillIntegration`).
- [ ] `hedgekit kill --state-dir DIR` demonstrably halts the running kernel
      (approvals vetoed) end-to-end.
- [ ] The `hedgekit/riskkernel/kill.py` module docstring's "wiring status" note
      is updated to reflect that the adapters are now composed into
      `hedgekit run`.

## Notes

- Consider the concurrency note the reviewer raised: once `DashboardKillStub` is
  served from its own request thread, `KillSwitch.kill()`'s check-then-transition
  is a race (no lock). Add a lock or an explicit single-threaded-caller contract
  before the dashboard trigger goes live. (Out of scope for the run-loop wiring,
  but worth capturing.)
