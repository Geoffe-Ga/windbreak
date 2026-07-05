## Role

You are a senior Python engineer working in this repo's `hedgekit/connector/` and `hedgekit/screener/` subpackages, experienced in contract testing and config-driven filtering.

## Goal

Every connector endpoint has recorded-fixture contract tests (happy path + the §7.6 fault matrix), and the screener applies the real §16 filters — category blocklist, minimum 24h volume, minimum depth, horizon bounds — replacing ISSUE_01's stub decisions, completing M1's done criterion: snapshots and screen decisions ledgered on schedule.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** #EPIC_02_ISSUE_05_NUMBER (must be merged first — validation/fault machinery exists to test against)
- **SPEC section:** `plans/SPEC_v3.md` §7.6 (acceptance criteria — "recorded-fixture contract tests for every endpoint including error/rate-limit/malformed/schema-drift cases; fixed-point preservation with no float conversion anywhere in the path"), §16 `screener:` block (`category_blocklist: [sports, crypto_price, celebrity, insider_prone]`, `min_volume_24h_micros`, `min_depth_contract_centis`, `horizon_days {min: 2, max: 120}`), §1.2 (sports blocked by default; unblocking requires explicit config plus a ledgered legal-risk acknowledgement), §18 M1 done criterion
- **Files involved:**
  - `hedgekit/screener/filters.py` — pure filter functions over `NormalizedMarket` + book stats
  - `hedgekit/screener/screener.py` — compose filters from typed config; ledger every decision with pass/fail reasons per filter
  - `tests/connector/test_contract_matrix.py` — parametrized endpoint × scenario matrix
  - `tests/screener/test_filters.py` — per-filter unit + property tests
  - `tests/fixtures/exchange/kalshi/` — any fixtures still missing from the matrix
- **Prior decisions:** Screen decisions are first-class ledger events (§12 read model `screen_decisions`); a blocked market records which filter blocked it and the measured value, so §13.3 selection-bias reports can later show excluded-by-category and excluded-by-liquidity cohorts. Fixed-point throughout — volume in `MoneyMicros`, depth in `ContractCentis`.
- **State of the world:** All connector machinery (ISSUE_01–05) exists; the screener still ledgers stub pass-through decisions from ISSUE_01. Contract tests exist piecemeal per issue but not as a systematic endpoint × scenario matrix.

## Output Format

Deliverable is a single PR containing:

- [ ] Parametrized contract-test matrix: every `MarketConnector` endpoint × {happy, error, rate-limit, malformed, schema-drift} against recorded fixtures, for both `KalshiConnector` and `PaperExchange`
- [ ] AST/property check asserting fixed-point preservation — no float appears anywhere in the response-parsing path (extends the M0 float-lint to the whole connector package)
- [ ] Screener filters: category blocklist (sports blocked by default; unblocking path requires config flag + ledgered legal-risk acknowledgement event), `min_volume_24h_micros`, `min_depth_contract_centis`, `horizon_days` min/max
- [ ] Every screen decision ledgered with per-filter outcomes and measured values
- [ ] Property tests: filters are pure and deterministic; a market failing any filter is never eligible; tightening any threshold never admits a previously-blocked market
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands**

```python
def test_sports_blocked_by_default(screener, sports_market):
    decision = screener.screen(sports_market, book_stats_ok)
    assert decision.eligible is False
    assert decision.blocked_by == ["category_blocklist"]

def test_screen_decision_ledgered_with_reasons(screener, ledger, thin_market):
    screener.screen(thin_market, book_stats_thin)
    event = ledger.events_by_type("SCREEN_DECISION")[-1]
    assert event.payload["filters"]["min_depth_contract_centis"]["passed"] is False
    assert isinstance(event.payload["filters"]["min_depth_contract_centis"]["measured"], int)
```

## Constraints

**Scope fence:** Do not implement forecast triage (EPIC_03/M2 §8.4 owns the triage threshold), refresh triggers, or operator-flag plumbing beyond reading typed config. Do not implement the §13.3 reports themselves — only ledger the data they need. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — `hedgekit run` now ledgers real screen decisions with reasons, on schedule, against fixtures or the demo environment. This completes the M1 done criterion. If your change breaks an unrelated surface, revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `connector`
