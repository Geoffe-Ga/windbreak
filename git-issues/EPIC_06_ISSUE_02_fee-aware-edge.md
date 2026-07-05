## Role

You are a senior Python engineer with market-microstructure experience, working in this repo's `hedgekit/selector/` package on fixed-point financial arithmetic.

## Goal

`select()` computes the fee-aware executable edge by walking the actual recorded order book to the proposed size — never the midpoint — producing `gross_edge`, `fee_adjusted_edge`, `slippage_adjusted_edge`, `research_cost_adjusted_edge`, and `annualized_expected_return`, and emits a BUY_TO_OPEN intent only when every §9.3 entry condition passes.

## Context

- **Parent epic:** #EPIC_06_NUMBER
- **Predecessor issue(s):** #EPIC_06_ISSUE_01_NUMBER (must be merged first — provides `SelectorInputs`/`SelectorDecision` and the determinism harness)
- **SPEC section:** `plans/SPEC_v3.md` §9.2 (fee-aware executable edge), §9.3 (entry conditions, all required), §2 (why fees/microstructure are first-class), §16 `risk:` keys `min_net_edge_ppm`, `annualized_hurdle_ppm`, `idle_cash_apr_ppm`
- **Files involved:**
  - `hedgekit/selector/edge.py` — book-walking and edge arithmetic (new)
  - `hedgekit/selector/entry.py` — §9.3 entry-condition evaluation returning a per-condition pass/fail record (new)
  - `hedgekit/selector/__init__.py` — `select()` calls edge → entry conditions → (still-stubbed sizing)
  - `tests/selector/test_edge.py`, `tests/selector/test_entry_conditions.py`
- **Prior decisions:** all arithmetic in fixed-point integer units (§6.1) with rounding always conservative — overstate cost, understate edge. The annualization hurdle is compared net of `idle_cash_apr_ppm` so trades must beat parked capital, not zero (§9.2). Freshness is judged from timestamps carried in `SelectorInputs`, never a live clock read.
- **State of the world:** `select()` is a stub returning zero intents with reasons; the golden harness exists and must keep passing.

## Output Format

Deliverable is a single PR containing:

- [ ] `hedgekit/selector/edge.py`: walk the book's asks (for YES buys) / bids side to the proposed size, accumulating executable cost level by level; fail with an explicit reason if depth is insufficient; compute all five edge figures
- [ ] `hedgekit/selector/entry.py`: every §9.3 condition — `net_edge ≥ min_net_edge`, annualized return ≥ hurdle net of idle-cash yield, forecast CI does not straddle the executable price, quote/forecast/fee-model freshness, market eligibility (jurisdiction, category, coherence, citation support), price bands (band values consumed here, enforcement logic may be a stub until #EPIC_06_ISSUE_04_NUMBER), `forecast.eligible_for_live` for live modes — each producing a named pass/fail entry in `SelectorDecision.reasons`
- [ ] Tests: hand-computed edge fixtures (book + fees → expected edges, computed in the test by hand, not by calling the code under test); negative tests for each failed entry condition
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_walks_book_not_midpoint():
    # Book: 100 contracts at 45¢, 100 at 47¢. Proposed size: 150 contracts.
    # Executable cost = 100·4500 + 50·4700 pips, NOT 150·midpoint.
    result = compute_executable_edge(book_fixture_two_levels, size_centis=15000,
                                     forecast_ppm=550_000, fee_model=kalshi_fees)
    assert result.executable_price_pips == weighted(4500, 100, 4700, 50)

def test_no_intent_when_ci_straddles_price(inputs_with_wide_ci):
    decision = select(inputs_with_wide_ci)
    assert decision.intents == ()
    assert "ci_straddles_executable_price" in [r.code for r in decision.reasons]
```

## Constraints

**Scope fence:** Do not implement sizing (that is #EPIC_06_ISSUE_03_NUMBER — emit a fixed 1-contract probe size behind a clearly named placeholder so intents are shape-complete), price-band/execution-style logic beyond consuming config values (#EPIC_06_ISSUE_04_NUMBER), or correlation caps (#EPIC_06_ISSUE_05_NUMBER). No floats anywhere on money/price/probability paths.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges: golden determinism tests from #EPIC_06_ISSUE_01_NUMBER still pass byte-identically (update goldens only via the documented regeneration script, with the diff explained in the PR body).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Edge formulas documented in docstrings with SPEC § citations.
- [ ] PR body includes `Refs #EPIC_06_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `selector`
