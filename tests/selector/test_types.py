"""Tests for windbreak.selector's core types (issues #43/#44/#45).

Pins the frozen/slots invariants of `SelectorInputs`, `SelectorDecision`, the
issue-#44 concrete seam carriers (`FeeModelInput`, `SlippageModelInput`,
`RiskConfigInput`) and the issue-#45 `PositionReadModelInput` (which replaces
the issue-#43 opaque `PositionReadModelRef` placeholder now that
concentration/sizing arithmetic reads its nine fields), the
`NormalizedOrderIntent` type alias identity with
`windbreak.riskkernel.checks.OrderIntent`, and that `fixture_loader.load_inputs`
round-trips both committed bundles into real, post-init-validated
`ForecastRecord` / `OrderBookSnapshot` instances.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from windbreak.config.schema import RiskConfig
from windbreak.connector.fees import FeeModel
from windbreak.connector.models import OrderBookSnapshot
from windbreak.forecast.records import ForecastRecord
from windbreak.numeric import ContractCentis, MoneyMicros, PricePips, ProbabilityPpm
from windbreak.riskkernel.checks import OrderIntent
from windbreak.selector import NormalizedOrderIntent, SelectorDecision
from windbreak.selector.types import (
    FeeModelInput,
    PositionReadModelInput,
    RiskConfigInput,
    SelectorOrderIntent,
    SlippageModelInput,
)

if TYPE_CHECKING:
    from windbreak.selector import SelectorInputs

#: A fixed reference instant for the fee-model carrier's `as_of` stamp.
_AS_OF = datetime(2025, 1, 1, tzinfo=UTC)

#: A distinct second instant, for the frozen-immutability mutation attempt.
_OTHER_AS_OF = datetime(2025, 2, 1, tzinfo=UTC)


def _fee_model_input() -> FeeModelInput:
    """Build a `FeeModelInput` over a real, post-init-validated `FeeModel`."""
    model = FeeModel(
        schedule_id="fee-model-standard",
        maker_fee_ppm=0,
        taker_fee_ppm=10_000,
        settlement_fee_ppm=0,
    )
    return FeeModelInput(model=model, as_of=_AS_OF)


def _slippage_model_input() -> SlippageModelInput:
    """Build a `SlippageModelInput` with a known buffer."""
    return SlippageModelInput(
        model_id="slippage-model-linear", per_contract_buffer_ppm=2_000
    )


def _risk_config_input() -> RiskConfigInput:
    """Build a `RiskConfigInput` over unmodified `RiskConfig` defaults."""
    return RiskConfigInput(config=RiskConfig(), config_hash="sha256:risk-config-a")


def _position_read_model_input() -> PositionReadModelInput:
    """Build a `PositionReadModelInput` with nine distinct, hand-legible values."""
    return PositionReadModelInput(
        snapshot_id="positions-snap-0001",
        equity_micros=MoneyMicros(500_000_000_000),
        above_floor_capital_micros=MoneyMicros(100_000_000),
        total_deploy_cap_micros=MoneyMicros(400_000_000_000),
        market_exposure=MoneyMicros(1_000_000),
        event_exposure=MoneyMicros(2_000_000),
        bucket_exposure=MoneyMicros(3_000_000),
        total_exposure=MoneyMicros(4_000_000),
        notional_today=MoneyMicros(5_000_000),
    )


#: A stub decision's fields, factored out so every `SelectorDecision`-only
#: test builds an identical, minimal-but-valid instance.
_STUB_DECISION_KWARGS: dict[str, object] = {
    "intents": (),
    "reasons": ("stub: selection logic not yet implemented",),
    "forecast_id": "fc-0001",
    "market_ticker": "KXFED-24DEC",
    "calibration_map_version": "calib-v1",
}


def _stub_decision() -> SelectorDecision:
    return SelectorDecision(**_STUB_DECISION_KWARGS)


# --- NormalizedOrderIntent: TypeAlias identity ------------------------------


def test_normalized_order_intent_is_the_selector_order_intent() -> None:
    """`NormalizedOrderIntent` is a `TypeAlias` for `SelectorOrderIntent` --
    superseding this module's former `NormalizedOrderIntent is OrderIntent`
    pin (issues #43/#44) now that issue #46 enriches the emitted intent type
    with the three execution-style fields (`execution_style`,
    `resting_ttl_seconds`, `cancel_on_move_ticks`). `SelectorOrderIntent`
    itself remains a genuine subclass of the risk-kernel's `OrderIntent`, so
    a selector-built intent is still directly accepted by the Risk Kernel
    (whose checks read only the base `OrderIntent` fields, which every
    `SelectorOrderIntent` still carries) -- the same #44-over-#43-style
    supersession this package already applies elsewhere (e.g. the
    sizing-shaped golden superseding the stub-shaped one).
    """
    assert NormalizedOrderIntent is SelectorOrderIntent
    assert issubclass(SelectorOrderIntent, OrderIntent)


# --- SelectorInputs: frozen/slots and loader round-trip ---------------------


def test_selector_inputs_is_frozen(recorded_inputs_bundle_a: SelectorInputs) -> None:
    """Assigning to any `SelectorInputs` field raises `FrozenInstanceError`."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        recorded_inputs_bundle_a.calibration_map_version = "other"


def test_selector_inputs_is_slotted(recorded_inputs_bundle_a: SelectorInputs) -> None:
    """`SelectorInputs` is slotted: no `__dict__`, no stray attribute storage."""
    assert not hasattr(recorded_inputs_bundle_a, "__dict__")


def test_selector_inputs_correlation_tags_is_a_tuple(
    recorded_inputs_bundle_a: SelectorInputs,
) -> None:
    """`correlation_tags` is a tuple, not a list -- immutable by construction."""
    assert isinstance(recorded_inputs_bundle_a.correlation_tags, tuple)


def test_loader_round_trip_builds_valid_forecast_record_and_order_book(
    recorded_inputs_bundle_a: SelectorInputs,
    recorded_inputs_bundle_b: SelectorInputs,
) -> None:
    """`fixture_loader.load_inputs` builds a real, post-init-validated
    `ForecastRecord` and `OrderBookSnapshot` from BOTH recorded bundles --
    not a stub or a mock -- proving the loader round-trips through the
    actual domain types' construction invariants rather than merely
    stashing raw JSON.
    """
    for inputs in (recorded_inputs_bundle_a, recorded_inputs_bundle_b):
        assert isinstance(inputs.forecast, ForecastRecord)
        assert isinstance(inputs.order_book, OrderBookSnapshot)


def test_loader_round_trip_distinguishes_the_two_bundles(
    recorded_inputs_bundle_a: SelectorInputs,
    recorded_inputs_bundle_b: SelectorInputs,
) -> None:
    """Bundle A and bundle B are genuinely distinct recorded inputs, not the
    same fixture loaded twice -- guards against a copy-paste fixture bug that
    would silently make the two-bundle determinism parametrization redundant.
    """
    assert recorded_inputs_bundle_a.forecast.forecast_id != (
        recorded_inputs_bundle_b.forecast.forecast_id
    )
    assert recorded_inputs_bundle_a.order_book.ticker != (
        recorded_inputs_bundle_b.order_book.ticker
    )


# --- SelectorDecision: frozen/slots ------------------------------------------


def test_selector_decision_is_frozen() -> None:
    """Assigning to any `SelectorDecision` field raises `FrozenInstanceError`."""
    decision = _stub_decision()

    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.forecast_id = "other"


def test_selector_decision_is_slotted() -> None:
    """`SelectorDecision` is slotted: no `__dict__`, no stray attribute storage."""
    decision = _stub_decision()

    assert not hasattr(decision, "__dict__")


def test_selector_decision_intents_and_reasons_are_tuples() -> None:
    """`intents` and `reasons` are tuples, not lists -- immutable by construction."""
    decision = _stub_decision()

    assert isinstance(decision.intents, tuple)
    assert isinstance(decision.reasons, tuple)


# --- Seam carrier types: frozen/slots ----------------------------------------


@pytest.mark.parametrize(
    ("instance", "field_name", "new_value"),
    [
        (_fee_model_input(), "as_of", _OTHER_AS_OF),
        (_slippage_model_input(), "per_contract_buffer_ppm", 9_999),
        (_risk_config_input(), "config_hash", "sha256:other"),
        (_position_read_model_input(), "equity_micros", MoneyMicros(999)),
    ],
)
def test_seam_carrier_is_frozen(
    instance: object, field_name: str, new_value: object
) -> None:
    """Each selector seam carrier is frozen: mutating a field raises."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field_name, new_value)


@pytest.mark.parametrize(
    "instance",
    [
        _fee_model_input(),
        _slippage_model_input(),
        _risk_config_input(),
        _position_read_model_input(),
    ],
)
def test_seam_carrier_is_slotted(instance: object) -> None:
    """Each selector seam carrier is slotted: no `__dict__` attribute storage."""
    assert not hasattr(instance, "__dict__")


def test_fee_model_input_carries_a_real_fee_model_and_as_of() -> None:
    """`FeeModelInput` wraps a real `FeeModel` and its `as_of` freshness stamp."""
    fee_model = _fee_model_input()

    assert isinstance(fee_model.model, FeeModel)
    assert fee_model.model.taker_fee_ppm == 10_000
    assert fee_model.as_of == _AS_OF


def test_slippage_model_input_preserves_its_fields() -> None:
    """`SlippageModelInput` stores its model id and per-contract buffer verbatim."""
    slippage = _slippage_model_input()

    assert slippage.model_id == "slippage-model-linear"
    assert slippage.per_contract_buffer_ppm == 2_000


def test_risk_config_input_carries_a_real_risk_config_and_hash() -> None:
    """`RiskConfigInput` wraps a real `RiskConfig` and its content hash."""
    risk_config = _risk_config_input()

    assert isinstance(risk_config.config, RiskConfig)
    assert risk_config.config.min_net_edge_ppm == RiskConfig().min_net_edge_ppm
    assert risk_config.config_hash == "sha256:risk-config-a"


def test_position_read_model_input_preserves_every_field_verbatim() -> None:
    """`PositionReadModelInput` stores its snapshot id and all eight
    money-valued capital/exposure fields verbatim, each still wrapped in
    `MoneyMicros` (never unwrapped to a bare int).
    """
    positions = _position_read_model_input()

    assert positions.snapshot_id == "positions-snap-0001"
    assert positions.equity_micros == MoneyMicros(500_000_000_000)
    assert positions.above_floor_capital_micros == MoneyMicros(100_000_000)
    assert positions.total_deploy_cap_micros == MoneyMicros(400_000_000_000)
    assert positions.market_exposure == MoneyMicros(1_000_000)
    assert positions.event_exposure == MoneyMicros(2_000_000)
    assert positions.bucket_exposure == MoneyMicros(3_000_000)
    assert positions.total_exposure == MoneyMicros(4_000_000)
    assert positions.notional_today == MoneyMicros(5_000_000)


# --- SelectorOrderIntent: execution-style __post_init__ invariants (#46) ----


def _selector_order_intent(**overrides: object) -> SelectorOrderIntent:
    """Build a `SelectorOrderIntent` with sensible base-field defaults.

    Args:
        **overrides: Field values overriding the defaults below, including
            the three issue-#46 execution-style fields.

    Returns:
        The constructed `SelectorOrderIntent`.
    """
    defaults: dict[str, object] = {
        "intent_id": "intent-selector-0001",
        "market_ticker": "KXFED-24DEC",
        "outcome": "yes",
        "action": "buy",
        "price": PricePips(4_500),
        "size": ContractCentis(100),
        "max_notional": MoneyMicros(450_000),
        "implied_probability": ProbabilityPpm(500_000),
        "idempotency_key": "idem-selector-0001",
    }
    defaults.update(overrides)
    return SelectorOrderIntent(**defaults)


def test_selector_order_intent_cross_defaults_construct_with_no_resting_fields() -> (
    None
):
    """The default `execution_style="cross"` constructs fine with both
    resting fields left at their `None` defaults."""
    intent = _selector_order_intent()

    assert intent.execution_style == "cross"
    assert intent.resting_ttl_seconds is None
    assert intent.cancel_on_move_ticks is None


def test_selector_order_intent_cross_rejects_a_non_none_resting_ttl() -> None:
    """`cross` requires both resting fields `None`; a non-`None`
    `resting_ttl_seconds` on a `cross` intent raises."""
    with pytest.raises(ValueError):
        _selector_order_intent(execution_style="cross", resting_ttl_seconds=900)


def test_selector_order_intent_cross_rejects_a_non_none_cancel_on_move_ticks() -> None:
    """The mirror of the ttl case above: a non-`None` `cancel_on_move_ticks`
    on a `cross` intent likewise raises."""
    with pytest.raises(ValueError):
        _selector_order_intent(execution_style="cross", cancel_on_move_ticks=2)


def test_selector_order_intent_rest_inside_spread_constructs_with_both_fields() -> None:
    """`rest_inside_spread` requires both resting fields non-`None`;
    supplying both constructs fine."""
    intent = _selector_order_intent(
        execution_style="rest_inside_spread",
        resting_ttl_seconds=900,
        cancel_on_move_ticks=2,
    )

    assert intent.execution_style == "rest_inside_spread"
    assert intent.resting_ttl_seconds == 900
    assert intent.cancel_on_move_ticks == 2


def test_selector_order_intent_rest_inside_spread_rejects_a_none_resting_ttl() -> None:
    """A `rest_inside_spread` intent missing its `resting_ttl_seconds`
    (`None`) raises, even though `cancel_on_move_ticks` is set."""
    with pytest.raises(ValueError):
        _selector_order_intent(
            execution_style="rest_inside_spread",
            resting_ttl_seconds=None,
            cancel_on_move_ticks=2,
        )


def test_selector_order_intent_rest_inside_spread_rejects_a_none_cancel_ticks() -> None:
    """The mirror of the ttl case above: a `rest_inside_spread` intent
    missing its `cancel_on_move_ticks` (`None`) likewise raises, even though
    `resting_ttl_seconds` is set."""
    with pytest.raises(ValueError):
        _selector_order_intent(
            execution_style="rest_inside_spread",
            resting_ttl_seconds=900,
            cancel_on_move_ticks=None,
        )
