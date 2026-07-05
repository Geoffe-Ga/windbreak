# SPEC_v3 Decomposition Summary — 2026-07-04

One-page restatement of [`plans/SPEC_v3.md`](../plans/SPEC_v3.md) and the epic map derived from it.

## What hedgekit is

A local-first, always-on daemon that screens prediction markets, produces calibrated LLM-ensemble probability forecasts with verified citations, compares them against live executable order books, and may emit order intents — which reach an exchange only through an independent veto-holding Risk Kernel and a credential-isolated Order Gateway. v1's success metric is **demonstrated forecast calibration and provable capital safety**, not profit; stopping at paper trading with "no durable edge" is a designed success state.

## Non-negotiables the decomposition must preserve (SPEC §1.1)

Floor invariant (worst-case equity ≥ configured floor, arithmetic pre-trade check); bounded-loss instruments only; no trade credentials outside the Order Gateway; evidence-gated autonomy (RESEARCH → PAPER → LIVE_MICRO → LIVE with pre-registered gates); research/execution firewall; temporal integrity (no backtest evidence); append-only hash-chained auditability.

## Epic map (SPEC §18: "milestones map to epics")

| Epic | Milestone | Delivers | Depends on |
|------|-----------|----------|------------|
| EPIC_01 foundations | M0 | Package/process skeleton, config loader, fixed-point types, hash-chained ledger, logging/alerts, deploy skeletons — `hedgekit run` idles with heartbeats | — |
| EPIC_02 connector | M1 | Kalshi adapter, normalization, fee model, balance-semantics contract, pessimistic PaperExchange | EPIC_01 |
| EPIC_03 forecast_engine | M2 | Research sandbox, triage, ensemble + canaries, citation verification, injection defense | EPIC_01, EPIC_02 (∥ EPIC_04) |
| EPIC_04 risk_kernel | M3 | Separate-process veto authority: floor, reservations, tokens, governance, kill. **Blocks all real-exchange code.** | EPIC_01, EPIC_02 (∥ EPIC_03) |
| EPIC_05 order_gateway | M4 | Credential-isolated submission, order lifecycle, reduce-only, sweeper, crash recovery, chaos suite | EPIC_04, EPIC_02 |
| EPIC_06 selector_paper | M5 | Fee-aware executable edge, dispersion-scaled Kelly, price bands, correlation buckets, always-on PAPER loop | EPIC_03, EPIC_04, EPIC_05 |
| EPIC_07 evaluation | M6 | Three-track metrics, clustered bootstrap, temporal integrity, pre-registration. **Gates any live promotion.** | EPIC_06 |
| EPIC_08 live_micro | M7 | Production preflight, micro-cap deployment, drills, live-vs-paper comparison, full doc set | EPIC_07 |

M8 (Polymarket adapter, local models, strategy-driven exits, etc.) is explicitly post-v1 and **not decomposed here**.

## Open questions (SPEC §20) — staged, not blocking

The spec pre-assigns its open questions to milestones (fee schedule details → M1; demo-environment fidelity → M1/M5; jurisdiction API exposure → M1; idle-cash APR → M5; bucket-taxonomy governance → M5; host separation → deployment docs; canary composition → M2; gate confidence level → M6). Each owning epic's issues carry the relevant question in Context rather than inventing answers.

## Conventions used in every issue

6-component prompt frame (Role/Goal/Context/Output Format/Examples/Constraints); tracer-code sequencing (skeleton first, demoable after every merge); stay-green Done-Done (tests + pre-commit + ≥90% coverage + Claude reviewer `LGTM` verdict); max-quality-no-shortcuts anti-bypass clause verbatim. Placeholders `EPIC_NN_NUMBER` / `EPIC_NN_ISSUE_MM_NUMBER` are substituted with real GitHub numbers at filing time.
