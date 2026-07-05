## Role

You are a senior Python engineer with market-data plumbing experience, working in this repo's `hedgekit/evaluation/` package (mypy --strict, integer fixed-point units per SPEC §6.1).

## Goal

The resolution tracker ingests market resolutions from ledger events — including `SETTLEMENT_REVERSED` corrections (SPEC T16) — and the baseline suite (§13.2) produces, for every forecast, the five comparison baselines keyed to that forecast's `baseline_quote_snapshot_id`.

## Context

- **Parent epic:** #EPIC_07_NUMBER
- **Predecessor issue(s):** #EPIC_07_ISSUE_01_NUMBER (must be merged first — replaces its resolution stub).
- **SPEC section:** `plans/SPEC_v3.md` §13.2 (baselines), §4 T16 (resolution reversal), §11.3 (`SETTLEMENT_REVERSED` order state), §6.3 (`ForecastRecord.market_price_baseline_pips`, `baseline_quote_snapshot_id`).
- **Files involved:**
  - `hedgekit/evaluation/resolution.py` — replace stub with real tracker over ledger read models
  - `hedgekit/evaluation/baselines.py` — new: the five baselines (executable price at baseline snapshot [primary]; midpoint at forecast time; uniform 0.5; base-rate model where available; previous forecast for same market)
  - `tests/evaluation/test_resolution.py`, `tests/evaluation/test_baselines.py`
  - `tests/evaluation/fixtures/` — extend the synthetic fixture with a reversal scenario
- **Prior decisions:** resolutions are ledger events, never mutated in place — a reversal appends a `SETTLEMENT_REVERSED` event and the tracker recomputes derived state (SPEC §12 append-only). The primary baseline is the *executable* price, not midpoint (§13.2).
- **State of the world:** `resolution.py` is a typed stub returning fixture data; `registry.py` and `report.py` exist from the skeleton.

## Output Format

Deliverable is a single PR containing:

- [ ] Real resolution tracker: resolved / unresolved / reversed states derived purely from ledger events
- [ ] Baseline computation for all five baselines, integer `ProbabilityPpm`/`PricePips` in and out
- [ ] Tests proving: a reversal flips a market's resolved outcome and every downstream metric recomputes; a forecast with no prior forecast omits (not zero-fills) the previous-forecast baseline; primary baseline reads the executable price at the forecast's own `baseline_quote_snapshot_id`, never a later snapshot
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_settlement_reversal_recomputes_outcome() -> None:
    tracker = ResolutionTracker.from_ledger(events_with_reversal())
    market = tracker.get("KXEXAMPLE-26-T1")
    assert market.status is ResolutionStatus.RESOLVED
    assert market.outcome is Outcome.NO  # reversed from YES
    assert market.reversal_count == 1
```

## Constraints

**Scope fence:** Do not compute any scores from these baselines (issue #EPIC_07_ISSUE_03_NUMBER) and do not implement temporal-integrity filtering (#EPIC_07_ISSUE_04_NUMBER). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #EPIC_07_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `evaluation`
