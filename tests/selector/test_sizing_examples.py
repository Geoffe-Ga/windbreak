"""Gate 1 RED, hand-computed examples for `windbreak.selector.sizing` (issue #45).

`windbreak/selector/sizing.py` does not exist yet, so every test below fails
collection with `ModuleNotFoundError: No module named 'windbreak.selector.sizing'`
-- the expected Gate 1 RED state for issue #45's sizing seam. The tests
importing `PositionReadModelInput` from `windbreak.selector.types` fail the
same way today (that symbol does not exist yet either): both are the correct
RED reason -- missing symbols for not-yet-wired behavior, never a typo.

Every expected number below is hand-derived in a comment directly above the
assertion it backs, using the exact fused-division formulas the chief
architect pinned:

    g(d, ceiling) = dispersion_scale(...)  -- the linear dispersion ramp.
    stake_micros  = divide(capital * net_edge_ppm * kelly_fraction_ppm * g,
                            (1_000_000 - executable_price_ppm) * 10**12,
                            UNDERSTATE_EQUITY)
    size_centis   = divide(stake_micros * 100, executable_price_ppm,
                            UNDERSTATE_EQUITY)
    cap_size_centis = divide(headroom_micros * 100, executable_price_ppm,
                              UNDERSTATE_EQUITY)
        where headroom_micros = max(0, ceiling_micros - exposure_micros), and
        a pct-based ceiling is divide(equity_micros * pct_ppm, 1_000_000,
        UNDERSTATE_EQUITY).

Money/price arithmetic on the sizing seam itself is on
`scripts/lint_no_floats.py`'s denylist: no float, no bare `/`/`//` -- every
division `windbreak.selector.sizing` performs routes through
`windbreak.numeric.divide` with an explicit `RoundingDirection`; this test
module only reproduces that same fused-division arithmetic in comments, by
hand, to pin the expected results.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from windbreak.config.schema import RiskConfig
from windbreak.connector.fees import FeeModel
from windbreak.connector.models import OrderBookLevel, OrderBookSnapshot
from windbreak.forecast.records import Citation, ForecastRecord
from windbreak.ledger.events import canonical_json
from windbreak.numeric import ContractCentis, MoneyMicros, PricePips
from windbreak.selector import SelectorInputs, select
from windbreak.selector.sizing import (
    CapClipResult,
    clip_to_caps,
    dispersion_scale,
    kelly_size,
)
from windbreak.selector.types import (
    FeeModelInput,
    PositionReadModelInput,
    RiskConfigInput,
    SlippageModelInput,
)

#: A fixed reference instant every timestamp in this module is pinned to.
_INSTANT = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

_CITATION = Citation(
    url="https://example.com/sizing-example",
    content_hash="sha256:sizing-example-citation",
    quoted_text="Example quoted text supporting the sizing-example forecast.",
    publication_date=None,
    source_type="news_article",
)


def _generous_positions(**overrides: object) -> PositionReadModelInput:
    """Build a `PositionReadModelInput` sized so none of the five notional
    caps bind by default: huge equity/deploy-cap, zero exposures/notional.

    Args:
        **overrides: Field values overriding the generous defaults below.

    Returns:
        The constructed `PositionReadModelInput`.
    """
    defaults: dict[str, object] = {
        "snapshot_id": "positions-sizing-example",
        "equity_micros": MoneyMicros(1_000_000_000_000),
        "above_floor_capital_micros": MoneyMicros(1_000_000_000),
        "total_deploy_cap_micros": MoneyMicros(1_000_000_000_000),
        "market_exposure": MoneyMicros(0),
        "event_exposure": MoneyMicros(0),
        "bucket_exposure": MoneyMicros(0),
        "total_exposure": MoneyMicros(0),
        "notional_today": MoneyMicros(0),
    }
    defaults.update(overrides)
    return PositionReadModelInput(**defaults)


def _book(levels: tuple[tuple[int, int], ...]) -> OrderBookSnapshot:
    """Build an `OrderBookSnapshot` from `(price_pips, quantity_centis)` pairs.

    Args:
        levels: Best-first `(price_pips, quantity_centis)` pairs.

    Returns:
        The constructed `OrderBookSnapshot`.
    """
    return OrderBookSnapshot(
        ticker="SIZING-TICKER",
        yes_bids=(),
        yes_asks=tuple(
            OrderBookLevel(price=PricePips(p), quantity=ContractCentis(q))
            for p, q in levels
        ),
        fetched_at=_INSTANT,
    )


# --- dispersion_scale: hand-computed examples --------------------------------


def test_dispersion_scale_at_zero_dispersion_is_full_scale() -> None:
    """`g(0, ceiling)` is exactly `1_000_000` for any positive ceiling: zero
    dispersion never discounts the Kelly stake.
    """
    assert dispersion_scale(0, 200_000) == 1_000_000


def test_dispersion_scale_at_the_ceiling_is_zero() -> None:
    """`g(ceiling, ceiling)` is exactly `0`: dispersion at (or past) the
    configured ceiling fully zeros the sizing scale.
    """
    assert dispersion_scale(200_000, 200_000) == 0


def test_dispersion_scale_halfway_to_the_ceiling_halves_the_scale() -> None:
    """`g(ceiling/2, ceiling)` is exactly `500_000`.

    g(100_000, 200_000) = divide((200_000-100_000)*1_000_000, 200_000, floor)
                         = divide(100_000_000_000, 200_000, floor) = 500_000
    (100_000_000_000 / 200_000 = 500_000 exactly, no remainder).
    """
    assert dispersion_scale(100_000, 200_000) == 500_000


def test_dispersion_scale_negative_dispersion_is_treated_as_zero() -> None:
    """A negative `vote_dispersion_ppm` (never produced by a valid forecast,
    but defensively handled) is treated as zero dispersion: full scale.
    """
    assert dispersion_scale(-1, 200_000) == 1_000_000


def test_dispersion_scale_non_positive_ceiling_and_zero_dispersion_is_full_scale() -> (
    None
):
    """`ceiling <= 0` with `d <= 0` (here `d == 0`) is the degenerate
    "no discounting configured, no dispersion" case: full scale.
    """
    assert dispersion_scale(0, 0) == 1_000_000


def test_dispersion_scale_non_positive_ceiling_and_positive_dispersion_is_zero() -> (
    None
):
    """`ceiling <= 0` with `d > 0` is degenerate the other way: any
    dispersion at all fully zeros the scale, since there is no positive
    ceiling to ramp against.
    """
    assert dispersion_scale(1, 0) == 0


# --- kelly_size: the architect's own worked example --------------------------


def test_kelly_size_worked_example_matches_the_architects_hand_computation() -> None:
    """capital=1_000_000_000, net_edge=50_000, price=450_000, kelly=100_000,
    g=1_000_000.

    stake_micros = divide(1_000_000_000 * 50_000 * 100_000 * 1_000_000,
                           (1_000_000-450_000) * 10**12, floor)
                 = divide(5_000_000_000_000_000_000_000_000,
                          550_000_000_000_000_000, floor)
                 = floor(50_000_000/5.5) = floor(9_090_909.0909...) = 9_090_909
    (denominator*9_090_909 = 550_000_000_000_000_000*9_090_909
     = 4_999_999_950_000_000_000_000_000; remainder
     = 5_000_000_000_000_000_000_000_000 - 4_999_999_950_000_000_000_000_000
     = 50_000_000_000_000_000 < denominator, confirming the floor).

    size_centis = divide(9_090_909*100, 450_000, floor)
                = divide(909_090_900, 450_000, floor)
    450_000*2_020 = 909_000_000; remainder = 909_090_900-909_000_000 = 90_900
    (< 450_000) -> size_centis = 2_020.
    """
    result = kelly_size(
        net_edge_ppm=50_000,
        min_net_edge_ppm=30_000,
        executable_price_ppm=450_000,
        kelly_fraction_ppm=100_000,
        dispersion_scale_ppm=1_000_000,
        above_floor_capital_micros=MoneyMicros(1_000_000_000),
    )

    assert result == ContractCentis(2_020)


def test_kelly_size_is_exactly_zero_below_the_net_edge_floor() -> None:
    """`net_edge_ppm` one ppm below `min_net_edge_ppm` yields exactly
    `ContractCentis(0)` -- guards a degenerate implementation that instead
    returns a tiny-but-nonzero stake near the threshold.
    """
    result = kelly_size(
        net_edge_ppm=29_999,
        min_net_edge_ppm=30_000,
        executable_price_ppm=450_000,
        kelly_fraction_ppm=100_000,
        dispersion_scale_ppm=1_000_000,
        above_floor_capital_micros=MoneyMicros(1_000_000_000),
    )

    assert result == ContractCentis(0)


def test_kelly_size_is_exactly_zero_at_a_zero_executable_price() -> None:
    """`executable_price_ppm <= 0` yields exactly `ContractCentis(0)` -- a
    zero-pip fill has no well-defined Kelly fraction (`f* = edge/(1-P)` is
    fine at P=0, but a non-positive/degenerate price is guarded explicitly
    per the architect's plan).
    """
    result = kelly_size(
        net_edge_ppm=50_000,
        min_net_edge_ppm=30_000,
        executable_price_ppm=0,
        kelly_fraction_ppm=100_000,
        dispersion_scale_ppm=1_000_000,
        above_floor_capital_micros=MoneyMicros(1_000_000_000),
    )

    assert result == ContractCentis(0)


def test_kelly_size_is_exactly_zero_at_a_full_dollar_executable_price() -> None:
    """`executable_price_ppm >= 1_000_000` yields exactly `ContractCentis(0)`
    -- a full-dollar fill leaves no Kelly denominator (`1 - P == 0`).
    """
    result = kelly_size(
        net_edge_ppm=50_000,
        min_net_edge_ppm=30_000,
        executable_price_ppm=1_000_000,
        kelly_fraction_ppm=100_000,
        dispersion_scale_ppm=1_000_000,
        above_floor_capital_micros=MoneyMicros(1_000_000_000),
    )

    assert result == ContractCentis(0)


# --- clip_to_caps: one example per cap as the UNIQUE binder ------------------


def test_clip_to_caps_per_market_headroom_uniquely_binds() -> None:
    """equity=$1,000, `max_pos_market_pct_ppm`=20_000 (2%) -> ceiling
    $20; `market_exposure`=$15 -> headroom $5.

    cap_size_centis = divide(5_000_000*100, 450_000, floor)
                    = divide(500_000_000, 450_000, floor)
    450_000*1_111 = 499_950_000; remainder = 50_000 (< 450_000) -> 1_111.

    Every other cap is generous (event/bucket ceilings off the same equity,
    zero exposure; total_deploy/daily headroom left at their huge/default
    size; a single, deep ask level makes participation loose), so 1_111 is
    the unique binder: `raw=5_000` clips to 1_111, then floors to the
    nearest 100 -> 1_100, with `binding_cap="per_market"`.
    """
    positions = _generous_positions(
        equity_micros=MoneyMicros(1_000_000_000),
        market_exposure=MoneyMicros(15_000_000),
    )
    risk_config = RiskConfig()
    order_book = _book(((4_500, 1_000_000),))

    result = clip_to_caps(
        ContractCentis(5_000),
        executable_price_ppm=450_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(size=ContractCentis(1_100), binding_cap="per_market")


def test_clip_to_caps_per_event_headroom_uniquely_binds() -> None:
    """equity=$1,000, `max_pos_event_pct_ppm`=40_000 (4%) -> ceiling $40;
    `event_exposure`=$39 -> headroom $1.

    cap_size_centis = divide(1_000_000*100, 450_000, floor)
                    = divide(100_000_000, 450_000, floor)
    450_000*222 = 99_900_000; remainder = 100_000 (< 450_000) -> 222.

    per_market headroom is untouched ($20, cap_size 4_444) and every other
    cap is generous, so 222 is the unique binder: `raw=5_000` clips to 222,
    floors to 200, `binding_cap="per_event"`.
    """
    positions = _generous_positions(
        equity_micros=MoneyMicros(1_000_000_000),
        event_exposure=MoneyMicros(39_000_000),
    )
    risk_config = RiskConfig()
    order_book = _book(((4_500, 1_000_000),))

    result = clip_to_caps(
        ContractCentis(5_000),
        executable_price_ppm=450_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(size=ContractCentis(200), binding_cap="per_event")


def test_clip_to_caps_per_bucket_headroom_uniquely_binds() -> None:
    """equity=$1,000, `max_pos_bucket_pct_ppm`=100_000 (10%) -> ceiling $100;
    `bucket_exposure`=$99 -> headroom $1 -> cap_size_centis=222 (identical
    fused division to the per_event case above), floors to 200,
    `binding_cap="per_bucket"`.
    """
    positions = _generous_positions(
        equity_micros=MoneyMicros(1_000_000_000),
        bucket_exposure=MoneyMicros(99_000_000),
    )
    risk_config = RiskConfig()
    order_book = _book(((4_500, 1_000_000),))

    result = clip_to_caps(
        ContractCentis(5_000),
        executable_price_ppm=450_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(size=ContractCentis(200), binding_cap="per_bucket")


def test_clip_to_caps_total_deployed_headroom_uniquely_binds() -> None:
    """`total_deploy_cap_micros`=$21, `total_exposure`=$20 -> headroom $1 ->
    cap_size_centis=222 (same fused division), floors to 200,
    `binding_cap="total_deployed"`. Equity is left huge so the three pct
    caps stay generous.
    """
    positions = _generous_positions(
        total_deploy_cap_micros=MoneyMicros(21_000_000),
        total_exposure=MoneyMicros(20_000_000),
    )
    risk_config = RiskConfig()
    order_book = _book(((4_500, 1_000_000),))

    result = clip_to_caps(
        ContractCentis(5_000),
        executable_price_ppm=450_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(
        size=ContractCentis(200), binding_cap="total_deployed"
    )


def test_clip_to_caps_daily_notional_headroom_uniquely_binds() -> None:
    """`max_notional_per_day_micros`=$21 (overriding the $500 default),
    `notional_today`=$20 -> headroom $1 -> cap_size_centis=222 (same fused
    division), floors to 200, `binding_cap="daily_notional"`.
    """
    positions = _generous_positions(notional_today=MoneyMicros(20_000_000))
    risk_config = RiskConfig(max_notional_per_day_micros=21_000_000)
    order_book = _book(((4_500, 1_000_000),))

    result = clip_to_caps(
        ContractCentis(5_000),
        executable_price_ppm=450_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(
        size=ContractCentis(200), binding_cap="daily_notional"
    )


def test_clip_to_caps_participation_fixed_point_uniquely_binds() -> None:
    """Two 10_000-centi levels (D1 then D2), `max_participation_ppm=250_000`
    (25%), `raw=8_000` -- above the clamp.

    Fixed-point walk:
      S0 = min(8_000, floor(250_000*20_000/1_000_000)) = min(8_000, 5_000) = 5_000
      A fill of 5_000 stays within level 1 (10_000 deep), so its marginal
      price is level 1's own price and the "at-or-better" depth is level 1's
      10_000 alone (level 2's price is strictly worse for a YES buy).
      cap = floor(250_000*10_000/1_000_000) = 2_500
      S0(5_000) > cap(2_500) -> S1 = 2_500
      A fill of 2_500 is still within level 1, so the marginal/at-or-better
      depth is unchanged (10_000) -> cap = 2_500 again; S1(2_500) <= cap ->
      fixed point reached at 2_500.

    2_500 is already an exact multiple of 100, so the final floor-to-100
    quantization is a no-op: `binding_cap="participation"`,
    `size=ContractCentis(2_500)`.
    """
    positions = _generous_positions()
    risk_config = RiskConfig(max_participation_ppm=250_000)
    order_book = _book(((4_000, 10_000), (4_200, 10_000)))

    result = clip_to_caps(
        ContractCentis(8_000),
        executable_price_ppm=500_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(
        size=ContractCentis(2_500), binding_cap="participation"
    )


def test_clip_to_caps_exchange_min_order_zeros_a_sub_lot_raw_size() -> None:
    """`raw=50` (half a contract) survives every notional/participation cap
    untouched (all generous, none reduce it below 50), but the final
    floor-to-100-centis quantization takes 50 down to 0 -- `size=
    ContractCentis(0)`, `binding_cap="exchange_min_order"`, distinguishing
    this path from `binding_cap=None` (which applies when the survivor is
    still >= 100 after flooring).
    """
    positions = _generous_positions()
    risk_config = RiskConfig()
    order_book = _book(((4_500, 1_000_000),))

    result = clip_to_caps(
        ContractCentis(50),
        executable_price_ppm=450_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(
        size=ContractCentis(0), binding_cap="exchange_min_order"
    )


def test_clip_to_caps_returns_none_binding_cap_when_raw_survives_unclipped() -> None:
    """A raw size no cap needs to touch (2_020, matching the worked Kelly
    example) survives with `binding_cap=None`; only the routine floor-to-100
    quantization applies (2_020 -> 2_000), which is not itself a "binding
    cap" (see the exchange-min test above for the distinct zeroing case).
    """
    positions = _generous_positions()
    risk_config = RiskConfig()
    order_book = _book(((4_500, 1_000_000),))

    result = clip_to_caps(
        ContractCentis(2_020),
        executable_price_ppm=450_000,
        order_book=order_book,
        risk_config=risk_config,
        positions=positions,
    )

    assert result == CapClipResult(size=ContractCentis(2_000), binding_cap=None)


# --- select()-level: the pinned sizing reason and a sized-to-zero decline ----


def _sizing_forecast(**overrides: object) -> ForecastRecord:
    """Build the sizing-example `ForecastRecord`: probability 500_000 ppm,
    zero research cost, all twelve SPEC S9.3 entry conditions passing.

    Args:
        **overrides: Field values overriding the defaults below.

    Returns:
        The constructed, post-init-validated `ForecastRecord`.
    """
    defaults: dict[str, object] = {
        "forecast_id": "fc-sizing-0001",
        "market_ticker": "SIZING-TICKER",
        "normalized_question_hash": "sha256:sizing-question",
        "probability_ppm": 500_000,
        "ci_low_ppm": 100_000,
        "ci_high_ppm": 200_000,
        "model_votes": (),
        "vote_dispersion_ppm": 0,
        "rationale_markdown": "n/a",
        "citations": (_CITATION,),
        "source_quality_notes": (),
        "research_cost_micros": 0,
        "triage_stage": "full",
        "created_at": _INSTANT,
        "forecast_horizon_hours": 48,
        "market_price_baseline_pips": 4_500,
        "baseline_quote_snapshot_id": "snap-sizing-0001",
        "coherence_group_sum_ppm": None,
        "coherence_flag": False,
        "abstention_reason": None,
        "eligible_for_live": True,
    }
    defaults.update(overrides)
    return ForecastRecord(**defaults)


def _sizing_inputs(*, above_floor_capital_micros: int) -> SelectorInputs:
    """Assemble the sizing-example `SelectorInputs`: a single deep 4_500-pip
    ask level, zero fee/slippage, and a `PositionReadModelInput` carrying the
    given capital -- everything else generous so only Kelly sizing (never a
    cap) can determine the raw candidate.

    Args:
        above_floor_capital_micros: The capital Kelly sizes against, in
            micros.

    Returns:
        The constructed `SelectorInputs`.
    """
    return SelectorInputs(
        forecast=_sizing_forecast(),
        calibration_map_version="calib-sizing-v1",
        order_book=_book(((4_500, 1_000_000),)),
        fee_model=FeeModelInput(
            model=FeeModel(
                schedule_id="sizing-fee-zero",
                maker_fee_ppm=0,
                taker_fee_ppm=0,
                settlement_fee_ppm=0,
            ),
            as_of=_INSTANT,
        ),
        slippage_model=SlippageModelInput(
            model_id="sizing-slippage-zero", per_contract_buffer_ppm=0
        ),
        positions=_generous_positions(
            above_floor_capital_micros=MoneyMicros(above_floor_capital_micros)
        ),
        risk_config=RiskConfigInput(
            config=RiskConfig(), config_hash="sha256:risk-sizing"
        ),
        correlation_tags=(),
    )


def test_select_emits_the_pinned_sizing_reason_alongside_the_sized_intent() -> None:
    """On the worked-Kelly capital ($1,000,000,000), `select` emits one
    intent sized 2_000 (see the `kelly_size`/`clip_to_caps` worked-example
    tests above for the raw=2_020/g=1_000_000/binding_cap=None derivation)
    and appends the pinned sizing reason to `reasons`, after the twelve
    `pass:*` entries.
    """
    inputs = _sizing_inputs(above_floor_capital_micros=1_000_000_000)

    decision = select(inputs)

    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.size == ContractCentis(2_000)
    assert intent.intent_id == "fc-sizing-0001:yes:buy:sized"

    assert decision.reasons[:12] == (
        "pass:net_edge_min",
        "pass:annualized_hurdle",
        "pass:ci_straddles_executable_price",
        "pass:quote_snapshot_fresh",
        "pass:forecast_fresh",
        "pass:fee_model_current",
        "pass:market_coherent",
        "pass:citation_support",
        "pass:jurisdiction_eligible",
        "pass:category_eligible",
        "pass:price_within_bands",
        "pass:forecast_live_eligible",
    )
    assert decision.reasons[12] == (
        "sizing: raw_centis=2020 g_ppm=1000000 binding_cap=none final_centis=2000"
    )
    assert len(decision.reasons) == 13


def test_select_declines_with_no_intent_when_sizing_zeros_to_the_exchange_min() -> None:
    """A tiny capital ($1) drives the Kelly raw candidate down to 2 centis --
    well under one contract -- so `select` declines: no intent, but the
    reasons stay non-empty (the twelve `pass:*` entries plus the pinned
    sizing reason naming the zeroed final size).

    stake_micros = divide(1_000_000*50_000*100_000*1_000_000,
                           550_000*10**12, floor)
                 = divide(5_000_000_000_000_000_000_000,
                          550_000_000_000_000_000, floor)
                 = floor(5_000_000_000_000_000_000_000 / 550_000_000_000_000_000)
                 = floor(9_090.909...) = 9_090
    size_centis  = divide(9_090*100, 450_000, floor) = divide(909_000, 450_000, floor)
                 450_000*2 = 900_000; remainder = 9_000 (< 450_000) -> 2
    2 centis floors to 0 -> `binding_cap="exchange_min_order"`.
    """
    inputs = _sizing_inputs(above_floor_capital_micros=1_000_000)

    decision = select(inputs)

    assert decision.intents == ()
    assert decision.reasons
    assert decision.reasons[-1] == (
        "sizing: raw_centis=2 g_ppm=1000000 binding_cap=exchange_min_order "
        "final_centis=0"
    )


def _idempotency_key_fields(
    forecast_id: str, market_ticker: str, price_pips: int, size_centis: int
) -> dict[str, object]:
    """Build the six named fields `select`'s idempotency key hashes over.

    Mirrors `windbreak.selector._idempotency_key` verbatim so this module can
    hand-derive the expected key without calling `select` to produce it.

    Args:
        forecast_id: The originating forecast's id.
        market_ticker: The market the intent targets.
        price_pips: The intent's price, in pips.
        size_centis: The intent's size, in contract-centis.

    Returns:
        The six-field mapping `canonical_json` hashes.
    """
    return {
        "forecast_id": forecast_id,
        "market_ticker": market_ticker,
        "outcome": "yes",
        "action": "buy",
        "price": price_pips,
        "size": size_centis,
    }


def test_select_sized_intent_fields_match_the_worked_kelly_example_exactly() -> None:
    """The sized intent's price/size/max_notional/idempotency_key match the
    worked-Kelly hand computation exactly.

    Re-walking the (single, flat 4_500-pip) book at the final size=2_000:
        cost = 4_500*2_000 = 9_000_000 micros (exact)
        executable_price_ppm = divide(9_000_000*100, 2_000, ceil)
                              = divide(900_000_000, 2_000, ceil) = 450_000 (exact)
        fee = 0 (zero-rate fee model); max_notional = 9_000_000 + 0 = 9_000_000
        net_edge_at_final_size = probability(500_000) - price(450_000) = 50_000
            >= min_net_edge_ppm(30_000) -> the final-size guard passes.
    """
    inputs = _sizing_inputs(above_floor_capital_micros=1_000_000_000)

    decision = select(inputs)

    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.price == PricePips(4_500)
    assert intent.size == ContractCentis(2_000)
    assert intent.max_notional == MoneyMicros(9_000_000)

    expected_key = hashlib.sha256(
        canonical_json(
            _idempotency_key_fields("fc-sizing-0001", "SIZING-TICKER", 4_500, 2_000)
        ).encode("utf-8")
    ).hexdigest()
    assert intent.idempotency_key == expected_key
