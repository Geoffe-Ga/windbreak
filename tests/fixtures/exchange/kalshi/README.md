# Kalshi fixture evidence: `BalanceSemantics` (issue #18)

SPEC §7.3 ("Balance-semantics contract") calls the `BalanceSemantics` record a
**blocker for live trading**: the Risk Kernel refuses to trade live while any
one of its eight fields is `unknown`. This directory holds the fixtures that
back Kalshi's recorded answers -- one fixture (or fixture pair) per field --
so each field's value in
`hedgekit.connector.kalshi.adapter.KALSHI_BALANCE_SEMANTICS` is proven by a
concrete before/after payload rather than asserted from memory. The tests in
`tests/connector/kalshi/test_balance_semantics.py` pin the record against
this evidence.

## Evidence table

| Field | Resolved value | Fixture(s) | Justification |
|---|---|---|---|
| `fee_debit_timing` | `FeeDebitTiming.AT_EXECUTION` | `fills.json` | Each fill record carries its own non-null `fee`, recorded at the fill's `created_time` -- i.e. at execution, not deferred to settlement. |
| `fee_rounding` | `FeeRounding.UP_TO_NEXT_CENT` | `series_KXFED.json` | Kalshi's documented fee formula (`fee = ceil(rate * C * P * (1-P))`) rounds every fee up to the next whole cent; see `hedgekit.connector.fees.FeeModel.max_trading_fee_micros`, which mirrors this rounding as its conservative (overstate-cost) bound. |
| `partial_fill_representation` | `PartialFillRepresentation.PER_FILL_RECORDS` | `fills.json` | `trade-1` and `trade-2` share one `order_id` (`"order-abc123"`) but are two distinct fill records, each with its own `count` and `fee`, rather than one aggregated fill. |
| `unsettled_proceeds` | `UnsettledProceeds.EXCLUDED_UNTIL_CREDITED` | `settlements.json` | `balance_before_settlement` (89,000,000 micros) only becomes `balance_after_settlement` (189,000,000 micros -- a jump of exactly the `revenue` field: 10,000 cents = 100,000,000 micros) strictly after `settled_time`; proceeds are never visible earlier. |
| `open_order_collateral_in_available` | `OrderCollateralInAvailable.DEDUCTED_FROM_AVAILABLE` | `resting_order_balance.json` | `balance_before_order` vs. `balance_after_order` shows `available_balance` drop by exactly the resting order's max cost (50 contracts x 45c = 2,250 cents = 22,500,000 micros: 100,000,000 -> 77,500,000), while `balance` (total) is unchanged. |
| `open_order_collateral_in_total` | `OrderCollateralInTotal.UNKNOWN` | -- | No fixture evidence exists either way; see "Deliberately unknown" below. |
| `cancel_collateral_release` | `CancelCollateralRelease.UNKNOWN` | -- | No fixture evidence exists either way; see "Deliberately unknown" below. |
| `halted_market_behavior` | `HaltedMarketBehavior.UNKNOWN` | -- | No fixture evidence exists either way; see "Deliberately unknown" below. |

## Deliberately unknown, not guessed

Three fields stay `UNKNOWN`:

* `open_order_collateral_in_total` -- whether a resting order's posted
  collateral is folded into the reported account *total* (as opposed to
  *available*) is not documented in Kalshi's public API, and no
  before/after total-balance pair around placing an order was recorded.
* `cancel_collateral_release` -- whether collateral frees immediately on
  cancel-acknowledgment or only after some later step is not committed to
  in Kalshi's public docs, and no before/after balance pair around a
  cancellation was recorded.
* `halted_market_behavior` -- whether new orders are rejected or accepted
  against a halted market is not committed to in Kalshi's public docs, and
  no fixture captures order-submission behavior during a halt.

SPEC §7.3 requires these questions be answered "via fixture tests and, where
available, demo-environment tests" -- it does not permit inferring a
convenient default when no such evidence exists. These three fields are
therefore left at their enum's explicit `UNKNOWN` member on purpose: an
undocumented behavior is *recorded as unknown*, never silently guessed. Each
would be promoted to a known member only by future evidence of the same
shape as the five known rows above -- e.g. a demo-environment before/after
balance pair straddling a cancel, a total-balance snapshot pair straddling a
resting order, or an observed order-submission response against a halted
market.

## Fee-schedule fixtures are an invented shape

* `series_KXFED.json` -- a recorded `/series/{ticker}` fee-schedule document.
  **The shape is invented**: Kalshi's real `/series/{ticker}` fee-field
  layout was not confirmed from this offline environment. It carries a
  `fee_type` discriminator that acts as a sanctioned-schedule allowlist --
  `"quadratic"` is the only value the adapter accepts; any other value fails
  closed with `UnknownFeeModelError` rather than being misread -- plus
  `maker_fee_bps` / `taker_fee_bps` / `settlement_fee_bps` in basis points
  (1 bp = 100 ppm, so the adapter's `_bps_to_ppm` multiplies by 100), and an
  explicit `fee_schedule_id` copied verbatim into `FeeModel.schedule_id`.
  This fixture normalizes to `maker_fee_ppm=0`, `taker_fee_ppm=70_000`,
  `settlement_fee_ppm=0` -- the rate behind the golden trading-fee example
  proven in `tests/connector/kalshi/test_fees.py`: **$1.75 (1,750,000
  micros) for 100 contracts at 50c**, the issue's worked example for
  `FeeModel.max_trading_fee_micros`.
* `series_KXBAD.json` -- the same invented shape with an unrecognized
  `fee_type` (`"mystery_v9"`), backing the malformed-schedule fail-closed
  test.

Per SPEC §20 Q1, fee schedules must be data pulled from the exchange's live
fee schedule, never hardcoded from a secondary source, with golden tests
checked against exchange-documented examples. Confirming the real
`/series/{ticker}` schema against Kalshi's live API -- and re-recording these
two fixtures against it if the real shape differs -- is a follow-up, not
something this offline environment can verify.

## Fixture inventory

* `series_KXFED.json` -- see above.
* `series_KXBAD.json` -- see above.
* `fills.json` -- two fill records sharing one `order_id`, each with its own
  per-fill `fee`: evidence for `partial_fill_representation` and
  `fee_debit_timing`.
* `settlements.json` -- a settlement record plus a balance snapshot pair
  taken immediately before and after `settled_time`: evidence for
  `unsettled_proceeds`.
* `resting_order_balance.json` -- a balance snapshot pair taken immediately
  before and after placing one resting order: evidence for
  `open_order_collateral_in_available`.

These fixtures are evidence artifacts, not (yet) wired into `KalshiConnector`
methods beyond `get_fee_model` (`series_KXFED.json` / `series_KXBAD.json`).
`fills.json` (top-level list, keyed by `id`/`price`/`quantity`) already backs
`FakeExchange.get_fills`; this directory's `fills.json` is the
Kalshi-raw-shape sibling used only as semantics evidence, not loaded by
`FakeExchange`.
