## Role

You are a senior Python systems engineer working in this repo's `hedgekit/` package, experienced with multi-process daemons, state machines, and strict-typed Python (mypy --strict).

## Goal

A `riskkernel` process exists as an independently runnable service with the full mode state machine from SPEC §10.2 and a stub per-order check pipeline that **vetoes every intent**, proving the veto path end-to-end before any approval logic exists.

## Context

- **Parent epic:** #5
- **Predecessor issue(s):** none — this is the skeleton issue (requires EPIC_01/M0 foundations: ledger, config loader, structured logging)
- **SPEC section:** `plans/SPEC_v3.md` §10.1–§10.3, §5.1 (process topology), §5.3 (import-boundary CI test), §18 M3
- **Files involved:**
  - `hedgekit/riskkernel/__init__.py` — new package; the ONLY package allowed to import the signing-key handle (enforced later; scaffold the boundary now)
  - `hedgekit/riskkernel/process.py` — entrypoint (`python -m hedgekit.riskkernel`), heartbeat loop, ledger wiring
  - `hedgekit/riskkernel/modes.py` — mode state machine
  - `hedgekit/riskkernel/checks.py` — check-pipeline scaffold, every check returns VETO (stub)
  - `tests/riskkernel/test_modes.py`, `tests/riskkernel/test_process_isolation.py`, `tests/riskkernel/test_checks_stub.py`
  - `plans/architecture/.importlinter` — add contract: only `hedgekit.riskkernel` may import the (stubbed) signing-key module
- **Prior decisions:** Process isolation is mandatory (§5.1): killing Process A must not kill the Kernel. The Kernel never fetches web content, never calls LLMs, never holds trade credentials (§10.1). Every check *error* fails closed (§10.3).
- **State of the world:** `hedgekit/` contains only the generated `main.py` hello-world stub. The ledger and config loader come from EPIC_01. Nothing Kernel-shaped exists.

## Output Format

Deliverable is a single PR containing:

- [ ] `hedgekit/riskkernel/` package with process entrypoint, mode state machine, and stub check pipeline
- [ ] Mode state machine implementing exactly: `RESEARCH → PAPER → LIVE_MICRO → LIVE`; any mode → `PAUSED | HALT | KILLED`; `KILLED` re-arm is a typed-confirmation stub; `mode_ceiling` from config bounds all transitions upward (§10.2)
- [ ] Stub `evaluate_intent(intent) -> Veto` that runs the §10.3 check list as named no-op checks, each returning VETO with reason `"not implemented"`, and ledgers the veto
- [ ] Heartbeat event written to the ledger on an interval; import-linter contract added
- [ ] Tests in `tests/riskkernel/` proving: all transitions in the matrix (legal and illegal); every intent is vetoed; kernel process starts, heartbeats, and keeps running when a simulated Process A dies
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test cases that should pass after this issue lands**

```python
def test_mode_ceiling_bounds_promotion():
    sm = ModeStateMachine(mode_ceiling=Mode.PAPER)
    sm.transition(Mode.PAPER)
    with pytest.raises(ModeCeilingExceeded):
        sm.transition(Mode.LIVE_MICRO)

def test_stub_pipeline_vetoes_everything():
    kernel = RiskKernel.for_testing()
    decision = kernel.evaluate_intent(make_intent())
    assert decision.vetoed and decision.ledgered
```

## Constraints

**Scope fence:** Do not implement floor math (issue #30), reservations/tokens (#31), exchange verification (#32), promotion logic (#33), governance (#34), or kill triggers (#35). Every check stub vetoes. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. `hedgekit run` (Process A) must keep idling in RESEARCH with heartbeats while the Kernel runs beside it.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%; `riskkernel` targets 100% branch coverage (§17.6).
- [ ] `mypy --strict` clean; public APIs have docstrings.
- [ ] PR body includes `Refs #5` and `Closes #29`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer Action is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `risk-kernel`
