## Epic Summary

Build the Order Gateway: the single credential-isolated process (Process C) that can submit live orders, per SPEC §11 ("The only component that can submit live orders"). It verifies Risk Kernel approval tokens, owns trade-scope credentials, submits/cancels exchange orders through the §11.3 state machine, runs the adverse-selection sweeper, and reconciles ledger state against the exchange continuously and after crashes (§11.4). This epic closes threats T3 (runaway order loop), T9 (crash mid-order), and the gateway half of T13 (adverse selection) from SPEC §4.

## Scope

**In scope:**
- Order Gateway as a separate process holding trade-scope credentials and the approval-token verification key (§5.2)
- Token verification: signature, intent-hash match, expiry, single-use (§11.2)
- Order state machine §11.3 with ledger-before-next-action on every transition
- Limit orders only; deterministic client order IDs (hash of intent) for idempotent resubmission
- Reduce-only enforcement for `SELL_TO_CLOSE` (exchange flag if available, local validation regardless, post-fill re-verification per §6.4)
- Crash recovery: write-ahead intent log, startup reconciliation, halt on unexplained mismatch (§11.4)
- Continuous Reconciler (default 60s) auto-healing known-benign cases only
- Stale-order sweeper: resting TTL expiry and `cancel_on_move_ticks` cancellation (§9.7 gateway side)
- Chaos suite per §11.5 acceptance criteria

**Out of scope:**
- Risk Kernel token *signing*, reservations, floor checks — EPIC_04 (M3)
- Kalshi adapter and PaperExchange internals — EPIC_02 (M1)
- Trade Selector intent generation — EPIC_06 (M5)
- Production trade-key scope validation and live drills — EPIC_08 (M7)

## Success Criteria

The epic is done when:

- [ ] No order can reach an exchange adapter without a valid, unexpired, unused Kernel token — proven by the import-boundary test and the token-verification matrix
- [ ] PaperExchange chaos suite shows zero duplicate live orders, zero orders without valid tokens, zero net-short positions, and correct reservation release (§11.5)
- [ ] Gateway killed at any state edge recovers to consistent state via startup reconciliation before accepting new approvals (§11.4)
- [ ] Sweeper cancels resting orders on TTL expiry and price-band breach against recorded volatile-market fixtures
- [ ] All child issues are closed
- [ ] Smoke tests for the full epic surface pass on `main`

## Child Issues

- [ ] #37 — feat(gateway): Gateway process skeleton with token verification and typed state machine
- [ ] #38 — feat(gateway): Limit-only submission path with idempotent client order IDs
- [ ] #39 — feat(gateway): Reduce-only enforcement for closes
- [ ] #40 — feat(gateway): Crash recovery and continuous reconciler
- [ ] #41 — feat(gateway): Adverse-selection sweeper and volatility freeze
- [ ] #42 — test(gateway): Chaos suite over every order-state edge

## Sequencing Notes

- **Blocks:** EPIC_06 (M5 Selector + PAPER mode) — the selector's intents need a working approval→submission path.
- **Blocked by:** EPIC_04 (M3 Risk Kernel) — SPEC §18: "M3 blocks any code that could reach a real exchange"; the Gateway verifies tokens the Kernel signs. Also EPIC_02 (M1 Connector + PaperExchange) — all submission in this epic targets PaperExchange.
- **Parallel-safe:** EPIC_03 (M2 Forecast Engine) shares no files with this epic.
- SPEC §18 dependency order: M0 → M1 → {M2 ∥ M3} → M4 → M5 → M6 → M7.

## SPEC Reference

`plans/SPEC_v3.md` — §11 (Order Gateway, entire), §11.3 (state machine), §11.4 (crash recovery), §11.5 (acceptance criteria), §6.4 (NormalizedOrderIntent), §5.3 (order flow / import-boundary rule), §4 rows T3, T9, T13.

## Labels

`epic`, `spec-decomposition`, `order-gateway`
