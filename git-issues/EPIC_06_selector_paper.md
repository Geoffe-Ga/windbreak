## Epic Summary

Deliver the Trade Selector — a pure, credentialless, deterministic function from `(ForecastRecord, calibration map version, fresh order book, fee model, slippage model, position read model, risk-config snapshot, correlation tags)` to zero or more `NormalizedOrderIntent`s — and wire the always-on RESEARCH→PAPER loop so hedgekit runs continuously in paper mode. Covers SPEC_v3 §9 (Trade Selector, entire), §18 M5, threat mitigations T2 (disagreement-scaled sizing), T10 (correlation buckets), and T13 (adverse-selection controls), plus the §14 dashboard views for positions, equity-vs-floor, and selector decisions.

## Scope

**In scope:**
- Pure selector function with byte-identical determinism on identical inputs (§9.1).
- Fee-aware executable edge computed by walking the actual book — gross, fee-adjusted, slippage-adjusted, research-cost-adjusted edges and idle-cash-adjusted annualized hurdle (§9.2–§9.3).
- Sizing: fractional Kelly on above-floor capital, dispersion-scaled (§9.5–§9.6), clipped by every cap including book participation.
- Price bands (§9.4), execution-style policy with resting-order TTL / cancel-on-move (§9.7), hold-to-resolution exits (§9.8).
- Correlation-bucket tagging and per-bucket caps enforced selector-side (§9.9).
- Always-on RESEARCH→PAPER loop through Risk Kernel + Order Gateway + PaperExchange; dashboard positions/equity/floor/selector-decision views; weekly report stub (§18 M5).

**Out of scope:**
- Strategy-driven early exits — post-v1 (§9.8, §19).
- Kernel-side enforcement of the same caps — EPIC_04's scope (defense in depth means both exist independently).
- Evaluation metrics, calibration, promotion-gate computation — EPIC_07 (M6).
- Live trading of any kind; PAPER is the ceiling for this epic.

## Success Criteria

The epic is done when:

- [ ] Golden tests reproduce byte-identical intents from recorded books across two runs and two machines (§9.10).
- [ ] Property tests prove: sizing monotone in edge, zero below threshold, never exceeds any cap or participation limit, never negative-EV-after-fees, never opens outside price bands, dispersion scaling monotone (§9.10).
- [ ] No live intent is ever produced for ineligible markets or ineligible forecasts (§9.10).
- [ ] `hedgekit run` operates continuously in PAPER mode: screens, forecasts, selects, submits through Kernel→Gateway→PaperExchange, and ledgers every decision.
- [ ] Dashboard shows open positions, equity curve vs. floor line, and selector decisions with veto reasons (§14).
- [ ] All child issues are closed.
- [ ] Smoke tests for the full epic surface pass on `main`.

## Child Issues

- [ ] #43 — feat(selector): Pure selector skeleton with golden determinism harness
- [ ] #44 — feat(selector): Fee-aware executable edge and entry conditions
- [ ] #45 — feat(selector): Dispersion-scaled fractional Kelly sizing with cap clipping
- [ ] #46 — feat(selector): Price bands, execution style, and adverse-selection controls
- [ ] #47 — feat(selector): Correlation buckets with per-bucket caps
- [ ] #48 — feat(selector): Always-on PAPER loop with dashboard views

## Sequencing Notes

- **Blocked by:** EPIC_03 (M2 Forecast Engine — supplies `ForecastRecord`s), EPIC_04 (M3 Risk Kernel — approves intents), EPIC_05 (M4 Order Gateway — submits and reconciles). All three must land before Issue 06 (the loop) can run end-to-end; Issues 01–05 need only EPIC_02's recorded books/fee models plus the domain types from EPIC_01, so they may start as soon as those exist.
- **Blocks:** EPIC_07 (M6 Evaluation & Calibration) — evaluation needs a continuously running PAPER loop producing selector decisions and paper fills.
- **Parallel-safe:** none of this epic's files overlap the Kernel or Gateway packages; selector work can proceed alongside late EPIC_04/EPIC_05 issues.

## SPEC Reference

`plans/SPEC_v3.md` — §9 (Trade Selector, all subsections), §18 M5, §4 rows T2/T10/T13, §14 (dashboard views), §16 `risk:` config block.

## Labels

`epic`, `spec-decomposition`, `selector`
