## Role

You are a senior Python/SRE engineer who writes operational drills as executable, repeatable tests, working in this repo's `hedgekit/` package and a new `drills/` test surface (Python ≥3.11, mypy --strict, bash where unavoidable).

## Goal

Every §19 RUNBOOK-critical procedure — restore-from-backup, kill/re-arm, reconciliation-mismatch response, key rotation — plus the §10.7 profit-sweep advisory and floor ratchet, is exercised by a scripted, idempotent drill that runs against production APIs (read-only or micro-capped), leaves ledgered evidence, and can be re-run on demand.

## Context

- **Parent epic:** #9
- **Predecessor issue(s):** #58 (must be merged first — drills assert against the full live monitoring surface).
- **SPEC section:** `plans/SPEC_v3.md` §18 M7 ("restore/kill/reconciliation drills on production APIs; profit-sweep + ratchet in anger"), §10.7 (floor governance: ratchet raises freely, profit-sweep advisory alert), §10.11 (kill switch triggers and effects; re-arm is manual), §11.4 (crash recovery/reconciliation), §12 (encrypted backups; restore drills tested; audit-bundle export).
- **Files involved:**
  - `hedgekit/drills/` — drill runner + individual drill implementations (create).
  - `hedgekit/cli.py` (or equivalent) — `hedgekit drill <name> [--production]` entrypoint.
  - `tests/drills/` — every drill runs green in CI against PaperExchange/fixtures; the `--production` path only changes the adapter binding.
  - `scripts/` — thin wrappers only if needed; logic lives in the package.
- **Prior decisions:** kill positions are **held, not dumped** (§10.11); backups are encrypted and restore equivalence is assertable via `hedgekit rebuild` (§12); the dashboard can never lower the floor (§10.7/§14) — drills must not create a bypass. Drills that mutate state (kill, ratchet) must be safe to run during LIVE_MICRO: bounded by the same Kernel checks as any other path, never a privileged side door.
- **State of the world:** kill switch, reconciler, backup/restore, ratchet, and profit-sweep advisory all exist from earlier epics with unit/chaos tests against PaperExchange. Nothing runs them end-to-end against production APIs, and there is no operator-facing drill entrypoint or evidence trail.

## Output Format

Deliverable is a single PR containing:

- [ ] Drill framework: `Drill {name, preconditions, steps, assertions, evidence}` — each drill writes a ledgered `DRILL_COMPLETED` event with a structured result payload.
- [ ] Drills implemented: `restore-from-backup` (restore latest encrypted backup to a temp state dir, `rebuild`-verify hash-chain equivalence); `kill-rearm` (trigger kill via all three paths — dashboard/CLI/KILL-file — assert open orders cancelled, approvals disabled, positions held, then manual re-arm with typed confirmation); `reconciliation-mismatch` (inject a synthetic mismatch, assert HALT + alert + the RUNBOOK-documented recovery path clears it); `key-rotation` (rotate exchange + LLM keys, assert old keys are gone from every process env and preflight passes with new keys).
- [ ] Ratchet/sweep in anger: drive equity above the high-water mark on production data (micro scale), assert `floor_ratchet_ppm_of_new_profits` raises the floor and the profit-sweep advisory alert fires (§10.7) — and that the system did not and cannot move funds itself.
- [ ] CI runs every drill against PaperExchange fixtures; `--production` is manual-only and documented.
- [ ] No drive-by changes unrelated to the goal.

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_kill_rearm_drill_holds_positions(paper_exchange_with_positions):
    result = run_drill("kill-rearm", exchange=paper_exchange_with_positions)
    assert result.passed
    assert result.evidence["open_orders_after_kill"] == 0
    assert result.evidence["positions_after_kill"] == result.evidence["positions_before_kill"]  # held, not dumped
    assert ledger.contains("DRILL_COMPLETED", drill="kill-rearm")
```

**Example: operator invocation**

```
$ hedgekit drill restore-from-backup
✓ backup decrypted (age)            ✓ rebuild hash-chain equivalent
✓ read models match event replay    → DRILL_COMPLETED ledgered (evidence id 01J...)
```

## Constraints

**Scope fence:** Do not write the RUNBOOK prose — #60 documents these drills; this issue makes them executable. Do not add new kill/restore/ratchet *logic* — drills exercise existing mechanisms; if a drill reveals a defect, file a separate bug, don't fix it inside the drill PR.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. Drills are additive; a failed drill must never leave the daemon in a state the RUNBOOK can't recover (each drill's teardown restores pre-drill state or halts loudly).

**No privileged side doors:** drills go through the same Kernel/Gateway paths as production traffic; a drill must never bypass floor checks, token verification, or the ledger.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #9` and `Closes #59`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `live-micro`
