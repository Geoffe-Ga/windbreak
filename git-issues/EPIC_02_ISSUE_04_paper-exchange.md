## Role

You are a senior Python engineer working in this repo's `hedgekit/connector/` subpackage, experienced in market-microstructure simulation and property-based testing with `hypothesis`.

## Goal

A `PaperExchange` adapter replays recorded real order books and simulates fills pessimistically per SPEC §17.4 — taker fills walk the recorded book and pay live-schedule fees plus a +25%-of-fees haircut; resting orders fill only on trade-through — with a property test proving no simulated fill is ever better than the recorded book allows.

## Context

- **Parent epic:** #3
- **Predecessor issue(s):** #18 (must be merged first — fee model exists to charge against)
- **SPEC section:** `plans/SPEC_v3.md` §7.5 (PaperExchange), §17.4 (paper-fill realism model — normative), §9.5 (participation caps apply in simulation exactly as live: `max_participation_ppm` default 250000), §4 T13 (adverse selection — the resting model exists to charge for it), §13.6 (any change to this model re-registers the gate plan)
- **Files involved:**
  - `hedgekit/connector/paper.py` — `PaperExchange(MarketConnector)` replaying recorded books
  - `hedgekit/connector/fills.py` — taker walk + haircut, resting trade-through logic (pure functions, separately testable)
  - `tests/connector/test_paper_exchange.py` — behavior tests
  - `tests/connector/test_paper_fill_properties.py` — hypothesis property suite
  - `tests/fixtures/books/` — recorded order-book sequences including trade prints (needed to detect trade-through)
- **Prior decisions:** Fees come from ISSUE_03's `FeeModel` at the live schedule; the haircut default is +25% of modeled fees and must be config, not a constant (§17.4 default). A touch is NOT a fill for resting orders — only trading through the limit price fills, approximating queue loss and adverse selection. All arithmetic fixed-point (§6.1), rounding against the trader.
- **State of the world:** Interface, FakeExchange, Kalshi adapter, and fee model all exist. No fill simulation exists.

## Output Format

Deliverable is a single PR containing:

- [ ] Taker simulation: walks the recorded book level-by-level at recorded depth to the requested size, pays live-schedule fees + configurable pessimism haircut (default +25% of modeled fees)
- [ ] Resting simulation: fills only when the recorded market trades through the limit price; touch ≠ fill, with a fixture test proving the distinction
- [ ] Participation cap enforced in simulation: fills never exceed `max_participation_ppm` of resting size at-or-better than the limit
- [ ] Partial-fill representation consistent with the `BalanceSemantics` record from ISSUE_03
- [ ] Hypothesis property test: for any recorded book and any order, the simulated average fill price is never better (lower for buys) than walking the recorded book, and simulated cost is never below book cost + modeled fees
- [ ] A `PAPER_FILL_MODEL_VERSION` constant/hash exposed so EPIC_07 (evaluation) can detect model changes for gate re-registration (§13.6)
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands**

```python
def test_touch_is_not_a_fill(paper_exchange, book_touching_limit):
    order = paper_exchange.rest_order("MKT-A", side="YES", limit_price_pips=4200, count_centis=1000)
    paper_exchange.replay(book_touching_limit)  # best ask touches 4200, never trades through
    assert paper_exchange.get_fills(since=0) == []

@given(recorded_books(), orders())
def test_no_fill_better_than_recorded_book(book, order):
    fill = simulate_taker_fill(book, order, fee_model, haircut_ppm=250_000)
    assert fill.avg_price_pips >= walk_book_best_possible(book, order).avg_price_pips
```

## Constraints

**Scope fence:** Do not implement selector edge math (EPIC_06/M5), Gateway order lifecycle (EPIC_05/M4), or the book-recording daemon beyond what fixtures need. Do not soften the pessimism model to make future paper PnL look better — §17.4 is normative and changes re-register promotion gates. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. If your change breaks an unrelated surface, revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #3` and `Closes #19`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `connector`
