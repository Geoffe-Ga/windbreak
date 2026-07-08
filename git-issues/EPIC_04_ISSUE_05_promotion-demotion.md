## Role

You are a senior Python engineer building governed state machines, working in this repo's `windbreak/riskkernel/` package.

## Goal

Promotion through `RESEARCH → PAPER → LIVE_MICRO → LIVE` happens only via pre-registered quantitative gates evaluated as data, every §10.10 trigger demotes/halts automatically, `mode_ceiling` is unexceedable, and the sole significance-bypass is a ledgered operator override that caps the system at LIVE_MICRO permanently.

## Context

- **Parent epic:** #5
- **Predecessor issue(s):** #32 (must be merged first)
- **SPEC section:** `plans/SPEC_v3.md` §10.9 (promotion gates), §10.10 (demotion/halt triggers), §10.2 (mode machine), §1.1-4 (evidence-gated autonomy), §0 ("the promotion-gate loophole is closed — significance is mandatory for live promotion, overrides are ledgered and mode-capped"), threats T7, T15
- **Files involved:**
  - `windbreak/riskkernel/promotion.py` — new: gate definitions as typed data, gate evaluation, promotion decisions
  - `windbreak/riskkernel/demotion.py` — new: trigger registry mapping §10.10 conditions to PAUSE/demote-one-mode/HALT actions
  - `windbreak/riskkernel/modes.py` — extend with guarded promote/demote entrypoints (no other transition API)
  - `tests/riskkernel/test_promotion.py`, `tests/riskkernel/test_demotion.py`, `tests/riskkernel/test_override.py`
- **Prior decisions:** Gate metric *values* (Brier skill, bootstrap CI, resolved counts) are computed by EPIC_07 (M6) and consumed here as a typed `GateEvidence` input — the Kernel evaluates thresholds, it does not compute statistics. Defaults per §10.9: PAPER→LIVE_MICRO needs ≥300 resolved real-time forecasts, ≥100 independent event groups, Brier-skill CI excluding zero (MANDATORY), paper PnL > 0 net of fees+slippage+research over ≥90 days, drawdown < threshold, calibration slope in band, zero Kernel invariant failures. Demotion examples (§10.10): daily-loss breach → pause to next UTC day; drawdown breach → demote one mode; balance mismatch → HALT; canary drift unacknowledged, token replay attempt, stale heartbeat, clock skew, fee model unavailable, jurisdiction unknown, disk below threshold, backup failures → per-trigger action. LIVE_MICRO caps deployed capital at `micro_cap_micros` regardless of all other settings (§10.2).
- **State of the world:** Mode machine with ceiling exists; verification loop can HALT; no promotion path exists (transitions are manual/test-only).

## Output Format

Deliverable is a single PR containing:

- [ ] `promotion.py`: gates as serializable data structures (thresholds from config), evaluation returning pass/fail per criterion, promotion applied only when all pass AND `mode_ceiling` permits; every evaluation ledgered with full evidence
- [ ] The significance override path: explicit operator acknowledgement, ledgered, and permanently capping that deployment at LIVE_MICRO (§10.9) — with a test proving the cap survives restart
- [ ] `demotion.py`: every §10.10 trigger wired to its action; triggers are individually testable and ledger their firing
- [ ] `KILLED` requires manual re-arm with typed confirmation (§10.2) — enforce at the transition API (full kill-switch trigger plumbing is #35)
- [ ] Full mode-transition matrix test: every (mode, event) pair asserts the spec-mandated destination or rejection (§10.12)
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test cases that should pass after this issue lands**

```python
def test_significance_is_mandatory_for_live_micro():
    evidence = gate_evidence(resolved=350, groups=120, brier_ci=(−0.002, 0.011),
                             paper_pnl_positive=True)  # CI includes zero
    assert not evaluate_promotion(Mode.PAPER, evidence).promoted

def test_override_caps_at_live_micro_forever():
    kernel.apply_ledgered_override(operator_ack="...")
    assert kernel.mode_ceiling_effective is Mode.LIVE_MICRO
    kernel = restart(kernel)
    assert kernel.mode_ceiling_effective is Mode.LIVE_MICRO
```

## Constraints

**Scope fence:** Do not compute statistics (Brier, bootstrap, calibration) — EPIC_07 (M6) owns metric computation and pre-registration hashing (§13.6); consume `GateEvidence` as input. Floor-lowering governance is #34. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges: with synthetic passing evidence the Kernel promotes RESEARCH→PAPER in a test harness; all live-mode paths remain veto-gated as before.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] 100% branch coverage on `riskkernel` (§17.6); ≥90% on other changed lines.
- [ ] `mypy --strict` clean; gate data structures documented against §10.9.
- [ ] PR body includes `Refs #5` and `Closes #33`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer Action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `risk-kernel`
