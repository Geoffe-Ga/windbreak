## Role

You are a senior Python engineer with distributed-systems reliability experience (failure taxonomies, bounded retries, fail-closed design) working in this repo's `hedgekit/forecast/` package.

## Goal

Any provider failure — timeout, rate limit, HTTP error, malformed body, version drift, cost overrun — degrades deterministically to a ledgered discard and, when the surviving ensemble falls below quorum, a scored abstention; never to a stub value, a silent retry storm, or an unledgered exception.

## Context

- **Parent epic:** #183
- **Predecessor issue(s):** #189, #191, #192 (all merged first — this issue hardens their live paths).
- **SPEC section:** `plans/SPEC_v3.md` §8.8 (abstention is first-class and scored), §8.4/§16 (budgets), §4 T11 (cost/DoS defense), §8.5 (discard-and-ledger, never repair).
- **Files involved:**
  - `hedgekit/forecast/providers/base.py` — complete the `ProviderError` taxonomy: `ProviderTimeoutError`, `ProviderRateLimitedError`, `ProviderHTTPError`, `ProviderMalformedResponseError`, `ProviderVersionDriftError`, `ProviderCostOverrunError` (modify)
  - `hedgekit/forecast/providers/retry.py` — bounded retry policy: max attempts + total-deadline from config; every attempt's cost charged; rate-limit honors Retry-After up to the deadline (new)
  - `hedgekit/forecast/pipeline.py` — quorum rule: fewer than `min_ensemble_votes` (config, default 2) surviving votes → abstain with new reason `ABSTENTION_ENSEMBLE_QUORUM_NOT_MET`; provider-down abstention reason `ABSTENTION_PROVIDER_UNAVAILABLE`; both registered in `_ABSTENTION_RATIONALE_BY_REASON` with truthful rationales (modify)
  - `hedgekit/forecast/budget.py` — real per-provider price table (config-sourced, micros) replacing the flat `_RESEARCH_COST_MICROS` stub for live paths; unknown price → configured ceiling, never zero (modify)
  - `tests/forecast/test_provider_failures.py` — fault-injection suite (new)
- **Prior decisions:** `_build_abstention_record` raises on an unregistered abstention reason — register the new reasons with rationale text that states exactly what happened; discard events reuse `FORECAST_OUTPUT_DISCARDED_EVENT` payload shape (fingerprint-only, never raw bodies); charging is fail-closed (`PerForecastBudgetExceededError` aborts, day exhaustion halts before research).
- **State of the world:** live adapters exist but treat failures ad hoc; research cost is a flat 3_000_000-micro constant; there is no quorum concept — today a single surviving vote aggregates as if it were an ensemble.

## Output Format

Deliverable is a single PR containing:

- [ ] The full typed error taxonomy, mapped from each adapter's raw failures
- [ ] Bounded retry with per-attempt cost charging and a hard total deadline; zero retries for malformed responses (discard immediately — repair is forbidden)
- [ ] Quorum-gated aggregation + two new registered abstention reasons with truthful rationale text
- [ ] Per-provider price table wired into `ResearchBudget` for every live call path (votes, research forecaster, triage, search/fetch)
- [ ] Fault-injection tests covering every error class × every provider family, asserting: correct ledger event, correct abstention reason, no unbudgeted spend, no stub fallback values
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: quorum abstention is honest**
```python
def test_single_surviving_vote_abstains_not_aggregates(pipeline_env):
    record = run_with_failures(pipeline_env, failing=["anthropic", "futuresearch"])
    assert record.abstention_reason == "ensemble_quorum_not_met"
    assert record.eligible_for_live is False
    assert "2 of 3 members failed" in record.rationale_markdown
```

**Example: retries never exceed budget**
```python
def test_rate_limited_retries_charge_every_attempt(pipeline_env):
    run_with_rate_limits(pipeline_env, attempts=3)
    assert pipeline_env.budget.charged_micros == 3 * PER_ATTEMPT_COST_MICROS
```

## Constraints

**Scope fence:** No provider-selection/track-record logic (#194) and no alerting/dashboards (#195). Do not weaken any existing budget or injection behavior. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The happy-path pipeline (all providers healthy, replayed) must produce byte-identical records before and after this PR — hardening adds branches, it never changes the clean path.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`), including the injection corpus.
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%, including every error branch; `mypy --strict` passes.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #183` and `Closes #193`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `forecast-engine`
