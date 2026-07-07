# ADR-0003: Order Gateway write-ahead log distinct from the ledger, and centralized event definitions

- **Status:** Accepted
- **Date:** 2026-07-07
- **Issue:** #40 (Order Gateway crash recovery and continuous reconciler, SPEC
  §11.4); closes handoffs from #38 and #39

## Context

Issue #40 makes the Order Gateway survive death at any point in the order
lifecycle: on startup it must load durable state, fetch the exchange's open
orders / positions / fills, reconcile the two, halt on any unexplained mismatch,
and only then accept new approvals; a continuous Reconciler runs the same diff
every 60s. Two design questions had to be settled before the recovery code could
be written.

**1. Where does the pre-submission durable record live?** The system already has
one durable append-only log — the hash-chained SQLite ledger
(`hedgekit/ledger/store.py`) — that records every order-lifecycle *transition*
(`OrderTransitionLedgered`). But a transition record deliberately carries only
the four state-machine fields (`client_order_id`, `from_state`, `event`,
`to_state`); it does **not** carry the full economics of the intent. Crash
recovery needs to re-derive the deterministic `client_order_id` (a SHA-256 over
all nine `OrderIntent` fields, from #38) and re-associate a bare exchange
`OpenOrder` (which has no `client_order_id` field) with the intent that produced
it. The ledger alone cannot answer "what intent was this order, and what venue
order-id did it become?"

**2. How do the Gateway's ledger events reach `EVENT_TYPES`?** #38/#39 defined
the four gateway events (`OrderTransitionLedgered`, `SubmissionRefused`,
`ReduceOnlyRefused`, `ReduceOnlyViolation`) inside
`hedgekit/order_gateway/ledger_writer.py`, which imports `Event` from
`hedgekit/ledger/events.py`. Every *other* ledgered event in the codebase (the
M0 events, the Risk Kernel kill/promotion/demotion events) is defined directly in
`events.py` and registered in its `EVENT_TYPES` literal, so a persisted envelope
can be reconstructed as `EVENT_TYPES[event_type](component=..., **data)`. The
#38 handoff requires the gateway events to join `EVENT_TYPES` (so `rebuild` stops
silently skipping them). Registering them from `ledger_writer.py` would force
`events.py` to import `ledger_writer.py`, which already imports `events.py` — a
circular import that breaks whenever `ledger_writer` is imported first.

## Decision

**1. The write-ahead log is a separate append-only JSONL file, not the ledger.**
`hedgekit/order_gateway/wal.py` provides `WriteAheadLog`, one
`canonical_json` line per record, `flush` + `os.fsync` per append (and a
parent-directory `fsync` on first create), with two record kinds:

- an **intent** record — the full nine `OrderIntent` fields (the four scaled-int
  money-path fields as their bare `.value` ints, SPEC §6.1), written *before* the
  `REQUEST_SUBMISSION` transition is ledgered. This guarantees no order can rest
  on the exchange without a durable intent to reconcile it against.
- an **ack** record — the venue-order-id ↔ `client_order_id` correlation plus the
  immediately-filled quantity, written the instant `place` returns.

The signed approval token and any key material are **never** written to the WAL
(nor to any ledger payload). `read_all` reconstructs each `OrderIntent` from ints
only and re-derives its `client_order_id`, raising on any mismatch so a tampered
journal can never silently mis-attribute an order.

**2. Gateway event definitions are centralized in `hedgekit/ledger/events.py`.**
The four gateway event classes move verbatim into `events.py` and are added to
the `EVENT_TYPES` literal alongside the three new recovery events
(`ReconciliationHalted`, `ReconciliationHealed`, `RecoveryCompleted`).
`ledger_writer.py` re-exports the four moved classes
(`from hedgekit.ledger.events import OrderTransitionLedgered as ...`) so every
existing import site (`gateway.py`, the package `__init__`, the #38/#39 tests) is
unchanged. `ledger_writer.py`'s now-dead local `_derive_typed_event` /
`_SCHEMA_VERSION` are deleted.

## Alternatives considered

1. **Reuse the ledger as the write-ahead record (no separate WAL).** Rejected:
   the ledger's `OrderTransitionLedgered` intentionally omits intent economics,
   and widening it to carry the full nine-field intent would (a) bloat every
   transition row, (b) leak the intent economics into a schema whose purpose is
   the state machine, and (c) still not carry the venue-order-id ↔ coid map,
   which only exists *after* the exchange acks. A purpose-built pre-commit log is
   the write-ahead-logging pattern the SPEC (§11.4) names explicitly.

2. **Register the gateway events into `EVENT_TYPES` via a `register_event_type()`
   hook called from `ledger_writer.py` on import.** Rejected: reconstruction in
   `rebuild` would then `KeyError` unless `order_gateway` had already been
   imported, making a pure ledger fold order-dependent on an unrelated package's
   import, and the exact-set test on `EVENT_TYPES` would flap depending on import
   order. Centralizing the definitions removes the circular import at the root
   rather than papering over it.

3. **Leave the gateway events in `ledger_writer.py` and import them at the bottom
   of `events.py`.** Rejected: this is the circular import — it only "works" when
   `events.py` is imported before `ledger_writer.py`, and fails otherwise.

## Consequences

- **Positive:** Recovery has exactly the durable inputs it needs — the ledger for
  lifecycle state and the halt latch, the WAL for intent economics and the
  venue-id join key — with a clean separation of concerns. `EVENT_TYPES` is once
  again the single, import-order-independent registry of every ledgered event, so
  `rebuild` reconstructs gateway events and projects them into a new
  `gateway_events.json` read model.
- **Fail-safe by construction:** the WAL is written before the externally-visible
  action at every edge, so a crash leaves recovery enough truth to converge with
  zero duplicated and zero lost orders; any residual ambiguity (e.g. a placed
  order whose ack-record never landed) resolves to a halt, never an unsafe
  action (SPEC §3.2, "when in doubt, halt").
- **Durable #39 handoff closed:** the in-flight-closes tally and the net-short
  halt latch — previously in-memory only — are now rebuilt on restart by folding
  the ledger (halt latch; no un-halt event exists, so a halted Gateway stays
  halted) and the WAL against live exchange open orders (tally retired for
  settled closes, preserved for still-open ones).
- **Residual risk (accepted):** fill attribution in recovery/reconciliation
  matches on ticker/side/limit-price, so two distinct Gateway orders resting at
  the identical ticker/side/limit share one attribution pool. Content-addressed
  client-order-ids keep field-identical intents idempotent (never two live
  orders), and any non-positive attributable remainder defers to the halt path.
