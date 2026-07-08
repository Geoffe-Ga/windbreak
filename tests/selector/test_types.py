"""Tests for hedgekit.selector's core types (issue #43).

Pins the frozen/slots invariants of `SelectorInputs`, `SelectorDecision`, and
the four placeholder ref types (`FeeModelRef`, `SlippageModelRef`,
`PositionReadModelRef`, `RiskConfigRef`), the `NormalizedOrderIntent` type
alias identity with `hedgekit.riskkernel.checks.OrderIntent`, and that
`fixture_loader.load_inputs` round-trips both committed bundles into real,
post-init-validated `ForecastRecord` / `OrderBookSnapshot` instances.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from hedgekit.connector.models import OrderBookSnapshot
from hedgekit.forecast.records import ForecastRecord
from hedgekit.riskkernel.checks import OrderIntent
from hedgekit.selector import NormalizedOrderIntent, SelectorDecision
from hedgekit.selector.types import (
    FeeModelRef,
    PositionReadModelRef,
    RiskConfigRef,
    SlippageModelRef,
)

if TYPE_CHECKING:
    from hedgekit.selector import SelectorInputs

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


def test_normalized_order_intent_is_the_riskkernel_order_intent() -> None:
    """`NormalizedOrderIntent` is a `TypeAlias` for the real risk-kernel type,
    not a parallel redefinition -- so a selector-built intent is directly
    accepted by the Risk Kernel without translation.
    """
    assert NormalizedOrderIntent is OrderIntent


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


# --- Placeholder ref types: frozen/slots -------------------------------------


@pytest.mark.parametrize(
    ("ref_type", "field_name"),
    [
        (FeeModelRef, "model_id"),
        (SlippageModelRef, "model_id"),
        (PositionReadModelRef, "snapshot_id"),
        (RiskConfigRef, "config_hash"),
    ],
)
def test_placeholder_ref_is_frozen(ref_type: type, field_name: str) -> None:
    """Each placeholder ref type is frozen: mutating its sole field raises."""
    ref = ref_type("some-id")

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(ref, field_name, "other-id")


@pytest.mark.parametrize(
    "ref_type", [FeeModelRef, SlippageModelRef, PositionReadModelRef, RiskConfigRef]
)
def test_placeholder_ref_is_slotted(ref_type: type) -> None:
    """Each placeholder ref type is slotted: no `__dict__` attribute storage."""
    ref = ref_type("some-id")

    assert not hasattr(ref, "__dict__")


@pytest.mark.parametrize(
    ("ref_type", "field_name", "value"),
    [
        (FeeModelRef, "model_id", "fee-model-standard"),
        (SlippageModelRef, "model_id", "slippage-model-linear"),
        (PositionReadModelRef, "snapshot_id", "positions-snap-0001"),
        (RiskConfigRef, "config_hash", "sha256:risk-config-a"),
    ],
)
def test_placeholder_ref_preserves_its_single_field(
    ref_type: type, field_name: str, value: str
) -> None:
    """Each placeholder ref type stores and returns its single str field verbatim."""
    ref = ref_type(value)

    assert getattr(ref, field_name) == value
