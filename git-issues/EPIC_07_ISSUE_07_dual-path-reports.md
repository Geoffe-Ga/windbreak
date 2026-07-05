## Role

You are a senior Python engineer comfortable with both SQL and Python data paths, working in this repo's `hedgekit/evaluation/` package (SQLite WAL ledger per SPEC §12, mypy --strict).

## Goal

Every promotion-gate metric is computed twice — once in SQL against the ledger read models, once in Python — with disagreement beyond integer-rounding tolerance failing loudly (SPEC T12); the weekly report generates end-to-end, including research cost per resolved forecast, cost per profitable trade, and cost-adjusted expectancy (§13.5).

## Context

- **Parent epic:** #EPIC_07_NUMBER
- **Predecessor issue(s):** #EPIC_07_ISSUE_06_NUMBER (must be merged first — gate computations read the registered plan).
- **SPEC section:** `plans/SPEC_v3.md` §13.6 ("Gate computations are dual-pathed (SQL + Python) and validated against synthetic known-answer datasets"), §4 T12 (silent gate-metric failure), §13.5 (cost metrics), §13.7 (acceptance criteria), §2 ("Why the LLM cost model is part of the strategy").
- **Files involved:**
  - `hedgekit/evaluation/sql_gates.py` — new: SQL implementations of each gate metric over ledger read models
  - `hedgekit/evaluation/crosscheck.py` — new: dual-path comparator with integer tolerance; mismatch → ledgered `GATE_COMPUTATION_MISMATCH` + alert hook
  - `hedgekit/evaluation/costs.py` — new: research-cost aggregation from `ForecastRecord.research_cost_micros` (both triage and full stages)
  - `hedgekit/evaluation/report.py` — weekly report assembly; cost meter section
  - `tests/evaluation/test_dual_path.py`, `tests/evaluation/test_costs.py`, `tests/evaluation/test_weekly_report.py`
- **Prior decisions:** the Python path is the reference implementation from #EPIC_07_ISSUE_03_NUMBER; SQL must reproduce it, not vice versa. A mismatch is a halt-worthy anomaly for the Kernel (§10.10 "silent gate-metric failure" class), surfaced via the alert-sink abstraction from M0.
- **State of the world:** all metrics, windows, cohorts, temporal gate, and pre-registration are real; only the Python path exists; report has no cost section.

## Output Format

Deliverable is a single PR containing:

- [ ] SQL path for every gate metric named in the registered `GatePlan`
- [ ] Cross-check harness running both paths on every gate evaluation; mismatch ledgered + alerted, never silently averaged
- [ ] Cost metrics: research cost per resolved forecast, per profitable trade, cost-adjusted expectancy — integer `MoneyMicros` end-to-end
- [ ] Weekly report generation wired to a scheduler hook, tested via direct invocation
- [ ] Tests: dual paths agree on all synthetic fixtures; a deliberately corrupted SQL query (test fixture) triggers the mismatch alarm; cost metrics match hand-computed values
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_dual_path_mismatch_is_loud() -> None:
    result = crosscheck_gates(fixture_db(), plan=REGISTERED_PLAN,
                              sql_path=corrupted_sql_for_test())
    assert result.status is CrosscheckStatus.MISMATCH
    assert ledger.last_event().event_type == "GATE_COMPUTATION_MISMATCH"
    assert alert_sink.last_alert().severity is Severity.CRITICAL
```

## Constraints

**Scope fence:** Do not implement dashboard rendering (dashboard epic) or Kernel halt behavior (EPIC_04 consumes the mismatch event; you only emit it). If you find yourself touching files outside the list above, stop and check with the user.

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

`spec-decomposition`, `polish`, `evaluation`
