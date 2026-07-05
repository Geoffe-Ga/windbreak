## Role

You are a senior Python engineer with a statistics background (forecast verification, bootstrap methods), working in this repo's `hedgekit/evaluation/` package (mypy --strict, `hypothesis` available for property tests).

## Goal

All SPEC §13.5 metrics — Brier, log score, Brier skill score, expected calibration error, calibration slope/intercept, reliability diagram data, sharpness, per-price-bucket calibration and PnL, edge-bucket performance — compute correctly on the synthetic known-answer fixture, with bootstrap CIs clustered by event/correlation group, plus a documented power analysis at N=300.

## Context

- **Parent epic:** #8
- **Predecessor issue(s):** #50 (must be merged first — metrics consume resolutions and baselines).
- **SPEC section:** `plans/SPEC_v3.md` §13.5 (statistical machinery), §9.4 (price buckets), §13.2 (skill is measured against the executable-price baseline), §10.9 (gate thresholds these metrics feed), §21 glossary (clustered bootstrap).
- **Files involved:**
  - `hedgekit/evaluation/metrics.py` — new: scoring rules + calibration statistics
  - `hedgekit/evaluation/bootstrap.py` — new: cluster bootstrap over event/correlation groups
  - `hedgekit/evaluation/power.py` — new: power analysis (minimum detectable Brier skill at N=300 given observed clustering), rendered into the report
  - `hedgekit/evaluation/registry.py` — register the real metrics, replacing `NOT_IMPLEMENTED` sentinels
  - `tests/evaluation/test_metrics.py`, `tests/evaluation/test_bootstrap.py` — known-answer + property tests
  - `tests/evaluation/fixtures/clustered_fixture.json` — new fixture with *known correlation structure* (e.g., 3 event groups of perfectly correlated markets) whose correct clustered CI is derivable by hand
- **Prior decisions:** cluster by `mutually_exclusive_group_id`/correlation bucket so related markets don't masquerade as independent (§13.5); all randomness seeded and logged (§3.5); mixing observation windows in one metric is a test failure (§13.4 — enforcement lands in #53, but design the metric API to take a window parameter now).
- **State of the world:** registry, report, resolutions, and baselines are real; every metric slot still returns `NOT_IMPLEMENTED`.

## Output Format

Deliverable is a single PR containing:

- [ ] All §13.5 metrics implemented and registered
- [ ] Clustered bootstrap with seeded RNG; naive (unclustered) bootstrap deliberately NOT exposed
- [ ] Power-analysis document generated from code into the report (not a hand-written prose file)
- [ ] Known-answer tests: every metric matches hand-computed values on the synthetic fixtures; clustered CI on the correlated fixture is wider than the (internal-only) unclustered CI
- [ ] Property tests: Brier ∈ [0, 1]; skill score of the baseline against itself is 0; ECE of a perfectly calibrated synthetic set is 0
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_clustered_ci_respects_correlation_groups() -> None:
    result = brier_skill_ci(clustered_fixture(), confidence_ppm=950_000, seed=42)
    # 3 independent clusters, not 30 independent markets:
    assert result.effective_n == 3
    assert result.ci_width > unclustered_width_reference()
```

## Constraints

**Scope fence:** Do not implement temporal-integrity filtering (#52), observation-window enforcement (#53), or the SQL dual path (#55). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

**Determinism:** identical inputs + seed produce byte-identical CIs (SPEC §3.5).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #8` and `Closes #51`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `evaluation`
