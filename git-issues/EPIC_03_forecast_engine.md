## Epic Summary

Build the Forecast Engine (SPEC_v3 §8, milestone M2): the research sandbox that turns screened markets into immutable, schema-valid `ForecastRecord`s (§6.3) with calibrated probabilities, verified citations, full cost accounting, and live-eligibility flags. The engine never sees balances, positions, limits, mode, or order books beyond the single baseline snapshot taken at forecast start — it is the untrusted-input side of the research/execution firewall (§1.1-5).

## Scope

**In scope:**
- Pipeline stages of §8.2 (question normalization → resolution-criteria extraction → outside-view pass → decomposition → bounded web research → source-reliability pass → adversarial counterargument → independent model votes → median aggregation → coherence normalization → calibration map → shrinkage → schema-validated `ForecastRecord`).
- LLM cassette record/replay so the full pipeline runs offline and deterministically in CI (§17.1).
- Two-stage triage cost defense (§8.4, T11).
- Structural research tool boundary and sandbox isolation (§8.3).
- Pinned multi-model ensemble, response fingerprints, vote dispersion, coherence normalization across mutually exclusive groups (§8.6, §8.7, T2/T14/T17).
- Citation verification and first-class scored abstention (§8.8).
- Prompt-injection defense and the adversarial poisoned-page CI corpus (§8.5, T1).
- Weekly canary set with drift alerting; per-forecast/per-day research budgets (§8.6, §16 `forecast:` block).

**Out of scope:**
- Trade selection, sizing, and order intents — EPIC_05 (M5 Selector).
- Risk Kernel checks and live eligibility *enforcement* at approval time — EPIC_04 (M3).
- Calibration-map *fitting* from resolved outcomes and gate metrics — EPIC_07 (M6 Evaluation); this epic only applies a versioned map.
- Market screening and order-book snapshots — EPIC_02 (M1 Connector).

## Success Criteria

The epic is done when:

- [ ] ≥ 50 auditable research-only forecasts can be produced end-to-end from recorded fixtures within budget (M2 done-gate, §18).
- [ ] The full pipeline runs offline in CI via cassettes, byte-deterministically.
- [ ] The adversarial injection corpus produces zero effect on anything but probability/rationale fields and zero tool calls outside the allowlist (§8.5 release blocker).
- [ ] Attempted mutation of a persisted `ForecastRecord` raises (§8.9).
- [ ] Canary-drift alerting demonstrated with synthetic drift (§8.9).
- [ ] All child issues are closed.
- [ ] Smoke tests for the full epic surface pass on `main`.

## Child Issues

- [ ] #22 — feat(forecast): Wire pipeline stages as stubs with LLM cassette harness
- [ ] #23 — feat(forecast): Two-stage triage with cost ledgering
- [ ] #24 — feat(forecast): Research sandbox with structural tool boundary
- [ ] #25 — feat(forecast): Pinned ensemble, median aggregation, and coherence normalization
- [ ] #26 — feat(forecast): Citation verification and scored abstention
- [ ] #27 — feat(forecast): Prompt-injection defense with poisoned-page CI corpus
- [ ] #28 — feat(forecast): Weekly canary set and research budget enforcement

## Sequencing Notes

- **Blocked by:** EPIC_01 (M0 Foundations — fixed-point types incl. `ProbabilityPpm`/`MoneyMicros`, hash-chained ledger, typed config) and EPIC_02 (M1 Connector — `NormalizedMarket`, quote snapshots, screen decisions).
- **Parallel-safe:** EPIC_04 (M3 Risk Kernel) per §18 dependency graph `{M2 ∥ M3}` — the two share only ledger event schemas from EPIC_01.
- **Unblocks:** EPIC_05 (M5 Selector) which consumes `ForecastRecord`s, and EPIC_07 (M6 Evaluation) which scores them.

## SPEC Reference

`plans/SPEC_v3.md` — §18 M2 (milestone definition); §8 (Forecast Engine, entire); §6.3 (`ForecastRecord`); §4 threats T1, T2, T11, T14, T17; §16 `forecast:` config block.

## Labels

`epic`, `spec-decomposition`, `forecast-engine`
