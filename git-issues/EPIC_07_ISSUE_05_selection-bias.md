## Role

You are a senior Python engineer with forecast-evaluation experience, working in this repo's `hedgekit/evaluation/` package (mypy --strict). You are guarding against the subtlest failure mode in §13: fooling ourselves with the sample we chose to look at.

## Goal

Every eligible forecast is scored — not only traded ones — with reports split by cohort (all / above-threshold / traded / skipped / abstained / excluded-by-category / excluded-by-liquidity), abstentions counterfactually scored against resolution (SPEC §13.3), and precommitted observation windows enforced such that mixing windows in one metric is a test failure (§13.4).

## Context

- **Parent epic:** #8
- **Predecessor issue(s):** #51 and #52 (must be merged first — needs real metrics and the temporal gate).
- **SPEC section:** `plans/SPEC_v3.md` §13.3 (selection-bias controls), §13.4 (observation windows: first-per-market, latest-before-close, daily snapshots, trade-triggering; "the headline Brier metric names its window"), §6.3 (`abstention_reason` is a first-class outcome), §13.1 (selection-quality track: did traded forecasts outperform skipped ones?).
- **Files involved:**
  - `hedgekit/evaluation/cohorts.py` — new: cohort assignment from ledger events (selector decisions, screen decisions)
  - `hedgekit/evaluation/windows.py` — new: the four declared observation windows as an enum + window resolver; every metric call site must name one
  - `hedgekit/evaluation/abstention.py` — new: counterfactual scoring of abstentions (was abstaining wise, given resolution?)
  - `hedgekit/evaluation/report.py` — extend the selection-quality track with per-cohort tables
  - `tests/evaluation/test_cohorts.py`, `tests/evaluation/test_windows.py`, `tests/evaluation/test_abstention.py`
- **Prior decisions:** the metric API from issue 3 already takes a window parameter — this issue makes it mandatory and type-safe (no default window; callers must choose). Headline Brier uses `latest_before_close` per the generated config (`evaluation.observation_window`).
- **State of the world:** metrics compute on unfiltered-but-temporally-valid records with a window parameter that is currently accepted but unenforced.

## Output Format

Deliverable is a single PR containing:

- [ ] Cohort assignment for all seven cohorts, derived purely from ledger events
- [ ] Window enforcement: a metric computed from records spanning two windows raises `MixedObservationWindowError`; the report labels every metric with its window
- [ ] Abstention counterfactual scoring integrated into the selection-quality track
- [ ] Tests: traded-vs-skipped comparison on a fixture where skipped forecasts were (by construction) better — report must say so; window-mixing raises; abstention scored correctly for both wise and unwise abstentions
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_mixing_observation_windows_is_a_failure() -> None:
    records = fixture_records(windows=["first_per_market", "latest_before_close"])
    with pytest.raises(MixedObservationWindowError):
        brier(records)  # caller failed to resolve to a single declared window
```

## Constraints

**Scope fence:** Do not implement the pre-registration hash flow (#54) or dual-path computation (#55). If you find yourself touching files outside the list above, stop and check with the user.

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
- [ ] PR body includes `Refs #8` and `Closes #53`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `evaluation`
