## Role

You are a senior Python engineer working in this repo's `hedgekit/` package, experienced in designing typed adapter interfaces (Protocol/ABC) and event-sourced persistence.

## Goal

A `MarketConnector` interface exists with a fixture-backed `FakeExchange` implementation, and `hedgekit run` (RESEARCH mode) fetches snapshots from it on schedule and writes market snapshots plus screen decisions to the ledger — proven by smoke tests.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** none — this is the skeleton issue for this epic (requires EPIC_01/M0 foundations merged: ledger, fixed-point types, config loader, scheduler)
- **SPEC section:** `plans/SPEC_v3.md` §7.2 (interface), §6.2 (NormalizedMarket), §5.3 (order flow starts at "market snapshot → screen decision"), §18 M1
- **Files involved:**
  - `hedgekit/connector/__init__.py` — new subpackage
  - `hedgekit/connector/interface.py` — `MarketConnector` Protocol/ABC with the §7.2 method set
  - `hedgekit/connector/fake.py` — `FakeExchange` returning schema-valid stub data from fixtures
  - `hedgekit/screener/__init__.py` — stub screener that ledgers a `ScreenDecision` per market (pass/blocked + reason; real filters land in ISSUE_06)
  - `tests/connector/` — smoke + interface-conformance tests
  - `tests/fixtures/exchange/` — JSON fixtures for stub responses
- **Prior decisions:** All money/price/probability values are fixed-point integers per §6.1 (`PricePips`, `ContractCentis`, `MoneyMicros`, `ProbabilityPpm`) — the M0 numeric types must be used from day one; no float ever enters these fields. `market_type` is the literal `"fully_collateralized_binary"` only.
- **State of the world:** `hedgekit/` contains the M0 scaffold (config loader, ledger, scheduler heartbeat, `main.py`). No connector code exists yet.

## Output Format

Deliverable is a single PR containing:

- [ ] `MarketConnector` interface exposing exactly the §7.2 surface: `list_markets()`, `get_market(t)`, `get_order_book(t)`, `get_exchange_status()`, `get_exchange_time()`, `get_balance_semantics()`, `get_balances()`, `get_positions()`, `get_open_orders()`, `get_fills(since)`, `get_fee_model(market_or_series)`, `place_order(normalized_intent, approval_token)`, `cancel_order(id)`
- [ ] `FakeExchange` implementing the full interface from fixtures; `place_order`/`cancel_order` raise `NotImplementedError` (no order path exists until M4)
- [ ] `NormalizedMarket` model per §6.2 with all fields, including `mutually_exclusive_group_id`, `jurisdiction_status`, `raw_exchange_payload_hash`
- [ ] Scheduler task that snapshots markets and ledgers `MARKET_SNAPSHOT` and `SCREEN_DECISION` events on a configurable interval
- [ ] Smoke tests proving: interface conformance of `FakeExchange`; one scheduler tick produces ledgered snapshot + screen-decision events; every fixture round-trips schema validation
- [ ] Docstrings on all public interfaces
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands**

```python
def test_scheduler_tick_ledgers_snapshot_and_screen_decision(fake_exchange, ledger, scheduler):
    scheduler.run_once("market_snapshot")
    events = ledger.events_by_type("MARKET_SNAPSHOT")
    assert len(events) == len(fake_exchange.list_markets())
    decisions = ledger.events_by_type("SCREEN_DECISION")
    assert len(decisions) == len(events)
    assert all(d.payload["decision"] in ("eligible", "blocked") for d in decisions)
```

**Example: FakeExchange returns a schema-valid NormalizedMarket**

```python
market = FakeExchange.from_fixture_dir("tests/fixtures/exchange").get_market("KXFED-24DEC")
assert market.market_type == "fully_collateralized_binary"
assert market.jurisdiction_status in ("eligible", "ineligible", "unknown")
assert isinstance(market.price_tick_pips, int)
```

## Constraints

**Scope fence:** Do not implement the Kalshi HTTP adapter (ISSUE_02), fee/balance semantics (ISSUE_03), PaperExchange fills (ISSUE_04), freshness/rate-limit machinery (ISSUE_05), or real screener filters (ISSUE_06). The screener here ledgers stub decisions only. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — `hedgekit run` still idles in RESEARCH with visible heartbeats, now also ledgering snapshots. If your change breaks an unrelated surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `connector`
