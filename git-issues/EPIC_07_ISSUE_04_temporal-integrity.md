## Role

You are a senior Python engineer with an adversarial-QA mindset, working in this repo's `windbreak/evaluation/` package (mypy --strict). Your job is to make training-data leakage into gate metrics structurally impossible.

## Goal

The evaluation package rejects, at ingestion, any forecast record whose `created_at` postdates its question's resolution or predates system deployment, and no unresolved market can ever enter a headline metric — both enforced in code and proven by tests (SPEC §1.1 invariant 6, §8.6, §13.6).

## Context

- **Parent epic:** #8
- **Predecessor issue(s):** #50 (must be merged first — needs real resolution timestamps). Parallel-safe with #51.
- **SPEC section:** `plans/SPEC_v3.md` §1.1-6 (temporal integrity invariant), §8.6 ("the evaluation package must reject any record whose `created_at` postdates question resolution or predates system deployment"), §13.6 ("Unresolved markets can never enter a headline metric — enforced in code, tested"), §4 T14.
- **Files involved:**
  - `windbreak/evaluation/temporal.py` — new: the temporal-integrity gate, a single choke point every metric-input query passes through
  - `windbreak/evaluation/registry.py` — route all metric input through the choke point; no metric can opt out
  - `tests/evaluation/test_temporal_integrity.py`
  - `tests/evaluation/fixtures/` — leakage fixtures: backdated forecast, pre-deployment forecast, unresolved-market forecast
- **Prior decisions:** rejection is ledgered, not silent (§8.5 pattern: invalid input is discarded *and recorded*); deployment timestamp comes from the ledger's first mode-transition event, not config (config can be edited; the ledger is append-only, §12).
- **State of the world:** resolution tracker and baselines are real; metrics may or may not have landed (issue 3 is parallel); the registry currently feeds metrics unfiltered records.

## Output Format

Deliverable is a single PR containing:

- [ ] `temporal.py` choke-point filter with typed rejection reasons (`BACKDATED`, `PRE_DEPLOYMENT`, `UNRESOLVED`)
- [ ] Registry wiring such that *every* headline metric's input passes the filter — demonstrated by a test that registers a fake metric and proves it cannot receive a leaked record
- [ ] Ledgered rejection events for every excluded record
- [ ] Tests covering all three leakage classes plus the happy path
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_backdated_forecast_never_reaches_metrics() -> None:
    records = [valid_record(), backdated_record()]  # created_at > resolution time
    accepted, rejected = temporal_gate(records, deployment_ts=DEPLOY_TS)
    assert len(accepted) == 1
    assert rejected[0].reason is RejectionReason.BACKDATED
    # and the rejection is ledgered:
    assert ledger.last_event().event_type == "EVALUATION_RECORD_REJECTED"
```

## Constraints

**Scope fence:** Do not implement observation windows or selection-bias reporting (#53), and do not touch the Forecast Engine's own eligibility flags (EPIC_03 owns `eligible_for_live`). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

**No bypass parameter:** the filter must not accept any "skip validation" flag. There is deliberately no API to feed a backtest into gate metrics.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #8` and `Closes #52`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `evaluation`
