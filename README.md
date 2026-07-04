# hedgekit

An open-source, locally hosted, always-on AI forecast trader for fully collateralized binary event markets (e.g., Kalshi).

**Status:** Draft / pre-implementation. Building against [`SPEC_v3.md`](plans/SPEC_v3.md).

**License target:** Apache-2.0

## What this is

`hedgekit` is a local-first daemon that:

1. Screens prediction markets for questions where careful research can plausibly beat the crowd.
2. Uses an LLM "superforecaster" scaffold to produce calibrated probability estimates with verified citations.
3. Compares those estimates against live executable order books.
4. May create order intents — none of which can reach an exchange unless approved by an independent, veto-holding **Risk Kernel** and submitted through a token-verifying, credential-isolated **Order Gateway**.

The design descends from publicly documented AI-forecasting pipelines (screen by volume → exclude information-disadvantaged categories → deep LLM research → trade only where forecast and executable price disagree beyond fees). It deliberately does **not** assume the headline results around those pipelines are reproducible — widely cited figures come from a paper portfolio with no commissions/borrow costs/dividends, and a self-reported anecdote whose own author says the edge is already competed away.

## Important disclaimers

- **This is not investment advice.**
- Most operators should expect **no durable edge**. Discovering that and stopping at paper trading is a **success state** of this design, not a failure of the software.
- **Live trading is disabled by default.** Promotion from paper to live trading is gated by pre-registered, quantitative evidence — never by narrative or operator impatience.
- Only bounded-loss, fully collateralized binary event contracts are in scope. No margin, perps, options, leverage, or shorting-to-open.
- Legal eligibility to trade these products varies by jurisdiction and product; this software does not provide legal advice.
- The truest floor is money never deposited: fund the exchange only with risk capital, keep floor capital in an unlinked account, and grant only trade-scope API keys.

## Core invariants

1. **Floor Invariant** — worst-case equity must always be ≥ a configured floor, computed conservatively and enforced pre-trade by an independent process. Any unprovable input halts the system.
2. **Bounded-loss instruments only** — fully collateralized binary contracts with exactly known maximum loss at order time. Margin, perps, options, leverage, and equities/crypto spot/derivs are forbidden, not configurable.
3. **No trade credentials outside the Order Gateway** — research, forecasting, selection, and dashboard components never hold trade-capable credentials.
4. **Evidence-gated autonomy** — `RESEARCH → PAPER → LIVE_MICRO → LIVE`, with promotion by pre-registered quantitative gates only; demotion and halting are automatic.
5. **Research/execution firewall** — web content and model output can influence only the probability fields of a forecast; never config, credentials, routing, or control flow.
6. **Temporal integrity** — only real-time forecasts on then-unresolved questions count toward gates, guarding against LLM training-data leakage from backtests.
7. **Append-only auditability** — every snapshot, forecast, decision, veto, approval, order transition, and reconciliation is written to a hash-chained ledger.

## Architecture

Four isolated processes sharing only a ledger volume and localhost sockets:

- **Process A — Main pipeline** (no trade credentials): Market Connector → Screener → Forecast Engine → Trade Selector.
- **Process B — Risk Kernel** (read-only exchange credentials + signing key): independent veto authority over mode, floor enforcement, capital reservations, and approval tokens.
- **Process C — Order Gateway** (trade-scope credentials + verify key): the only component that can submit live orders; owns reconciliation.
- **Process D — Dashboard** (no exchange credentials, `127.0.0.1` only): visibility and a constrained set of allowed mutations (pause, kill, acknowledge, raise floor — never lower it).

Order flow has exactly one path: market snapshot → screen → (triage) → forecast → selector decision → order intent → Risk Kernel checks → capital reservation → signed approval token → Order Gateway verification → exchange submission → reconciliation → ledgered terminal state.

See [`plans/SPEC_v3.md`](plans/SPEC_v3.md) for the full specification, including the threat model, canonical data model, evaluation methodology, configuration reference, testing strategy, and milestone plan.

## Recommended language / stack

The spec's data model and tooling are Python-native: typed dataclasses/models for the canonical data types (`NormalizedMarket`, `ForecastRecord`, `NormalizedOrderIntent`, etc.), `mypy --strict` for type checking, `hypothesis` for property-based testing, `mutmut` for mutation testing, and `import-linter` for enforcing process/credential import boundaries in CI. SQLite (WAL mode) is the default ledger store, with Postgres supported behind the same repository interface.

## Status

Pre-implementation. No code has been written yet — this repository currently holds only the specification. Milestones (`M0`–`M8`) and their dependency graph are defined in §18 of the spec.
