## Role

You are a senior Python engineer building fail-safe control paths, working in this repo's `windbreak/riskkernel/` package.

## Goal

Every SPEC §10.11 kill trigger — dashboard button (double-confirm), CLI `windbreak kill`, a `KILL` file in the state dir (works with HTTP down), and automatic on repeated reconciliation mismatch — cancels all open orders, disables approvals, alerts, holds positions, and requires manual typed-confirmation re-arm.

## Context

- **Parent epic:** #5
- **Predecessor issue(s):** #34 (must be merged first)
- **SPEC section:** `plans/SPEC_v3.md` §10.11 (kill switch), §10.2 (`KILLED` requires manual re-arm with typed confirmation), §3.2 ("when in doubt, halt and alert"), config `ops.state_dir` (§16)
- **Files involved:**
  - `windbreak/riskkernel/kill.py` — new: trigger sources, kill execution, re-arm flow
  - `windbreak/riskkernel/process.py` — poll the KILL file and the auto-trigger condition
  - `windbreak/cli.py` — `windbreak kill`, `windbreak rearm` (typed confirmation phrase)
  - `tests/riskkernel/test_kill.py` — all four trigger paths + re-arm
- **Prior decisions:** Effect of kill: cancel all open orders, disable approvals, alert. **Positions are held, not dumped** — bounded loss means holding is safe and panic-selling into thin books is not (§10.11). Order cancellation is issued as reduce-only/cancel directives through the same intent path the Gateway will consume (EPIC_05); until the Gateway exists, the kill path emits ledgered `CANCEL_ALL` directives consumed by a test double. The KILL file must work with the HTTP dashboard down — file polling, no network dependency. Automatic trigger: repeated reconciliation mismatch (threshold config-driven), wired to #32's verification results.
- **State of the world:** Mode machine has `KILLED` with a typed-confirmation re-arm stub at the API level (#33); governance and ack flows exist; no trigger plumbing, no KILL-file watcher, no cancel-all emission.

## Output Format

Deliverable is a single PR containing:

- [ ] `kill.py`: unified kill executor — ledger `KILL` event with trigger source, emit `CANCEL_ALL` directive, disable the approval pipeline (every subsequent intent vetoed with reason `KILLED`), release non-submitted reservations, fire alert
- [ ] Trigger sources: CLI verb; KILL-file watcher on `ops.state_dir` (created → kill; works in a test with the dashboard/HTTP layer absent); auto-trigger after N consecutive verification mismatches; dashboard trigger stub as an authenticated Kernel API endpoint requiring a double-confirm token (UI itself is Process D scope)
- [ ] `windbreak rearm`: requires typing an exact confirmation phrase including the kill event's ledger sequence number; re-arm transitions `KILLED → PAUSED` (operator then resumes explicitly), ledgered
- [ ] Kill drill test (§10.12): from a state with open reservations + pending acks, kill → all reservations released or marked, approvals disabled, positions untouched, single alert fired; re-arm restores approval capability without replaying stale intents
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_kill_file_works_without_http(tmp_state_dir):
    kernel = running_kernel(state_dir=tmp_state_dir, dashboard=None)
    (tmp_state_dir / "KILL").touch()
    kernel.wait_for_cycle()
    assert kernel.mode is Mode.KILLED
    assert kernel.evaluate_intent(make_intent()).veto_reason == "KILLED"
    assert ledger.contains("CANCEL_ALL")

def test_rearm_requires_exact_typed_confirmation():
    with pytest.raises(ConfirmationMismatch):
        kernel.rearm(confirmation="yes please")
```

## Constraints

**Scope fence:** Do not implement actual exchange order cancellation — the Gateway (EPIC_05) consumes `CANCEL_ALL`; here a ledgered directive + test double suffices. Do not build the dashboard button UI. Do not add position-dumping logic under any circumstances — positions are held by design. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges: normal flow untouched until a trigger fires; after kill, the system is visibly and safely dead until `windbreak rearm`.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] 100% branch coverage on `riskkernel` (§17.6); ≥90% on other changed lines.
- [ ] `mypy --strict` clean; RUNBOOK stub notes for kill/re-arm updated if `docs/` runbook exists.
- [ ] PR body includes `Refs #5` and `Closes #35`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer Action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `risk-kernel`
