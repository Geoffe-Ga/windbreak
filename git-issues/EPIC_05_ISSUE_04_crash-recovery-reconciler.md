## Role

You are a senior Python engineer working in this repo's `hedgekit/order_gateway/` package, experienced with write-ahead logging, crash-consistent recovery, and reconciliation loops.

## Goal

The Gateway survives death at any point in the order lifecycle: a write-ahead intent log plus startup reconciliation (ledger state vs exchange open orders, positions, and fills since checkpoint) restores consistent state before any new approval is accepted, halting on unexplained mismatch; a continuous Reconciler (default 60s) auto-heals known-benign cases and halts otherwise (SPEC §11.4, T9).

## Context

- **Parent epic:** #EPIC_05_NUMBER
- **Predecessor issue(s):** #EPIC_05_ISSUE_03_NUMBER (must be merged first — reduce-only path, so recovery covers close orders too)
- **SPEC section:** `plans/SPEC_v3.md` §11.4 (crash recovery: load ledger → fetch exchange state → reconcile → halt on mismatch → only then accept approvals; continuous Reconciler), §4 rows T9 (crash mid-order) and T3 (reconciliation loop as runaway-order mitigation), §11.3 (`RECONCILED`/`DISPUTED` terminal states), §10.5 (reservation release/adjustment on reconciliation)
- **Files involved:**
  - `hedgekit/order_gateway/wal.py` — new: write-ahead intent log written before `SUBMISSION_REQUESTED`
  - `hedgekit/order_gateway/recovery.py` — new: startup sequence per §11.4
  - `hedgekit/order_gateway/reconciler.py` — new: continuous loop, benign-case allowlist (e.g., missed fill notification), halt on everything else
  - `hedgekit/order_gateway/gateway.py` — refuse new approvals until recovery completes
  - `tests/order_gateway/test_recovery.py` — kill-between-every-pair-of-states scenarios using PaperExchange
  - `tests/order_gateway/test_reconciler.py` — benign auto-heal vs unexplained-mismatch halt
- **Prior decisions:** Deterministic client order IDs from #EPIC_05_ISSUE_02_NUMBER are the join key between WAL entries and exchange open orders. "Benign" is a closed allowlist, not a heuristic — anything not on it halts (guiding principle §3.2: "When in doubt, halt and alert"). Reservation adjustments flow through the Kernel's ledger interfaces from EPIC_04; this issue consumes them, it does not reimplement them.
- **State of the world:** After issue 03 the Gateway submits and validates orders but restarts blind: an intent submitted-but-unacked before a crash would be lost or double-submitted.

## Output Format

Deliverable is a single PR containing:

- [ ] `wal.py`, `recovery.py`, `reconciler.py` with the §11.4 startup ordering enforced
- [ ] Gateway refuses approvals until recovery completes (tested)
- [ ] Kill-at-every-state-edge test matrix over PaperExchange proving zero lost and zero duplicated orders
- [ ] Continuous Reconciler with ledgered auto-heal events and halt-on-mismatch
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
@pytest.mark.parametrize("kill_after", ORDER_STATE_EDGES)
def test_crash_recovery_converges(kill_after, paper_exchange, ledger):
    gw = Gateway(paper_exchange, ledger)
    gw.submit_until(kill_after, approved_intent())   # dies at this edge
    recovered = Gateway(paper_exchange, ledger)      # fresh process
    recovered.recover()
    assert paper_exchange.order_count() <= 1                     # never duplicated
    assert recovered.state_of(intent_id) in CONSISTENT_STATES    # never lost
    assert recovered.accepting_approvals is True

def test_unexplained_exchange_order_halts(paper_exchange, ledger):
    paper_exchange.inject_foreign_order("mystery-123")           # no matching intent
    gw = Gateway(paper_exchange, ledger)
    gw.recover()
    assert gw.halted is True
    assert ledger.last_event().event_type == "RECONCILIATION_HALT"
```

## Constraints

**Scope fence:** Do not implement the sweeper (issue #EPIC_05_ISSUE_05_NUMBER) or extend the chaos suite beyond the recovery matrix (issue #EPIC_05_ISSUE_06_NUMBER covers network cuts, duplicate ACKs, and fill races). Do not add Kernel-side reservation logic. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. Normal (no-crash) submission behavior is unchanged; recovery only adds a startup phase and a background loop.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%.
- [ ] `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #EPIC_05_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `order-gateway`
