## Role

You are a senior Python engineer working in this repo's `hedgekit/connector/` subpackage, experienced in resilient API clients (rate limiting, backoff, circuit breakers) and fail-closed system design.

## Goal

Every connector response is schema-validated with unknown money/risk-relevant fields halting trading (T8); order-book snapshots carry `fetched_at` with consumer-enforced TTLs; the Kalshi client gains token-bucket rate limiting, exponential backoff with jitter, a circuit breaker that HALTs after N consecutive failures, and maintenance-window suspension — all proven by fault-injection fixtures.

## Context

- **Parent epic:** #3
- **Predecessor issue(s):** #19 (must be merged first — full adapter surface exists to harden)
- **SPEC section:** `plans/SPEC_v3.md` §7.4 (data quality & freshness — TTL defaults: 30s selection, 10s approval/submission), §4 T5 (stale data trading) and T8 (exchange API semantic change), §3 principle 3 (every safety mechanism fails closed — API down → no trading, never blind trading), §7.6 (fault cases in acceptance criteria)
- **Files involved:**
  - `hedgekit/connector/validation.py` — versioned response schemas; unknown-field policy (unknown money/risk field → ledgered `SCHEMA_ANOMALY` + halt signal; unknown cosmetic field → warn)
  - `hedgekit/connector/freshness.py` — `fetched_at` stamping and TTL check helpers for consumers
  - `hedgekit/connector/resilience.py` — token bucket, backoff with jitter, circuit breaker
  - `hedgekit/connector/kalshi/client.py` — wire resilience in; suspend on maintenance windows from `get_exchange_status()`
  - `tests/connector/test_validation.py`, `tests/connector/test_resilience.py`
  - `tests/fixtures/exchange/kalshi/faults/` — error, rate-limit, malformed, schema-drift fixtures
- **Prior decisions:** "Halt" here means emitting the halt signal/event the M0 scheduler and future Risk Kernel consume — the connector never exits the process itself. Distinguish drifted-money-field (halt) from added-cosmetic-field (warn): the T8 mitigation targets fields affecting money/risk.
- **State of the world:** Adapter, fees/semantics, and PaperExchange exist; the client currently does naive requests with a plain timeout and trusts payload shapes.

## Output Format

Deliverable is a single PR containing:

- [ ] Versioned schema validation on every endpoint response; schema-drift fixture proves an unknown money/risk field produces a ledgered `SCHEMA_ANOMALY` and a trading-halt signal, while an unknown cosmetic field only warns
- [ ] `fetched_at` on every order-book snapshot and quote; TTL helper that consumers call with their own limit (selection 30s, approval 10s per §7.4 — values from config, not constants)
- [ ] Client-side token-bucket rate limiter honoring configured request budgets
- [ ] Exponential backoff with jitter on retryable failures; non-retryable failures surface immediately
- [ ] Circuit breaker: N consecutive failures (config) → open circuit → ledgered HALT signal; half-open probe recovery
- [ ] Maintenance windows from `get_exchange_status()` suspend snapshot fetching and (future) submission, ledgered
- [ ] Fault-injection tests covering: HTTP 5xx, 429 rate-limit, malformed JSON, truncated body, schema drift — each asserting fail-closed behavior
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands**

```python
def test_unknown_money_field_halts(fault_connector, ledger):
    fault_connector.serve_fixture("faults/orderbook_new_fee_field.json")
    with pytest.raises(SchemaAnomalyHalt):
        fault_connector.get_order_book("MKT-A")
    assert ledger.events_by_type("SCHEMA_ANOMALY")

def test_circuit_breaker_opens_and_halts(fault_connector, ledger):
    fault_connector.serve_repeated_500s(count=5)
    for _ in range(5):
        with pytest.raises(ConnectorError):
            fault_connector.get_exchange_status()
    assert fault_connector.circuit.state is CircuitState.OPEN
    assert ledger.events_by_type("CONNECTOR_HALT")
```

## Constraints

**Scope fence:** Do not implement Risk Kernel halt handling (EPIC_04) — emit the signal/event only. Do not add websocket support or split-brain detection (§17.5, later epic). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — the snapshot loop keeps running under normal fixtures; it halts (visibly, ledgered) only under fault fixtures. If your change breaks an unrelated surface, revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #3` and `Closes #20`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `connector`
