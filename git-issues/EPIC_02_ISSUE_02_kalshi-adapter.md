## Role

You are a senior Python engineer working in this repo's `windbreak/connector/` subpackage, experienced with exchange REST APIs, defensive deserialization, and fixed-point arithmetic.

## Goal

A `KalshiConnector` implements the `MarketConnector` interface against the current Kalshi API generation (read/public scope), parsing order books into fixed-point integers and normalizing markets — including mutually-exclusive groups and jurisdiction status — while refusing to normalize any margin/perp/derivative product.

## Context

- **Parent epic:** #3
- **Predecessor issue(s):** #16 (must be merged first — interface and NormalizedMarket exist)
- **SPEC section:** `plans/SPEC_v3.md` §7.1 (responsibility; "no deprecated endpoints"; "must explicitly reject any margin/perp/derivative product surfaces"), §6.1 (numeric units), §6.2, §1.1 invariant 2 (bounded-loss only — "the connector must refuse to normalize such products … even if the exchange API exposes them"), §20 Q1 (fee fields pulled from live schedule at M1, never hardcoded from blog posts)
- **Files involved:**
  - `windbreak/connector/kalshi/client.py` — thin HTTP client, read/public endpoints only
  - `windbreak/connector/kalshi/adapter.py` — `KalshiConnector(MarketConnector)`
  - `windbreak/connector/kalshi/normalize.py` — raw payload → `NormalizedMarket` / order book, fixed-point conversion
  - `tests/connector/kalshi/` — contract tests against recorded fixtures
  - `tests/fixtures/exchange/kalshi/` — recorded API responses (markets, events, order books, exchange status)
- **Prior decisions:** ISSUE_01 fixed the interface surface; all prices parse to `PricePips` (int, 0.0001 payout-dollars — 1¢ = 100 pips), sizes to `ContractCentis`; rounding is always conservative (overstate cost/risk, understate equity, §6.1). No trade or withdrawal credentials exist in this component (§5.2 — Market Connector holds public/read-only only).
- **State of the world:** `MarketConnector` interface and `FakeExchange` exist. No HTTP code exists. CI runs offline — all tests must use recorded fixtures, no live network calls.

## Output Format

Deliverable is a single PR containing:

- [ ] `KalshiConnector` implementing every read/public interface method (`place_order`/`cancel_order` still raise — order path is M4)
- [ ] Fixed-point parsing of all price/size/money fields with zero float intermediaries (assert via the M0 AST float-lint on these modules)
- [ ] Normalization populating `mutually_exclusive_group_id` from Kalshi event structure and `jurisdiction_status` (`"unknown"` when the API does not expose eligibility — see §20 Q3; `"unknown"` must raise an alert per §6.2)
- [ ] A product-type gate: any payload whose product surface is not a fully collateralized binary is rejected with a ledgered `PRODUCT_REFUSED` event — never silently skipped, never normalized
- [ ] `raw_exchange_payload_hash` recorded on every normalized market
- [ ] Contract tests on recorded fixtures for every implemented endpoint, including at least one rejected-product fixture
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands**

```python
def test_orderbook_prices_are_fixed_point_pips(kalshi_fixture_connector):
    book = kalshi_fixture_connector.get_order_book("KXFED-24DEC")
    for level in book.yes_bids + book.yes_asks:
        assert isinstance(level.price_pips, int)
        assert 0 < level.price_pips < 10_000  # 0–$1.00 in pips
        assert isinstance(level.count_centis, int)

def test_margin_product_is_refused(kalshi_fixture_connector, ledger):
    markets = kalshi_fixture_connector.list_markets()
    assert "FAKE-PERP" not in [m.ticker for m in markets]
    assert ledger.events_by_type("PRODUCT_REFUSED")
```

## Constraints

**Scope fence:** Do not implement fee models or balance semantics (ISSUE_03), fill simulation (ISSUE_04), or rate limiting/backoff/circuit breakers (ISSUE_05 — a plain HTTP timeout is fine for now). Do not add trade-scope auth of any kind. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — the snapshot-and-ledger loop from ISSUE_01 now works with either `FakeExchange` or `KalshiConnector` (fixture-fed in tests). If your change breaks an unrelated surface, revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #3` and `Closes #17`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `connector`
