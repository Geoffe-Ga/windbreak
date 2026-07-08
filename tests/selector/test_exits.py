"""Gate 1 RED tests for `windbreak.selector.exits` (issue #46).

`windbreak/selector/exits.py` does not exist yet, so every test below fails
collection with `ModuleNotFoundError: No module named
'windbreak.selector.exits'` -- the expected Gate 1 RED state for issue #46's
close-side seam.

`CloseTrigger` is a closed, three-member enum (`KILL_PATH`, `KERNEL_DERISK`,
`OPERATOR_COMMAND`) and `build_close_intent(trigger, position, close_price,
size=None)` builds a reduce-only `SelectorOrderIntent` closing (at most) the
held position: the emitted size is `min(size or position.quantity,
position.quantity)`, never more than what is held, and a non-positive held
position or a non-positive requested size raises `ValueError` outright.
Every close is `execution_style="cross"` (both resting fields `None`),
`outcome="yes"`, `action="sell_to_close"`.

This module also carries the "no-close-from-select" proof required by the
architect's plan: `select()` -- the *open*-side entry point -- must never
itself construct a `"sell_to_close"` intent, behaviorally (every intent it
emits, across two recorded bundles and a wide-spread scenario, is a `"buy"`)
and structurally (the literal substring `"sell_to_close"` never appears in
the source of any module on `select()`'s call graph, and appears only in
`windbreak.selector.exits` -- the sole selector module that can construct a
close).
"""

from __future__ import annotations

import hashlib
import inspect
import typing
from datetime import UTC, datetime

import pytest

import windbreak.selector as selector_module
import windbreak.selector.edge as edge_module
import windbreak.selector.entry as entry_module
import windbreak.selector.execution_style as execution_style_module
import windbreak.selector.exits as exits_module
import windbreak.selector.serialization as serialization_module
import windbreak.selector.sizing as sizing_module
import windbreak.selector.types as types_module
from windbreak.config.schema import RiskConfig
from windbreak.connector.fees import FeeModel
from windbreak.connector.models import OrderBookLevel, OrderBookSnapshot, Position
from windbreak.forecast.records import Citation, ForecastRecord
from windbreak.ledger.events import canonical_json
from windbreak.numeric import ContractCentis, MoneyMicros, PricePips
from windbreak.selector import SelectorInputs, select
from windbreak.selector.exits import CloseTrigger, build_close_intent
from windbreak.selector.types import (
    FeeModelInput,
    PositionReadModelInput,
    RiskConfigInput,
    SlippageModelInput,
)

#: A fixed reference instant for the wide-spread `select()` scenario below.
_BASELINE_INSTANT = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

_ZERO_FEE_MODEL = FeeModel(
    schedule_id="exits-test-fee-zero",
    maker_fee_ppm=0,
    taker_fee_ppm=0,
    settlement_fee_ppm=0,
)

_DEEP_QTY = ContractCentis(1_000_000)

_BASELINE_CITATION = Citation(
    url="https://example.com/exits-test",
    content_hash="sha256:exits-test-citation",
    quoted_text="Example quoted text supporting the wide-spread forecast.",
    publication_date=None,
    source_type="news_article",
)


def _wide_spread_all_pass_inputs() -> SelectorInputs:
    """Build the same hand-verified, all-pass, wide-spread `SelectorInputs`
    exercised in `test_execution_style.py`'s own `select()` integration test
    (probability 500_000, bid 4_000/ask 4_500 -- spread 500, wide -- zero
    fee/slippage/research, generous positions), so `select()` emits exactly
    one real, Kelly-sized `"yes"`/`"buy"` intent here too -- a genuinely
    emitting scenario for the no-close-from-select proof below, complementing
    the two recorded bundles (one of which, bundle B, declines, so an
    "every intent is a buy" check over it alone would be only vacuously
    true).
    """
    forecast = ForecastRecord(
        forecast_id="fc-exits-0001",
        market_ticker="EXITS-TICKER",
        normalized_question_hash="sha256:exits-question",
        probability_ppm=500_000,
        ci_low_ppm=100_000,
        ci_high_ppm=200_000,
        model_votes=(),
        vote_dispersion_ppm=0,
        rationale_markdown="n/a",
        citations=(_BASELINE_CITATION,),
        source_quality_notes=(),
        research_cost_micros=0,
        triage_stage="full",
        created_at=_BASELINE_INSTANT,
        forecast_horizon_hours=48,
        market_price_baseline_pips=4_000,
        baseline_quote_snapshot_id="snap-exits-0001",
        coherence_group_sum_ppm=None,
        coherence_flag=False,
        abstention_reason=None,
        eligible_for_live=True,
    )
    order_book = OrderBookSnapshot(
        ticker="EXITS-TICKER",
        yes_bids=(OrderBookLevel(price=PricePips(4_000), quantity=_DEEP_QTY),),
        yes_asks=(OrderBookLevel(price=PricePips(4_500), quantity=_DEEP_QTY),),
        fetched_at=_BASELINE_INSTANT,
    )
    fee_model = FeeModelInput(model=_ZERO_FEE_MODEL, as_of=_BASELINE_INSTANT)
    slippage_model = SlippageModelInput(
        model_id="exits-test-slippage-zero", per_contract_buffer_ppm=0
    )
    positions = PositionReadModelInput(
        snapshot_id="positions-exits-0001",
        equity_micros=MoneyMicros(1_000_000_000_000),
        above_floor_capital_micros=MoneyMicros(1_000_000_000),
        total_deploy_cap_micros=MoneyMicros(1_000_000_000_000),
        market_exposure=MoneyMicros(0),
        event_exposure=MoneyMicros(0),
        bucket_exposure=MoneyMicros(0),
        total_exposure=MoneyMicros(0),
        notional_today=MoneyMicros(0),
    )
    risk_config = RiskConfigInput(config=RiskConfig(), config_hash="sha256:risk-exits")
    return SelectorInputs(
        forecast=forecast,
        calibration_map_version="calib-exits-v1",
        order_book=order_book,
        fee_model=fee_model,
        slippage_model=slippage_model,
        positions=positions,
        risk_config=risk_config,
        correlation_tags=(),
    )


def _position(**overrides: object) -> Position:
    """Build a `Position` with a 500-centi held quantity at 4_000 pips.

    Args:
        **overrides: Field values overriding the defaults below.

    Returns:
        The constructed `Position`.
    """
    defaults: dict[str, object] = {
        "ticker": "EXITS-TICKER",
        "quantity": ContractCentis(500),
        "average_price": PricePips(4_000),
    }
    defaults.update(overrides)
    return Position(**defaults)


# --- CloseTrigger: closed three-member enum ------------------------------------


def test_close_trigger_has_exactly_three_members_by_name_and_value() -> None:
    """`CloseTrigger` is a closed three-member enum -- no more, no fewer --
    naming the exact machine-readable value each member's `intent_id` suffix
    and idempotency key hash over.
    """
    names = {member.name for member in CloseTrigger}
    values = {member.value for member in CloseTrigger}

    assert names == {"KILL_PATH", "KERNEL_DERISK", "OPERATOR_COMMAND"}
    assert values == {"kill_path", "kernel_derisk", "operator_command"}
    assert len(CloseTrigger) == 3


# --- build_close_intent: required, typed `trigger` parameter -------------------


def test_build_close_intent_trigger_parameter_is_required_and_typed() -> None:
    """`trigger` has no default (every close must name why) and is annotated
    `CloseTrigger` -- resolved via `typing.get_type_hints` so this holds
    regardless of `from __future__ import annotations` postponed evaluation.
    """
    signature = inspect.signature(build_close_intent)
    trigger_param = signature.parameters["trigger"]

    assert trigger_param.default is inspect.Parameter.empty
    hints = typing.get_type_hints(build_close_intent)
    assert hints["trigger"] is CloseTrigger


# --- build_close_intent: reduce-only sizing -------------------------------------


def test_build_close_intent_defaults_to_closing_the_full_position() -> None:
    """`size=None` closes the entire held position."""
    position = _position(quantity=ContractCentis(500))

    intent = build_close_intent(CloseTrigger.KILL_PATH, position, PricePips(4_200))

    assert intent.size == ContractCentis(500)


def test_build_close_intent_clips_a_requested_size_above_the_position() -> None:
    """A requested close larger than the held position clips to the position
    -- reduce-only, never net-short.
    """
    position = _position(quantity=ContractCentis(500))

    intent = build_close_intent(
        CloseTrigger.KILL_PATH, position, PricePips(4_200), size=ContractCentis(700)
    )

    assert intent.size == ContractCentis(500)


def test_build_close_intent_passes_through_a_requested_size_equal_to_the_position() -> (
    None
):
    """A requested close exactly equal to the held position passes through
    unclipped."""
    position = _position(quantity=ContractCentis(500))

    intent = build_close_intent(
        CloseTrigger.KILL_PATH, position, PricePips(4_200), size=ContractCentis(500)
    )

    assert intent.size == ContractCentis(500)


def test_build_close_intent_passes_through_a_requested_size_below_the_position() -> (
    None
):
    """A partial close smaller than the held position passes through
    unclipped."""
    position = _position(quantity=ContractCentis(500))

    intent = build_close_intent(
        CloseTrigger.KILL_PATH, position, PricePips(4_200), size=ContractCentis(300)
    )

    assert intent.size == ContractCentis(300)


def test_build_close_intent_rejects_a_non_positive_position_quantity() -> None:
    """A non-positive held position cannot be closed -- there is nothing (or
    a phantom negative) to reduce."""
    position = _position(quantity=ContractCentis(0))

    with pytest.raises(ValueError):
        build_close_intent(CloseTrigger.KILL_PATH, position, PricePips(4_200))


def test_build_close_intent_rejects_a_zero_requested_size() -> None:
    """A zero requested close size is rejected outright, distinct from the
    reduce-only clip (which only ever shrinks a too-large request)."""
    position = _position(quantity=ContractCentis(500))

    with pytest.raises(ValueError):
        build_close_intent(
            CloseTrigger.KILL_PATH,
            position,
            PricePips(4_200),
            size=ContractCentis(0),
        )


def test_build_close_intent_rejects_a_negative_requested_size() -> None:
    """A negative requested close size is likewise rejected."""
    position = _position(quantity=ContractCentis(500))

    with pytest.raises(ValueError):
        build_close_intent(
            CloseTrigger.KILL_PATH,
            position,
            PricePips(4_200),
            size=ContractCentis(-100),
        )


# --- build_close_intent: emitted shape ------------------------------------------


def test_build_close_intent_emits_the_pinned_reduce_only_shape() -> None:
    """The emitted close always carries the same shape regardless of
    trigger: `"yes"` outcome, `"sell_to_close"` action, `cross` execution
    style with both resting fields `None`, and a notional/probability
    derived from the close price and the (possibly clipped) emitted size.
    """
    position = _position(quantity=ContractCentis(500))
    close_price = PricePips(4_200)

    intent = build_close_intent(CloseTrigger.KERNEL_DERISK, position, close_price)

    assert intent.outcome == "yes"
    assert intent.action == "sell_to_close"
    assert intent.execution_style == "cross"
    assert intent.resting_ttl_seconds is None
    assert intent.cancel_on_move_ticks is None
    assert intent.price == close_price
    assert intent.size == ContractCentis(500)
    assert intent.max_notional == MoneyMicros(4_200 * 500)
    assert intent.implied_probability.value == 4_200 * 100
    assert intent.intent_id == "EXITS-TICKER:yes:sell_to_close:kernel_derisk"


def test_build_close_intent_idempotency_key_is_deterministic_and_trigger_scoped() -> (
    None
):
    """Two calls with identical arguments agree on the idempotency key, and
    the key -- like the intent id -- changes when only the trigger differs,
    hashed via the same `hashlib.sha256(canonical_json(...))` primitive
    `windbreak.order_gateway.client_order_id` and
    `windbreak.selector.__init__._idempotency_key` already use elsewhere in
    this repo, applied here to the six named fields `{trigger, market_ticker,
    outcome, action, price, size}` (`trigger` hashed as its string `.value`,
    since a bare `CloseTrigger` member is not itself JSON-serializable).
    """
    position = _position(quantity=ContractCentis(500))
    close_price = PricePips(4_200)

    first = build_close_intent(CloseTrigger.KILL_PATH, position, close_price)
    second = build_close_intent(CloseTrigger.KILL_PATH, position, close_price)
    third = build_close_intent(CloseTrigger.KERNEL_DERISK, position, close_price)

    assert first.idempotency_key == second.idempotency_key
    assert first.intent_id != third.intent_id
    assert first.idempotency_key != third.idempotency_key

    expected_key_fields: dict[str, object] = {
        "trigger": CloseTrigger.KILL_PATH.value,
        "market_ticker": position.ticker,
        "outcome": "yes",
        "action": "sell_to_close",
        "price": close_price.value,
        "size": position.quantity.value,
    }
    expected_key = hashlib.sha256(
        canonical_json(expected_key_fields).encode("utf-8")
    ).hexdigest()
    assert first.idempotency_key == expected_key


# --- No-close-from-select proof --------------------------------------------------


def test_select_never_emits_a_sell_to_close_action_across_scenarios(
    recorded_inputs_bundle_a: SelectorInputs,
    recorded_inputs_bundle_b: SelectorInputs,
) -> None:
    """Every intent `select()` emits -- across the two recorded bundles (one
    emits, one declines) and a wide-spread scenario that rests rather than
    crosses -- is a `"buy"`, never a `"sell_to_close"`: `select` only ever
    opens, closes belong exclusively to `windbreak.selector.exits`.
    """
    scenarios = (
        recorded_inputs_bundle_a,
        recorded_inputs_bundle_b,
        _wide_spread_all_pass_inputs(),
    )

    for inputs in scenarios:
        decision = select(inputs)
        assert all(intent.action == "buy" for intent in decision.intents)

    # Guard against the check above being vacuously true for every scenario:
    # at least two of the three genuinely emit an intent to check.
    assert len(select(recorded_inputs_bundle_a).intents) == 1
    assert len(select(_wide_spread_all_pass_inputs()).intents) == 1


def test_close_construction_is_confined_to_the_exits_module() -> None:
    """Structural proof complementing the behavioral one above:
    `"sell_to_close"` never appears in the source of any module on
    `select()`'s call graph, and appears only in `windbreak.selector.exits`
    -- the sole selector module that can construct a close.
    """
    non_close_modules = (
        selector_module,
        entry_module,
        edge_module,
        sizing_module,
        execution_style_module,
        serialization_module,
        types_module,
    )
    for module in non_close_modules:
        source = inspect.getsource(module)
        assert "sell_to_close" not in source, (
            f"{module.__name__} must never construct a close"
        )

    assert "sell_to_close" in inspect.getsource(exits_module)
