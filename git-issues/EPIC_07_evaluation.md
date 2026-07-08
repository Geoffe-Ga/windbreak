## Epic Summary

Build the Evaluation & Calibration subsystem (SPEC_v3 §13, milestone M6 in §18): the three-track measurement machinery (forecast quality / selection quality / execution quality) that decides — from data, not narrative — whether windbreak's forecasts have any edge. This epic makes the PAPER→LIVE_MICRO promotion gate *measurable*; whether it is ever *met* depends on the data, not the code. It also implements the anti-Goodhart pre-registration flow (T15), temporal-integrity enforcement (T14 / §1.1-6), and dual-path gate computation (T12).

## Scope

**In scope:**
- Resolution tracker, including `SETTLEMENT_REVERSED` handling (T16).
- Baselines (§13.2): executable price at baseline snapshot (primary); midpoint, uniform 0.5, base-rate, previous-forecast (secondary).
- Statistical machinery (§13.5): Brier, log score, Brier skill score, ECE, calibration slope/intercept, reliability diagrams, sharpness, clustered bootstrap CIs, per-price-bucket calibration and PnL, power analysis at N=300.
- Temporal-integrity enforcement: only real-time, post-deployment forecasts on then-unresolved questions enter gate metrics.
- Selection-bias controls and abstention counterfactual scoring (§13.3); precommitted observation windows (§13.4).
- Pre-registration of gate definitions: canonical serialization, hashing, ledgering at PAPER entry; changes reset the PAPER clock (§13.6).
- Dual-path (SQL + Python) gate computation validated on synthetic known-answer datasets; weekly report generation; cost-adjusted expectancy reporting.

**Out of scope:**
- Promotion/demotion *decisions* — the Risk Kernel (EPIC_04 / M3) owns mode transitions; this epic only computes the gate inputs.
- Dashboard rendering of calibration plots — the dashboard epic consumes this epic's report artifacts.
- Strategy changes based on evaluation output (post-v1).

## Success Criteria

The epic is done when:

- [ ] All §13 metrics match hand-computed values on synthetic known-answer datasets (§13.7).
- [ ] SQL and Python gate paths agree on every synthetic dataset (T12).
- [ ] Clustered bootstrap validated on fixtures with known correlation structure.
- [ ] Temporal-integrity rejection is demonstrated by test: a record whose `created_at` postdates resolution, or predates deployment, never enters a headline metric.
- [ ] Pre-registration hash flow works end-to-end: gate plan serialized, hashed, ledgered; any change resets the PAPER clock.
- [ ] Weekly report generates, and the "no edge" state renders bluntly (§13.2).
- [ ] All child issues are closed.
- [ ] Smoke tests for the full epic surface pass on `main`.

## Child Issues

- [ ] #49 — feat(evaluation): Three-track report skeleton over synthetic fixtures
- [ ] #50 — feat(evaluation): Resolution tracker with reversals and baselines
- [ ] #51 — feat(evaluation): Statistical machinery with clustered bootstrap
- [ ] #52 — feat(evaluation): Temporal-integrity enforcement
- [ ] #53 — feat(evaluation): Selection-bias cohorts and abstention scoring
- [ ] #54 — feat(evaluation): Pre-registered gate plans with PAPER-clock reset
- [ ] #55 — feat(evaluation): Dual-path gate computation and weekly reports

## Sequencing Notes

- **Depends on:** EPIC_06 (M5 Selector + PAPER mode) — evaluation consumes ledgered forecasts, selector decisions, and paper fills; the always-on paper loop must be producing data. Also transitively on the ledger read models (M0) and ForecastRecord schema (M2).
- **Blocks:** EPIC_08 (M7 Live-micro hardening) and any PAPER→LIVE_MICRO promotion — per SPEC §18, "M6 gates any live promotion."
- **Parallel-safe:** none of the later milestones; within the epic, issues 4–6 can proceed in parallel after issue 2 lands.

## SPEC Reference

`plans/SPEC_v3.md` — §13 (Evaluation & Calibration, entire), §18 milestone M6, §4 threats T12/T14/T15/T16, §1.1 invariant 6 (temporal integrity), §10.9 (promotion gates consuming these metrics).

## Labels

`epic`, `spec-decomposition`, `evaluation`
