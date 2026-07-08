## Role

You are a senior Python engineer building operator-facing safety governance, working in this repo's `windbreak/riskkernel/` package and its CLI entrypoints.

## Goal

Floor changes obey raise-freely/lower-slowly exactly per SPEC §10.7 (48h cool-off, challenge nonce, alert, demotion to PAPER), the optional ratchet locks in a configured fraction of new high-water profits, profit-sweep advisories fire, and orders above the human-ack threshold hold for explicit ledgered acknowledgement.

## Context

- **Parent epic:** #5
- **Predecessor issue(s):** #33 (must be merged first)
- **SPEC section:** `plans/SPEC_v3.md` §10.7 (floor governance), §10.8 (human-ack thresholds), §10.3 ("human-ack satisfied if required" check), threat T7 (operator tilt), config keys `capital.floor_micros`, `capital.floor_ratchet_ppm_of_new_profits`, `capital.profit_sweep_threshold_micros`, `risk.require_human_ack_above_micros` (§16)
- **Files involved:**
  - `windbreak/riskkernel/governance.py` — new: floor-change state machine, ratchet, profit-sweep advisory
  - `windbreak/riskkernel/human_ack.py` — new: pending-ack queue with expiry
  - `windbreak/riskkernel/checks.py` — wire the human-ack check into the pipeline
  - `windbreak/cli.py` (or EPIC_01's CLI module) — `windbreak floor raise`, `windbreak floor request-lower`, `windbreak floor confirm-lower --nonce`, `windbreak ack <approval-id>`
  - `tests/riskkernel/test_governance.py`, `tests/riskkernel/test_human_ack.py`
- **Prior decisions:** Raising `floor_micros` applies immediately, from CLI or dashboard. Lowering requires: CLI request → ledgered pending change → 48h cool-off → second CLI confirmation with challenge nonce → alert → demotion to PAPER until the next full reconciliation passes. The dashboard can never lower the floor (§14 forbids it; enforce Kernel-side, not just UI-side). Ratchet: `floor_ratchet_ppm_of_new_profits` (default 50%) auto-raises the floor on each new equity high-water mark — raising needs no governance delay. Profit-sweep: alert only; the system cannot and must not move funds (§10.7). Human-ack: live-mode orders with `worst_case_cost > require_human_ack_above_micros` hold pending operator ack with expiry; ack events ledgered (§10.8).
- **State of the world:** Promotion/demotion engine exists (demotion-to-PAPER is callable); floor is a static config value consumed by floor math; no change-governance, no ack queue.

## Output Format

Deliverable is a single PR containing:

- [ ] `governance.py`: floor-change state machine (IMMEDIATE_RAISE | PENDING_LOWER → COOLING → AWAITING_NONCE_CONFIRM → APPLIED+DEMOTED), all transitions ledgered, cool-off duration config-driven with 48h default, clock injected for testability
- [ ] Ratchet: on every new equity high-water mark, floor rises by the configured ppm of the gain; idempotent, ledgered, and never lowers
- [ ] Profit-sweep advisory: equity > HWM + `profit_sweep_threshold_micros` fires an alert (advisory only — no fund movement code anywhere)
- [ ] `human_ack.py`: pending approvals with expiry after which the approval lapses and the reservation releases; `null` threshold (PAPER) disables the check
- [ ] CLI verbs wired; a dashboard-originated lower request is rejected at the Kernel API layer with a ledgered refusal
- [ ] Tests: cool-off cannot be shortened (time-travel attempts fail), wrong nonce fails, demotion-to-PAPER on completed lowering, ratchet math on fixed-point units, ack expiry releases reservation
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_lowering_floor_demotes_to_paper_after_cooloff_and_nonce():
    gov.request_lower(new_floor_micros=500_000_000)
    advance_clock(hours=48)
    gov.confirm_lower(nonce=gov.pending_challenge_nonce())
    assert kernel.mode is Mode.PAPER
    assert kernel.floor_micros == 500_000_000

def test_dashboard_can_never_lower_floor():
    with pytest.raises(ForbiddenOrigin):
        gov.request_lower(new_floor_micros=1, origin=Origin.DASHBOARD)
```

## Constraints

**Scope fence:** Do not build dashboard UI (Process D) — only the Kernel-side API refusal. Do not implement reconciliation itself ("until the next full reconciliation passes" hooks into #32's verification results). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges: `windbreak floor raise` works immediately; a lower request visibly parks in cool-off; normal intent flow is unaffected below the ack threshold.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] 100% branch coverage on `riskkernel` (§17.6); ≥90% on other changed lines.
- [ ] `mypy --strict` clean; CLI verbs documented in help text.
- [ ] PR body includes `Refs #5` and `Closes #34`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer Action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `risk-kernel`
