## Epic Summary

Deliver M0 Foundations (plans/SPEC_v3.md §18 M0): the repo scaffold for the four-process architecture, the typed config loader, fixed-point numeric types with an AST float-lint, the hash-chained append-only ledger with rebuild, structured logging with secret redaction, the alert-sink abstraction, and docker-compose/systemd skeletons plus a stub dashboard. Done means `hedgekit run` idles in RESEARCH mode with visible heartbeats — the tracer skeleton every later milestone builds on.

## Scope

**In scope:**
- Package layout for processes A–D (`hedgekit/pipeline`, `hedgekit/riskkernel`, `hedgekit/order_gateway`, `hedgekit/dashboard`) plus shared `hedgekit/ledger`, `hedgekit/config`, `hedgekit/numeric`, `hedgekit/alerts` (SPEC §5.1).
- `hedgekit run` entrypoint idling in RESEARCH with heartbeat logging (§18 M0 done criterion).
- Typed config loader covering the full §16 schema; unknown keys fatal; config versions ledgered with hash + diff.
- Fixed-point numeric types `PricePips`, `ContractCentis`, `MoneyMicros`, `ProbabilityPpm` with conservative rounding and an AST lint forbidding floats on money/price/probability paths (§6.1, §17.3).
- Hash-chained append-only ledger on SQLite (WAL) with `hedgekit rebuild` equivalence (§12).
- Structured logging with secret redaction; alert-sink abstraction (ntfy, SMTP, webhook, desktop, log-only) with the §14 mandatory-alert registry.
- docker-compose + systemd unit skeletons for A–D; stub localhost-only dashboard page.

**Out of scope:**
- Any exchange connectivity or Kalshi adapter (EPIC_02 / M1).
- Forecasting, LLM calls, research sandbox (EPIC_03 / M2).
- Risk Kernel checks, tokens, floor math beyond type stubs (EPIC_04 / M3).
- Order Gateway submission paths (EPIC_05 / M4).

## Success Criteria

The epic is done when:

- [ ] `hedgekit run` starts, enters RESEARCH mode, and emits heartbeat log lines until interrupted (§18 M0 "Done").
- [ ] Loading a config file with one unknown key exits non-zero with a fatal error naming the key (§16).
- [ ] The AST float-lint fails CI when a float literal or `float` annotation touches a money/price/probability path (§17.3).
- [ ] `hedgekit rebuild` reproduces byte-identical read-model state from the event ledger in CI (§12).
- [ ] A secret value passed through logging is redacted in output; every §14 mandatory alert has a registered emitter that reaches the configured sink.
- [ ] All child issues are closed.
- [ ] Smoke tests for the full epic surface pass on `main`.

## Child Issues

_Filled in after child issues are filed (Step 8/9 of spec-decomposition)._

- [ ] #NNN — Skeleton: four-process package layout + `hedgekit run` heartbeat idle loop
- [ ] #NNN — Core: typed config loader with unknown-keys-fatal and ledgered versions
- [ ] #NNN — Core: fixed-point numeric types + AST float-lint
- [ ] #NNN — Core: hash-chained append-only ledger + `hedgekit rebuild`
- [ ] #NNN — Edges: structured logging with secret redaction + alert-sink abstraction
- [ ] #NNN — Polish: docker-compose/systemd skeletons + stub localhost dashboard

## Sequencing Notes

- **Blocks:** every other epic. M0 is the root of the SPEC §18 dependency graph (`M0 → M1 → {M2 ∥ M3} → M4 → M5 → M6 → M7`).
- **Unblocks:** EPIC_02 (Connector + PaperExchange) immediately on completion.
- **Parallel-safe:** nothing — this epic lands first. Within the epic, issues 02/03/04 may proceed in parallel after issue 01 merges; issue 05 depends on 04 (alerts are ledgered); issue 06 depends on 01 only.

## SPEC Reference

plans/SPEC_v3.md — §18 "M0 — Foundations"; §5.1 (component topology), §6.1 (numeric units), §12 (Ledger & State), §14 (Dashboard & Alerts, alert sinks + mandatory alerts), §16 (Configuration), §17.3 (accounting proofs), §3 (guiding principles).

## Labels

`epic`, `spec-decomposition`, `foundations`
