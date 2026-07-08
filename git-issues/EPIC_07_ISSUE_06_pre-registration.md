## Role

You are a senior Python engineer with experience in tamper-evident systems, working in this repo's `windbreak/evaluation/` package (mypy --strict). You are implementing the anti-Goodhart mechanism (SPEC T15).

## Goal

At PAPER entry, the complete gate definition (metrics, windows, thresholds, baselines, clustering scheme) is canonically serialized, hashed, and ledgered; any subsequent change to the gate plan resets the PAPER evaluation clock and re-registers — all per SPEC §13.6.

## Context

- **Parent epic:** #8
- **Predecessor issue(s):** #53 (must be merged first — the gate plan references windows and cohorts by name).
- **SPEC section:** `plans/SPEC_v3.md` §13.6 (pre-registration), §4 T15 (metric shopping / Goodhart), §10.9 (the gates being registered: ≥300 resolved, ≥100 independent event groups, Brier skill CI excluding zero, etc.), §17.4 ("Any change to this model re-registers the gate plan" — the paper-fill model hash is part of the plan).
- **Files involved:**
  - `windbreak/evaluation/preregistration.py` — new: `GatePlan` dataclass, canonical serialization (sorted keys, integer units, no floats), hash, ledger events (`GATE_PLAN_REGISTERED`, `GATE_PLAN_CHANGED`)
  - `windbreak/evaluation/registry.py` — gate computations read thresholds *only* from the registered plan, never from live config
  - `tests/evaluation/test_preregistration.py`
- **Prior decisions:** the ledger is hash-chained and append-only (§12) — registration is an event, not a table update; the Risk Kernel (EPIC_04) consumes the "PAPER clock start" timestamp when checking the ≥90-day requirement, so expose it as a read model. The paper-fill model version (§17.4) is a field of the plan.
- **State of the world:** metrics, windows, and cohorts are real; gate thresholds are currently read straight from config at computation time.

## Output Format

Deliverable is a single PR containing:

- [ ] `GatePlan` with canonical serialization and hash; registration + change events ledgered
- [ ] Gate computations rewired to read from the registered plan; reading thresholds from live config for a gate is now impossible by construction
- [ ] PAPER-clock reset semantics: a changed plan hash invalidates the prior clock and emits `GATE_PLAN_CHANGED`
- [ ] Tests: same plan → same hash (byte-identical, key-order independent); any single-field change → different hash + clock reset; fee/fill-model version change also resets (per §17.4)
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_gate_plan_change_resets_paper_clock() -> None:
    reg = register_gate_plan(PLAN_A, ledger)
    clock_start = reg.paper_clock_start
    changed = replace(PLAN_A, brier_skill_required_ppm=PLAN_A.brier_skill_required_ppm + 1)
    reg2 = register_gate_plan(changed, ledger)
    assert reg2.plan_hash != reg.plan_hash
    assert reg2.paper_clock_start > clock_start
    assert ledger.last_event().event_type == "GATE_PLAN_CHANGED"
```

## Constraints

**Scope fence:** Do not implement promotion/demotion decisions — the Risk Kernel (EPIC_04) owns mode transitions; you only produce the registered plan and clock read model. Do not implement the dual-path computation (#55). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

**Canonicalization:** serialization must be deterministic across Python versions and dict orderings; no floats anywhere in the plan (integer ppm/micros only, SPEC §6.1).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #8` and `Closes #54`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `evaluation`
