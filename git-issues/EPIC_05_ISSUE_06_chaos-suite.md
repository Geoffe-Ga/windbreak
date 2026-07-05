## Role

You are a senior Python test engineer working in this repo's `tests/` tree, experienced with fault-injection harnesses, property-based testing, and chaos testing of stateful systems.

## Goal

A CI-gating chaos suite proves the §11.5 acceptance criteria: with the Gateway killed at every state edge, network cut mid-submit, duplicate ACKs, out-of-order fills, missed fills, and cancel/fill races injected over PaperExchange, the system always converges to consistent state with zero duplicate live orders, zero orders without valid tokens, zero net-short positions, and correct reservation release.

## Context

- **Parent epic:** #6
- **Predecessor issue(s):** #41 (must be merged first — the full Gateway surface, including the sweeper, is under test)
- **SPEC section:** `plans/SPEC_v3.md` §11.5 (acceptance criteria — this issue implements them verbatim), §17.1 (chaos as a required CI suite), §11.4 (recovery paths under test), §10.5 (reservation release correctness), §4 rows T3, T9
- **Files involved:**
  - `tests/chaos/conftest.py` — new: fault-injection harness wrapping PaperExchange (kill points, network faults, ACK/fill mutation)
  - `tests/chaos/test_gateway_chaos.py` — the six §11.5 scenario families as parametrized suites
  - `tests/chaos/invariants.py` — post-run assertions: no duplicates, no tokenless orders, no net-short, reservations balanced
  - `.github/workflows/ci.yml` — add the chaos suite as a required job (marker `-m chaos`)
- **Prior decisions:** All randomness is seeded and the seed is logged on failure (guiding principle §3.5: "all randomness seeded and logged") so any chaos failure reproduces deterministically. The suite tests through public process interfaces only — no reaching into Gateway internals — so it stays valid as internals evolve. Invariant checks read the ledger and PaperExchange state, the same sources reconciliation uses.
- **State of the world:** After issue 05 every Gateway feature exists with unit and integration tests, including a crash-recovery matrix; there is no combined fault-injection suite crossing failure modes (e.g., duplicate ACK arriving during recovery after a mid-submit network cut).

## Output Format

Deliverable is a single PR containing:

- [ ] Fault-injection harness with named, composable fault types
- [ ] Parametrized chaos suites covering all six §11.5 scenario families, individually and in randomized (seeded) combination
- [ ] `invariants.py` asserting the four §11.5 invariants after every scenario
- [ ] CI wiring: chaos suite runs and gates merges
- [ ] A short `tests/chaos/README.md` documenting how to reproduce a failed seed locally
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
@pytest.mark.chaos
@pytest.mark.parametrize("seed", CHAOS_SEEDS)
def test_random_fault_storm_preserves_invariants(seed, chaos_harness):
    run = chaos_harness.run(
        intents=random_intent_stream(seed, n=200),
        faults=random_faults(seed, kinds=ALL_FAULT_KINDS),
    )
    assert_no_duplicate_live_orders(run)
    assert_no_tokenless_orders(run)
    assert_no_net_short_positions(run)
    assert_reservations_balanced(run)   # every reservation released or consumed exactly once
```

## Constraints

**Scope fence:** Do not modify Gateway production code to make tests pass except to fix genuine bugs the suite exposes — each such fix must be a separately-reviewable commit within the PR with the failing seed in its message. Do not test Kernel-internal logic (EPIC_04 owns its own property suite). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. This issue adds tests and CI wiring; production behavior changes only via explicitly-justified bug-fix commits.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`), including the chaos suite.
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%.
- [ ] `mypy --strict` clean (test code included).
- [ ] Chaos suite is a required CI job; a red chaos run blocks merge.
- [ ] PR body includes `Refs #6` and `Closes #42`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `order-gateway`
