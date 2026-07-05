## Epic Summary

Build the **Risk Kernel** (Process B): the independent, veto-holding process that owns mode state, floor enforcement, capital reservations, approval-token signing, promotion/demotion, halt logic, kill switch, and read-only exchange verification (SPEC `plans/SPEC_v3.md` §10 entire, §18 M3). The Kernel is written as if every other component is malicious or insane (§3.6): no order can reach an exchange except via a Kernel-signed, single-use approval token.

> **⛔ M3 BLOCKS ANY CODE THAT COULD REACH A REAL EXCHANGE.** Per SPEC §18: "M3 blocks any code that could reach a real exchange." No Order Gateway work (EPIC_05) and no real-exchange submission path may merge until this epic's import-boundary and invariant tests are green on `main`.

## Scope

**In scope:**
- Separate `riskkernel` process that survives main-pipeline crashes (§5.1, §10.12)
- Mode state machine `RESEARCH → PAPER → LIVE_MICRO → LIVE` plus `PAUSED | HALT | KILLED` (§10.2)
- Per-order check pipeline, fail-closed on any check *error* (§10.3)
- Floor invariant: conservative `worst_case_equity` computation and pre-trade enforcement (§10.4, §17.3)
- Serialized (single-writer) reservation ledger (§10.5) — what makes T4 impossible rather than unlikely
- HMAC/signature approval tokens: single-use, intent-bound, TTL-bound (§10.6)
- Independent read-only exchange verification with HALT on mismatch (§10.1, §10.4)
- Promotion gates as pre-registered data, automatic demotion/halt triggers, `mode_ceiling` (§10.9, §10.10, T15)
- Floor governance: raise-freely/lower-slowly, 48h cool-off, ratchet, profit-sweep advisory, human-ack thresholds (§10.7, §10.8, T7)
- Kill switch: dashboard/CLI/KILL-file/automatic triggers, manual re-arm (§10.11)
- Property tests over concurrent intent streams with injected crashes; mutation score ≥90% on kernel, accounting, and token packages (§10.12, §17.2, §17.6)

**Out of scope:**
- Order submission, exchange trade credentials, order lifecycle — EPIC_05 (M4 Order Gateway)
- Selector logic producing intents — EPIC_06 (M5)
- Gate *metric computation* (Brier, clustered bootstrap) — EPIC_07 (M6); this epic consumes gate results as data
- Dashboard UI for kill/ack buttons — stub CLI/file interfaces only

## Success Criteria

The epic is done when:

- [ ] The Kernel runs as its own process; killing Process A does not kill it, and vice versa (§5.1, §10.12)
- [ ] An import-boundary CI test proves only `riskkernel` imports the signing-key handle and no order path can bypass the Kernel (§5.3, §18 M3)
- [ ] Property tests over random concurrent intent streams with injected crashes at every reserve/approve/ack edge prove the floor invariant unbreakable (§10.12)
- [ ] The full token-forgery matrix (replay, mutation, expiry, partial-field forgery, bit-flip) fails verification (§10.6, §10.12)
- [ ] The full mode-transition matrix is tested; floor cool-off, ratchet, and kill drills pass (§10.12)
- [ ] Mutation-testing score ≥90% on kernel, accounting, and token packages (§17.6)
- [ ] All child issues are closed
- [ ] Smoke tests for the full epic surface pass on `main`

## Child Issues

- [ ] #29 — feat(riskkernel): Kernel process skeleton with mode machine and veto-everything pipeline
- [ ] #30 — feat(riskkernel): Floor invariant and fail-closed per-order checks
- [ ] #31 — feat(riskkernel): Serialized reservations and signed single-use approval tokens
- [ ] #32 — feat(riskkernel): Read-only exchange verification with HALT on mismatch
- [ ] #33 — feat(riskkernel): Promotion gates, demotion triggers, and mode-ceiling enforcement
- [ ] #34 — feat(riskkernel): Floor governance — cool-off lowering, ratchet, profit-sweep, human-ack
- [ ] #35 — feat(riskkernel): Kill switch with hold-positions and manual re-arm
- [ ] #36 — test(riskkernel): Concurrent crash-injection properties and >=90% mutation score

## Sequencing Notes

- **Depends on:** EPIC_01 (M0 foundations: ledger, fixed-point types, config loader, alert sinks). Issue 4 additionally consumes EPIC_02 (M1 connector) read-only fixtures and the `BalanceSemantics` record.
- **Parallel-safe:** EPIC_03 (M2 Forecast Engine) — SPEC §18 dependency order is `M0 → M1 → {M2 ∥ M3} → M4`.
- **Blocks:** EPIC_05 (M4 Order Gateway) and *any* code that could reach a real exchange. Also blocks live promotion work in EPIC_08 (M7).

## SPEC Reference

`plans/SPEC_v3.md` — §10 (Risk Kernel, entire), §18 M3, §17.2 (Risk Kernel test matrix), §17.3 (accounting proofs), §17.6 (coverage & mutation floors), threat rows T3, T4, T7, T15, T16 (§4).

## Labels

`epic`, `spec-decomposition`, `risk-kernel`
