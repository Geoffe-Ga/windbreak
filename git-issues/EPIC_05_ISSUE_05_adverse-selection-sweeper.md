## Role

You are a senior Python engineer working in this repo's `hedgekit/order_gateway/` package, experienced with market microstructure and order-lifecycle management.

## Goal

A stale-order sweeper cancels any resting order whose `resting_ttl_seconds` has lapsed or whose market has moved beyond `cancel_on_move_ticks` since intent creation, and a market-level volatility freeze cancels and returns the market to the screener for re-forecast — closing the Gateway side of threat T13 (adverse selection / pick-off), verified against recorded volatile-market fixtures (SPEC §9.7, §11.2, §11.5).

## Context

- **Parent epic:** #6
- **Predecessor issue(s):** #40 (must be merged first — the sweeper's cancels must survive crashes via the WAL/Reconciler)
- **SPEC section:** `plans/SPEC_v3.md` §9.7 (execution style & adverse-selection controls: TTL default 900s, `cancel_on_move_ticks` default 2, volatility freeze), §11.2 ("run the adverse-selection sweeper — cancel resting orders on TTL expiry or cancel_on_move_ticks breach"), §11.5 (sweeper verified against recorded volatile-market fixtures), §4 row T13, §2 rationale ("a resting limit order… is a free option for anyone with faster news access")
- **Files involved:**
  - `hedgekit/order_gateway/sweeper.py` — new: periodic scan of resting orders; TTL and price-move checks; cancel via the §11.3 `CANCEL_REQUESTED → CANCELLED` path
  - `hedgekit/order_gateway/gateway.py` — start/stop sweeper with the process; expose freeze events
  - `tests/order_gateway/test_sweeper.py` — TTL expiry, tick-move breach, cancel/fill race (order fills as cancel is issued), freeze-and-return-to-screener event
  - `tests/fixtures/volatile_markets/` — recorded volatile-market order-book fixtures (from the M1 fixture corpus)
- **Prior decisions:** TTL and tick parameters ride on each `NormalizedOrderIntent` (`resting_ttl_seconds`, `cancel_on_move_ticks`, §6.4) — the sweeper reads them from the intent, never from global config, so per-intent overrides stay honored. Price-move reference is the intent's `quote_snapshot_id` baseline. The "return to screener" is a ledgered event the pipeline consumes; the Gateway does not call screener code (§5.1 process isolation).
- **State of the world:** After issue 04 the Gateway submits, recovers, and reconciles, but a resting `rest_inside_spread` order stays on the book indefinitely — free option for faster traders.

## Output Format

Deliverable is a single PR containing:

- [ ] `sweeper.py` with TTL and cancel-on-move sweeps through the existing cancel path
- [ ] Volatility-freeze event emission (ledgered `MARKET_FREEZE` + return-to-screener event)
- [ ] Cancel/fill race handled: a fill arriving during `CANCEL_REQUESTED` resolves per the §11.3 state machine without double-counting
- [ ] Tests against recorded volatile-market fixtures proving stale orders are cancelled within one sweep interval
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_ttl_expiry_cancels(gateway, paper_exchange, resting_intent_factory, clock):
    intent = resting_intent_factory(resting_ttl_seconds=900)
    gateway.submit(intent)
    clock.advance(seconds=901)
    gateway.sweep()
    assert gateway.state_of(intent.intent_id) == OrderState.CANCELLED

def test_cancel_on_move_breach(gateway, paper_exchange, resting_intent_factory):
    intent = resting_intent_factory(cancel_on_move_ticks=2, limit_price_pips=4200)
    gateway.submit(intent)
    paper_exchange.move_market("KXFED-25DEC-T3.00", by_ticks=3)
    gateway.sweep()
    assert gateway.state_of(intent.intent_id) == OrderState.CANCELLED
    assert ledger.contains(event_type="MARKET_FREEZE", ticker="KXFED-25DEC-T3.00")
```

## Constraints

**Scope fence:** Do not implement the selector's execution-style choice (`cross` vs `rest_inside_spread` is EPIC_06 / SPEC §9.7 selector side) or re-forecast logic — the sweeper only emits the return-to-screener event. Do not add new cancel mechanics; reuse the issue-02 cancel path. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. Orders with no TTL breach and no price move behave exactly as before; the sweeper is purely subtractive.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%.
- [ ] `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #6` and `Closes #41`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `order-gateway`
