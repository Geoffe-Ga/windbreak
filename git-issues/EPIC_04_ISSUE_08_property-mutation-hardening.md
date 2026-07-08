## Role

You are a senior Python engineer specializing in property-based testing (hypothesis) and mutation testing (mutmut), working in this repo's `tests/riskkernel/` suite.

## Goal

Property tests over random concurrent intent streams with crashes injected at every reserve/approve/ack edge prove the floor invariant unbreakable, the full §17.2 Risk Kernel matrix is covered, and mutation score reaches ≥90% on the kernel, accounting, and token packages.

## Context

- **Parent epic:** #5
- **Predecessor issue(s):** #35 (must be merged first — this issue hardens the completed Kernel surface)
- **SPEC section:** `plans/SPEC_v3.md` §10.12 (acceptance criteria), §17.2 (Risk Kernel matrix), §17.6 (coverage & mutation floors: "branch coverage alone demonstrably passes buggy comparators"), §3.5 (all randomness seeded and logged)
- **Files involved:**
  - `tests/riskkernel/test_property_concurrent.py` — new: hypothesis stateful/concurrent intent-stream tests
  - `tests/riskkernel/test_crash_injection.py` — new: crash points at every reserve/approve/ack edge
  - `tests/riskkernel/test_matrix.py` — new: §17.2 scenario matrix
  - `scripts/mutation.sh` / `pyproject.toml` mutmut config — scope to `windbreak/riskkernel/`, `windbreak/accounting/`, `windbreak/tokens/` with a ≥90% threshold gate
  - Production files ONLY where a surviving mutant exposes a real gap (each such fix documented in the PR body)
- **Prior decisions:** §17.2 matrix to cover: random concurrent intents; partial fills; cancels; expired approvals; crash between reserve/approve and approve/submit; balance mismatch; schema drift; fee-model outage; clock skew; jurisdiction unknown; floor-lowering governance; ratchet; token replay/mutation/expiry; human-ack expiry. Property invariant: after ANY interleaving + crash/restart, `worst_case_equity ≥ floor` and no reservation is leaked or double-spent. Hypothesis seeds logged for reproducibility (§3.5).
- **State of the world:** All Kernel features exist (#01–#07) with unit/branch coverage at 100% on riskkernel; no cross-feature concurrent property suite; mutmut configured repo-wide by the scaffold but not threshold-gated per package.

## Output Format

Deliverable is a single PR containing:

- [ ] Hypothesis stateful test driving random intent streams against a live Kernel instance with a simulated exchange state, asserting the floor invariant and reservation conservation after every step
- [ ] Crash injection at each edge (reserve→approve, approve→token-return, token-return→ack, ack→release) followed by restart + startup reconciliation, asserting invariants hold post-recovery
- [ ] Every §17.2 matrix row implemented as a named, individually runnable test
- [ ] Mutation run wired into `./scripts/mutation.sh` with a ≥90% score gate on the three packages; surviving-mutant analysis included in the PR description; kill-or-justify for each survivor
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: property test shape that should pass after this issue lands**

```python
class KernelInvariants(RuleBasedStateMachine):
    @rule(intent=intents())
    def submit(self, intent):
        self.kernel.evaluate_intent(intent)

    @rule(edge=sampled_from(CRASH_EDGES))
    def crash_and_recover(self, edge):
        self.kernel = crash_at(self.kernel, edge).restart()

    @invariant()
    def floor_never_breached(self):
        assert self.kernel.worst_case_equity() >= self.kernel.floor
        assert self.kernel.reservations.leaked() == []
```

## Constraints

**Scope fence:** This is a hardening issue — no new features. Production changes are allowed ONLY to kill a surviving mutant or fix a bug the property suite exposes, each documented. Do not touch Gateway/selector/forecast packages. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. Test-only additions plus documented bug fixes; no behavior redesigns.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] 100% branch coverage on `riskkernel`, accounting, and token packages; **mutmut score ≥90%** on the same three (§17.6), enforced by `./scripts/mutation.sh`.
- [ ] Hypothesis seeds logged and reproducible (`--hypothesis-seed` documented in `scripts/README.md`).
- [ ] PR body includes `Refs #5` and `Closes #36`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer Action is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `risk-kernel`, `testing`
