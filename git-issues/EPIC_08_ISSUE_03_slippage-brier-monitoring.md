## Role

You are a senior Python engineer with market-microstructure and forecast-evaluation experience, working in this repo's `hedgekit/evaluation/` and `hedgekit/riskkernel/` packages (Python ≥3.11, mypy --strict).

## Goal

Live LIVE_MICRO fills are continuously compared against the §17.4 paper-fill model (slippage) and the rolling live Brier score against its degradation band, with both series ledgered, rendered on the dashboard, consumed by the §10.9 LIVE_MICRO→LIVE gate computation, and wired into the §10.10 automatic demotion triggers.

## Context

- **Parent epic:** #EPIC_08_NUMBER
- **Predecessor issue(s):** #EPIC_08_ISSUE_02_NUMBER (must be merged first — there are no live fills to measure without the micro-cap deployment path).
- **SPEC section:** `plans/SPEC_v3.md` §10.9 ("live slippage ≤ configured multiple of paper model; live Brier within degradation band"), §10.10 ("rolling Brier degradation; live-vs-paper slippage divergence" as automatic demotion triggers), §13.1 (execution-quality track), §13.5 (slippage by market/category), §17.4 (the normative paper-fill model being compared against).
- **Files involved:**
  - `hedgekit/evaluation/execution_quality.py` — per-fill modeled-vs-actual comparison (create or extend from EPIC_07).
  - `hedgekit/evaluation/` gate computation — add the two §10.9 live inputs to the pre-registered gate evaluation.
  - `hedgekit/riskkernel/` — demotion trigger consumption (§10.10): divergence beyond config → demote one mode, ledgered.
  - `hedgekit/dashboard/` — live-vs-paper slippage and rolling-Brier panels (§14 display list).
  - `tests/evaluation/`, `tests/riskkernel/` — synthetic known-answer fixtures.
- **Prior decisions:** three evaluation tracks are never merged into one number (§13.1); gate definitions are pre-registered and hash-committed — if this issue adds gate inputs, the gate plan re-registers and the clock resets per §13.6 (this is expected and must be ledgered, not worked around). Dual-path (SQL + Python) computation applies to any gate-feeding metric (§13.6, T12).
- **State of the world:** EPIC_07 shipped the three-track evaluation, clustered bootstrap, and pre-registration flow against paper data. Live fills now exist (issue 02) but nothing compares them to the paper model; demotion triggers for slippage/Brier divergence are defined in config but not fed.

## Output Format

Deliverable is a single PR containing:

- [ ] Per-fill execution-quality records: for every live fill, the §17.4 model's predicted fill price/fees on the same recorded book vs. actual — difference ledgered in integer pips/micros (§6.1).
- [ ] Rolling live Brier on resolved live-mode forecasts with the configured degradation band vs. the PAPER baseline; window and band read from pre-registered gate config.
- [ ] Kernel wiring: breach of either divergence threshold → automatic demotion one mode + alert, ledgered with the triggering series snapshot (§10.10).
- [ ] Gate inputs: `live_slippage_ratio` and `live_brier_degradation` computed dual-path (SQL + Python) and validated on synthetic known-answer datasets (T12).
- [ ] Dashboard panels for both series with the thresholds drawn.
- [ ] No drive-by changes unrelated to the goal.

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_slippage_divergence_demotes_to_paper(synthetic_fills):
    # Paper model predicts 4520 pips avg; live fills at 4610 → ratio breaches 1.5x config
    kernel, evaluation = wire(synthetic_fills, slippage_multiple_limit_ppm=1_500_000)
    evaluation.run_cycle()
    assert kernel.mode is Mode.PAPER
    assert ledger.last_event().event_type == "MODE_DEMOTED"
    assert "slippage_divergence" in ledger.last_event().payload["reason"]
```

**Example: known-answer dataset shape (dual-path validation)**

```
fixtures/execution_quality/known_answer_01.json
  fills: 12 synthetic fills with hand-computed modeled vs actual costs
  expected.live_slippage_ratio_ppm: 1_237_000   # both SQL and Python must produce exactly this
```

## Constraints

**Scope fence:** Do not modify the §17.4 paper-fill model itself — any change to it re-registers the gate plan and belongs to a deliberate EPIC_07 follow-up. Do not implement operator drills (#EPIC_08_ISSUE_04_NUMBER) or docs (#EPIC_08_ISSUE_05_NUMBER). No new baseline metrics beyond the two §10.9 inputs.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — evaluation cycles on a PAPER-only ledger (no live fills) must produce empty-but-valid series, not errors.

**Measurement integrity:** unresolved markets never enter a headline metric (§13.6); temporal-integrity rejection (§8.6) applies to the live Brier series exactly as in paper.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #EPIC_08_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `live-micro`
