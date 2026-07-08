## Epic Summary

This workstream delivers the Market Connector — the layer that normalizes exchange APIs into windbreak's domain model — plus the PaperExchange simulator that makes safe, pessimistic paper trading possible. It covers SPEC_v3 §7 (Market Connector) end-to-end, producing `NormalizedMarket` records (§6.2), a proven balance-semantics contract (§7.3), and the §17.4 pessimistic fill model. Per §18, this is milestone M1: "snapshots and screen decisions ledgered on schedule."

## Scope

**In scope:**
- `MarketConnector` interface (§7.2) and a fixture-backed `FakeExchange` adapter
- Kalshi adapter against the current API generation (read/public scope only)
- Fixed-point order-book parsing (no floats on money/price paths, §6.1)
- Market normalization including `mutually_exclusive_group_id` and `jurisdiction_status` (§6.2)
- Fee-model lookup and the machine-readable `BalanceSemantics` contract (§7.3)
- PaperExchange with the §17.4 pessimistic fill model
- Data quality & freshness: schema validation, TTLs, rate limiting, circuit breaker (§7.4)
- Recorded-fixture contract tests for every endpoint, including fault cases
- Screener filters (category blocklist, volume, depth, horizon) from §16 config

**Out of scope:**
- Forecast Engine (M2 / EPIC_03), Risk Kernel (M3 / EPIC_04), Order Gateway order submission (M4 / EPIC_05)
- Live order placement of any kind — `place_order` exists on the interface but no trade credentials exist anywhere in this epic
- Polymarket or any second exchange adapter (post-v1, §18 M8)

## Success Criteria

The epic is done when:

- [ ] `windbreak run` in RESEARCH mode ledgers market snapshots and screen decisions on schedule using the Kalshi adapter (demo environment) or FakeExchange
- [ ] The adapter publishes a `BalanceSemantics` record with zero `unknown` fields against recorded fixtures (§7.3 — a blocker for live trading later)
- [ ] PaperExchange property test proves no simulated fill is ever better than the recorded book allows (§7.6)
- [ ] Unknown money/risk-relevant fields in any exchange response halt trading (T8), proven by schema-drift fixtures
- [ ] All child issues are closed
- [ ] Smoke tests for the full epic surface pass on `main`

## Child Issues

- [ ] #16 — feat(connector): Wire MarketConnector interface with FakeExchange and snapshot-ledger loop
- [ ] #17 — feat(connector): Kalshi adapter with fixed-point parsing and product refusal
- [ ] #18 — feat(connector): Fee-model lookup and BalanceSemantics contract
- [ ] #19 — feat(connector): PaperExchange with pessimistic fill model
- [ ] #20 — feat(connector): Data-quality halts, freshness TTLs, rate limiting, circuit breaker
- [ ] #21 — test(connector): Recorded-fixture contract suite and screener filters

## Sequencing Notes

- **Depends on:** EPIC_01 (M0 Foundations) — needs the ledger, fixed-point numeric types, typed config loader, and scheduler heartbeat before anything here can land.
- **Blocks:** EPIC_03 (M2 Forecast Engine) needs normalized markets and baseline snapshots; EPIC_06 (M5 Selector) needs order books, fee models, and PaperExchange; EPIC_05 (M4 Gateway) needs `place_order`/`cancel_order` adapter surfaces.
- **Parallel-safe:** Nothing until EPIC_01 lands. After this epic's skeleton (ISSUE_01) merges, EPIC_03 and EPIC_04 skeletons can start against the `FakeExchange`.
- SPEC §18 dependency order: M0 → M1 → {M2 ∥ M3} → M4 → M5 → M6 → M7.

## SPEC Reference

`plans/SPEC_v3.md` — §7 (Market Connector, entire), §6.2 (NormalizedMarket), §6.1 (numeric units), §17.4 (paper-fill realism model), §16 (`screener:` config block), §18 (M1), threats T8/T18 (§4).

## Labels

`epic`, `spec-decomposition`, `connector`
