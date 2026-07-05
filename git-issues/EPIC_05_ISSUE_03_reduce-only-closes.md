## Role

You are a senior Python engineer working in this repo's `hedgekit/order_gateway/` package, experienced with defensive validation in financial order paths.

## Goal

Every `SELL_TO_CLOSE` order is provably reduce-only: the Gateway sets the exchange's reduce-only flag when the adapter exposes one, independently validates count against the current position immediately before submission regardless, and re-verifies position size after fills — so a net-short position is impossible by construction (SPEC §6.4, §11.2).

## Context

- **Parent epic:** #6
- **Predecessor issue(s):** #38 (must be merged first — real submission path)
- **SPEC section:** `plans/SPEC_v3.md` §6.4 ("SELL_TO_CLOSE is reduce-only, always"; "If the exchange lacks a reduce-only flag, the Gateway enforces reduce-only locally immediately before submission and re-verifies position size after fills"), §11.2 (reduce-only enforcement requirement), §1.1-2 (bounded-loss invariant), §11.5 (zero net-short acceptance criterion)
- **Files involved:**
  - `hedgekit/order_gateway/reduce_only.py` — new: pre-submission position check + post-fill re-verification
  - `hedgekit/order_gateway/gateway.py` — wire the check into the `SELL_TO_CLOSE` path; `BUY_TO_OPEN` is untouched
  - `tests/order_gateway/test_reduce_only.py` — close ≤ position passes; close > position refused; concurrent partial fills shrink the closeable amount; post-fill mismatch halts
- **Prior decisions:** Position reads go through the connector's `get_positions()` (§7.2). Fixed-point `ContractCentis` integers only — no floats anywhere in the count math (§6.1, §17.3). The Kernel also checks "reduce-only provable for closes" (§10.3); this Gateway check is the independent second layer (defense in depth), not a replacement.
- **State of the world:** After issue 02, the Gateway submits both open and close intents to PaperExchange with no position-awareness; a close larger than the held position would currently pass through.

## Output Format

Deliverable is a single PR containing:

- [ ] `reduce_only.py` with pre-submission validation and post-fill re-verification
- [ ] Refusals ledgered as explicit events with the position snapshot that justified them
- [ ] Post-fill verification mismatch triggers the Gateway halt path
- [ ] Tests covering exact-size close, oversized close, partial-fill races, and the no-flag exchange path
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_close_exceeding_position_is_refused(gateway, positions, close_intent_factory):
    positions.set("KXFED-25DEC-T3.00", yes_contract_centis=500)
    oversized = close_intent_factory(count_centis=600)
    assert gateway.submit(oversized) is SubmitResult.REFUSED_REDUCE_ONLY

def test_partial_fill_shrinks_closeable(gateway, positions, close_intent_factory):
    positions.set("KXFED-25DEC-T3.00", yes_contract_centis=500)
    gateway.submit(close_intent_factory(count_centis=300))   # in flight
    second = close_intent_factory(count_centis=300)          # 300+300 > 500
    assert gateway.submit(second) is SubmitResult.REFUSED_REDUCE_ONLY
```

## Constraints

**Scope fence:** Do not implement strategy-driven exits — SPEC §9.8 keeps those post-v1; close intents originate only from kill/de-risk/operator paths, and this issue only enforces their reduce-only property. Do not modify the Risk Kernel's own reduce-only check. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. `BUY_TO_OPEN` submission behavior is byte-for-byte unchanged; only the close path gains validation.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%.
- [ ] `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #6` and `Closes #39`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `order-gateway`
