## Role

You are a senior Python engineer working in this repo's `hedgekit/forecast/` package, experienced with model-drift monitoring and operational cost controls.

## Goal

A weekly canary set (~20 stable questions with reference answers/distributions) detects silent LLM provider drift — drift beyond tolerance alerts and marks subsequent forecasts `eligible_for_live=False` until operator acknowledgement — and research budgets (`per_forecast_micros`, `per_day_micros`, `max_pages`) are enforced with cost-per-resolved-forecast reporting (SPEC §8.6, §8.4, §16).

## Context

- **Parent epic:** #4
- **Predecessor issue(s):** #23 and #25 (must be merged first — cost ledgering and pinned ensemble exist).
- **SPEC section:** `plans/SPEC_v3.md` §8.6 (canary set, drift alerting), §4 T14 (silent drift) and T11 (cost blowout), §16 `forecast.canary` + `forecast.budget` config, §8.9 ("canary-drift alerting tested with synthetic drift"), §14 (canary status + cost meter are dashboard surfaces; mandatory canary-drift alert).
- **Files involved:**
  - `hedgekit/forecast/canary.py` — canary set runner, drift scoring vs. reference, ack state (new)
  - `hedgekit/forecast/budget.py` — per-forecast/per-day/max-pages enforcement (new)
  - `hedgekit/forecast/pipeline.py` — budget hooks; drift-unacked ⇒ records marked live-ineligible (modify)
  - `tests/forecast/test_canary.py`, `tests/forecast/test_budget.py` (new)
- **Prior decisions:** drift tolerance and cadence come from config (`canary: {enabled: true, cadence_days: 7}`); the ack is a ledgered operator event (dashboard/CLI wiring is EPIC_01's alert-sink abstraction — emit through it, don't build UI here); budget exhaustion fails closed: no partial "cheap mode" forecasts.
- **State of the world:** cost ledgering per forecast exists (issue 02); ensemble votes carry pinned versions + fingerprints (issue 04); no canary machinery, no budget ceilings enforced.

## Output Format

Deliverable is a single PR containing:

- [ ] Production code in `hedgekit/forecast/{canary,budget}.py` + pipeline wiring
- [ ] Tests proving: synthetic drift beyond tolerance → alert emitted + subsequent records `eligible_for_live=False`; operator ack (ledgered event) restores eligibility for *new* forecasts only; within-tolerance canary run changes nothing; per-forecast budget breach aborts that forecast fail-closed with a ledger event; per-day budget breach halts further research until UTC rollover; `max_pages` enforced in the research stage
- [ ] Cost report function: research cost per resolved forecast and per profitable trade emitted as ledgered metrics (consumed later by M6)
- [ ] Docstring / doc updates
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_synthetic_drift_marks_forecasts_live_ineligible(canary_env):
    run_canary_with(canary_env, responses=SHIFTED_DISTRIBUTION)   # beyond tolerance
    assert canary_env.alerts.contains("CANARY_DRIFT")
    record = run_pipeline(canary_env.market, canary_env.baseline)
    assert record.eligible_for_live is False

def test_ack_restores_eligibility_for_new_forecasts_only(canary_env):
    run_canary_with(canary_env, responses=SHIFTED_DISTRIBUTION)
    stale = run_pipeline(canary_env.market, canary_env.baseline)
    canary_env.operator_ack()
    fresh = run_pipeline(canary_env.market, canary_env.baseline)
    assert stale.eligible_for_live is False and fresh.eligible_for_live is True
```

## Constraints

**Scope fence:** Do not build dashboard UI (EPIC_01 stub / M5 wiring) or evaluation-gate metrics (EPIC_07 / M6 — this issue only *emits* cost metrics). Canary question-set *composition* is Open Question §20.7 — ship a fixture set and make the real set config-loadable. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — with canary green and budgets unexhausted, the pipeline behaves exactly as before this issue.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #4` and `Closes #28`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `forecast-engine`
