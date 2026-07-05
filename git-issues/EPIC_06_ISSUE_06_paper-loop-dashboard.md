## Role

You are a senior Python engineer with daemon/scheduler and dashboard experience, working across `hedgekit/` to wire the always-on PAPER loop end-to-end.

## Goal

`hedgekit run` operates continuously in PAPER mode — screen → forecast → select → Kernel approval → Gateway submission → PaperExchange fill → reconciliation → ledger, on schedule with heartbeats — and the dashboard renders open positions, the equity curve against the floor line, and selector decisions with veto reasons; a weekly report generation stub produces a dated placeholder document.

## Context

- **Parent epic:** #7
- **Predecessor issue(s):** #46 and #47 (both merged — the selector is feature-complete for PAPER). Cross-epic: EPIC_03 (Forecast Engine), EPIC_04 (Risk Kernel), EPIC_05 (Order Gateway + Reconciler) must all be merged; this issue only *wires*, it does not modify them.
- **SPEC section:** `plans/SPEC_v3.md` §18 M5 ("always-on RESEARCH→PAPER loop; dashboard positions/equity/floor; weekly reports; Done: continuous paper operation"), §5.3 (order flow — there is no other path), §14 (dashboard display list and mutation allowlist)
- **Files involved:**
  - `hedgekit/scheduler/loop.py` — the periodic pipeline tick for PAPER mode (extends the M0 RESEARCH idle loop)
  - `hedgekit/dashboard/views/` — positions, equity-vs-floor, selector-decisions views (extends the M0 stub dashboard)
  - `hedgekit/reports/weekly.py` — report stub emitting a dated markdown file with section headers and "no data yet" bodies (new)
  - `tests/integration/test_paper_loop.py` — record/replay end-to-end tick
- **Prior decisions:** the loop follows §5.3's single order path — the scheduler never calls the Gateway directly; only Kernel-approved tokens reach it. Dashboard binds `127.0.0.1`, and this issue adds *read* views only (mutations like pause/kill are Kernel/dashboard scope from earlier epics). Everything the loop does is ledgered.
- **State of the world:** all four processes exist and pass their own suites; the selector is complete; `hedgekit run` still idles in RESEARCH mode only.

## Output Format

Deliverable is a single PR containing:

- [ ] Scheduler tick for PAPER: pulls fresh snapshots (respecting freshness TTLs), invokes the pipeline, routes intents through Kernel→Gateway→PaperExchange, and ledgers every stage; dead-man's-switch heartbeat maintained
- [ ] Dashboard views rendering from ledger read models: open positions, equity curve vs. floor line, selector decisions incl. skip/veto reasons
- [ ] `hedgekit/reports/weekly.py` stub + scheduler hook
- [ ] Integration test: one full offline tick over recorded fixtures + LLM cassettes produces a ledgered forecast, a selector decision, and (when the fixture has edge) a paper fill — deterministic in CI
- [ ] No drive-by changes unrelated to the goal

## Examples

**Integration test that should pass after this issue lands:**

```python
def test_full_paper_tick_offline(recorded_market_bundle, llm_cassettes):
    ledger = run_single_tick(mode="PAPER", fixtures=recorded_market_bundle)
    assert ledger.has_event("MARKET_SNAPSHOT")
    assert ledger.has_event("FORECAST_CREATED")
    assert ledger.has_event("SELECTOR_DECISION")
    # The edge-bearing fixture must produce an approved, paper-filled order:
    assert ledger.order_terminal_state(fixture_intent_id) in {"FILLED", "RECONCILED"}
```

**Demoable outcome:** `hedgekit run` left running against PaperExchange fixtures shows a growing equity curve and decision log in the dashboard at `http://127.0.0.1:<port>`.

## Constraints

**Scope fence:** Do not modify Kernel checks, Gateway submission logic, or the PaperExchange fill model (EPIC_04/EPIC_05/EPIC_02 scope). Do not implement evaluation metrics, calibration, or real weekly-report content — that is EPIC_07 (M6). Dashboard mutations beyond what earlier epics shipped are out of scope.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — RESEARCH mode must still work unchanged when `mode_ceiling: research`, and PAPER activates only when config permits.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] RUNBOOK section updated: starting/observing the PAPER loop.
- [ ] PR body includes `Refs #7` and `Closes #48`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `selector`
