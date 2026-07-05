## Role

You are a senior Python engineer with market-microstructure experience, working in this repo's `hedgekit/selector/` package on adverse-selection defenses.

## Goal

The selector enforces price bands (no opens below `min_open_price_pips` or above `max_open_price_pips`), chooses execution style per §9.7 (`cross` default; `rest_inside_spread` only when the spread is wide and edge persists at the improved price, always with `resting_ttl_seconds` and `cancel_on_move_ticks` set), and generates `SELL_TO_CLOSE` intents only from the three legitimate v1 sources — kill path, Kernel de-risking directive, explicit operator command — never from strategy logic.

## Context

- **Parent epic:** #7
- **Predecessor issue(s):** #45 (must be merged first — sizing determines whether a resting order still clears the hurdle at the improved price)
- **SPEC section:** `plans/SPEC_v3.md` §9.4 (price bands, favorite-longshot rationale), §9.7 (execution style & adverse-selection controls, T13), §9.8 (exits), §4 row T13, §16 keys `min_open_price_pips`, `max_open_price_pips`, `resting_order_ttl_seconds`, `cancel_on_move_ticks`
- **Files involved:**
  - `hedgekit/selector/execution_style.py` — cross vs. rest decision, TTL/cancel-on-move stamping (new)
  - `hedgekit/selector/entry.py` — replace the price-band stub from #44 with real enforcement
  - `hedgekit/selector/exits.py` — close-intent construction gated to the three legitimate trigger sources, reduce-only always (new)
  - `tests/selector/test_price_bands.py`, `tests/selector/test_execution_style.py`, `tests/selector/test_exits.py`
- **Prior decisions:** a fundamentals bot resting passively is a free option for faster traders (§2, T13) — `cross` is the default and resting is the exception. Close intents are reduce-only *always* (§6.4); the volatility-freeze path (market moved > threshold since intent creation → cancel and return market to screener) is Gateway/sweeper behavior, but the selector must stamp every resting intent with the fields the sweeper needs.
- **State of the world:** edge, entry conditions, and sizing work; every intent is currently emitted as `cross` with band checks stubbed; no close-intent machinery exists.

## Output Format

Deliverable is a single PR containing:

- [ ] Band enforcement in `entry.py`: opens outside `[min_open_price_pips, max_open_price_pips]` are rejected with a named reason
- [ ] `execution_style.py`: deterministic style decision from spread width and edge-at-improved-price; every `rest_inside_spread` intent carries `resting_ttl_seconds` and `cancel_on_move_ticks` from config
- [ ] `exits.py`: `build_close_intent(trigger: CloseTrigger, position, …)` where `CloseTrigger` is an enum of exactly `KILL_PATH | KERNEL_DERISK | OPERATOR_COMMAND`; `SELL_TO_CLOSE` intents are reduce-only and sized ≤ current position
- [ ] Tests: band boundary cases (exactly 500/9500 pips pass; 499/9501 fail); style decision table; a test proving no code path in the selector can construct a close intent without a `CloseTrigger`
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_no_open_below_band(inputs_with_executable_price_pips(499)):
    decision = select(inputs_with_executable_price_pips(499))
    assert decision.intents == ()
    assert "price_below_min_open_band" in [r.code for r in decision.reasons]

def test_resting_intent_carries_adverse_selection_fields(wide_spread_inputs):
    (intent,) = select(wide_spread_inputs).intents
    assert intent.execution_style == "rest_inside_spread"
    assert intent.resting_ttl_seconds == 900
    assert intent.cancel_on_move_ticks == 2

def test_strategy_logic_cannot_close():
    # The only public constructor requires a CloseTrigger; there is no
    # code path from select() that emits SELL_TO_CLOSE.
    decision = select(profitable_open_position_inputs)
    assert all(i.action == "BUY_TO_OPEN" for i in decision.intents)
```

## Constraints

**Scope fence:** Do not implement the Gateway-side sweeper (cancel on TTL/move — that is EPIC_05's scope), strategy-driven early exits (post-v1, §19), or correlation buckets (#47). If you find yourself touching `hedgekit/order_gateway/`, stop.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges; golden determinism tests still pass (documented regeneration only).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Docstrings cite §9.4/§9.7/§9.8 including the free-option rationale.
- [ ] PR body includes `Refs #7` and `Closes #46`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `selector`
