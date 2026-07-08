## Epic Summary

Harden windbreak for real-money LIVE_MICRO operation and ship the operator-facing documentation set. This epic covers SPEC §18 M7 ("Production read-only validation; trade-key scope validation; jurisdiction preflight; micro-cap deployment; restore/kill/reconciliation drills on production APIs; live-vs-paper slippage comparison; profit-sweep + ratchet in anger") plus the §19 documentation requirements. It is the final v1 epic: when it closes, the 60-day LIVE_MICRO→LIVE criteria (§10.9) are *computable safely* — whether they are ever met depends on the data, not the code.

## Scope

**In scope:**
- `windbreak preflight`: production read-only validation, trade-key scope validation (withdrawal capability → hard startup failure), jurisdiction preflight, secrets hygiene checks (world-readable secrets, unverifiable scope, unset LLM budgets) per §15 and §1.1-3.
- LIVE_MICRO deployment path: `micro_cap_micros` hard cap enforcement, human-ack flow (§10.8) exercised against real orders, outbound network allowlist enforcement (§15).
- Live-vs-paper slippage comparison and live Brier degradation-band monitoring feeding the §10.9 gate inputs and §10.10 demotion triggers.
- Scripted, repeatable operational drills on production APIs: restore-from-backup, kill/re-arm, reconciliation-mismatch response, key rotation; profit-sweep advisory and floor ratchet (§10.7) exercised in anger.
- Full §19 documentation set: `SECURITY.md`, `RUNBOOK.md`, `ARCHITECTURE.md`, `ACCOUNTING.md`, `EVALUATION.md`, `LEGAL_AND_COMPLIANCE.md`, `OPERATOR_WARNINGS.md`, README updates with the mandated plain statements.

**Out of scope:**
- Any M8 / post-v1 item (§18 M8, §19 list of post-v1 epics): Polymarket adapter, local-model ensemble member, strategy-driven early exits, dutch-book detector, child-order slicing, formal verification, tax-export improvements.
- New gate logic or metric definitions — those land in EPIC_07 (M6); this epic only *feeds and consumes* them in a live setting.
- Automatic withdrawals or transfers (forbidden system-wide, §1.2, §10.7).

## Success Criteria

The epic is done when:

- [ ] `windbreak preflight` runs against production APIs read-only and fails closed on any scope, jurisdiction, or secrets violation — with a fixture-tested check matrix.
- [ ] A LIVE_MICRO session on production APIs cannot deploy more than `micro_cap_micros` regardless of any other config, proven by test and by drill.
- [ ] Live-vs-paper slippage and live Brier degradation are computed continuously, ledgered, surfaced on the dashboard, and wired into §10.9 gate evaluation and §10.10 demotion triggers.
- [ ] Every RUNBOOK procedure has been executed at least once as a scripted drill against production APIs (restore, kill/re-arm, reconciliation mismatch, key rotation) with ledgered evidence.
- [ ] All eight §19 documents exist, are accurate against the shipped code, and the README carries every §19-mandated plain statement.
- [ ] All child issues are closed
- [ ] Smoke tests for the full epic surface pass on `main`

## Child Issues

- [ ] #56 — feat(livemicro): `windbreak preflight` production-readiness checklist
- [ ] #57 — feat(livemicro): LIVE_MICRO micro-cap deployment with human-ack and network allowlist
- [ ] #58 — feat(livemicro): Live-vs-paper slippage and Brier degradation monitoring
- [ ] #59 — feat(livemicro): Scripted operational drills
- [ ] #60 — docs(livemicro): Full SPEC section-19 documentation set

## Sequencing Notes

- **Blocks:** nothing — final v1 epic.
- **Blocked by:** EPIC_07 (M6 Evaluation & Calibration) — §18 dependency order is M0 → M1 → {M2 ∥ M3} → M4 → M5 → M6 → M7, and M6 gates any live promotion. Also transitively requires EPIC_04 (Risk Kernel) and EPIC_05 (Order Gateway), since M3 blocks any code that can reach a real exchange.
- **Parallel-safe:** the documentation issue (Polish) can start as soon as the components it documents are merged; it does not need the drills to finish.

## SPEC Reference

`plans/SPEC_v3.md` — §18 M7 (milestone definition), §15 (Security), §19 (Documentation Requirements), §10.7–§10.10 (floor governance, human-ack, promotion gates, demotion triggers), §2 (residual risks for OPERATOR_WARNINGS.md), §1.1-3 (credential invariant).

## Labels

`epic`, `spec-decomposition`, `live-micro`
