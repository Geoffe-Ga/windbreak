"""Tests for hedgekit.selector.serialize_decision (issue #43).

Pins three properties of the canonical serializer independent of the stub's
empty-intents behavior: no `float` leaf ever appears in the serialized form
(SPEC S6.1's "no floats on the money path", enforced here at the JSON-output
boundary), a `NormalizedOrderIntent`'s scaled-integer unit fields serialize as
bare ints via their `.value` (exercising `_intent_to_payload` even while the
stub always returns zero intents, so that code path stays covered rather than
dead), and `SelectorDecision`'s serialized form carries no generated
timestamp field -- only its five declared, non-temporal fields.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hedgekit.numeric import ContractCentis, MoneyMicros, PricePips, ProbabilityPpm
from hedgekit.selector import SelectorDecision, select, serialize_decision
from hedgekit.selector.types import SelectorOrderIntent

if TYPE_CHECKING:
    from hedgekit.selector import SelectorInputs

#: The exact, closed set of keys `SelectorDecision` may ever serialize to --
#: none of them a timestamp, because `SelectorDecision` carries no datetime
#: field (per the architect's plan). A mutant that started stamping a
#: generated `created_at`/`decided_at` onto the payload would grow this set
#: and fail the assertion below.
_EXPECTED_DECISION_KEYS = frozenset(
    {"intents", "reasons", "forecast_id", "market_ticker", "calibration_map_version"}
)


def _assert_no_float_leaf(node: object) -> None:
    """Recursively assert that no `float` leaf exists anywhere under `node`.

    Args:
        node: A JSON-decoded value (dict, list, or scalar) to walk.

    Raises:
        AssertionError: If any leaf under `node` is a `float`. `bool` is
            explicitly exempt (a `bool` is an `int` subclass in Python, never
            a `float`).
    """
    if isinstance(node, dict):
        for value in node.values():
            _assert_no_float_leaf(value)
    elif isinstance(node, list):
        for item in node:
            _assert_no_float_leaf(item)
    else:
        assert type(node) is not float, f"float leaf found in payload: {node!r}"


def test_serialized_decision_contains_no_floats(
    recorded_inputs_bundle_a: SelectorInputs,
) -> None:
    """No leaf anywhere in a serialized decision is a `float`."""
    decision = select(recorded_inputs_bundle_a)

    payload = json.loads(serialize_decision(decision))

    _assert_no_float_leaf(payload)


def test_serialize_decision_encodes_intents_via_int_values() -> None:
    """A `SelectorOrderIntent`'s scaled-integer fields serialize as bare
    ints equal to each unit type's `.value` -- proving `_intent_to_payload`
    (or equivalent) is exercised, despite the stub's `select` always
    returning zero intents, so this path is covered rather than dead code.
    """
    intent = SelectorOrderIntent(
        intent_id="intent-0001",
        market_ticker="KXFED-24DEC",
        outcome="yes",
        action="buy",
        price=PricePips(4500),
        size=ContractCentis(100),
        max_notional=MoneyMicros(450_000),
        implied_probability=ProbabilityPpm(620_000),
        idempotency_key="idem-0001",
    )
    decision = SelectorDecision(
        intents=(intent,),
        reasons=("test: exercising the intent-encoding path",),
        forecast_id="fc-0001",
        market_ticker="KXFED-24DEC",
        calibration_map_version="calib-v1",
    )

    payload = json.loads(serialize_decision(decision))
    encoded_intent = payload["intents"][0]

    assert encoded_intent["price"] == 4500
    assert encoded_intent["size"] == 100
    assert encoded_intent["max_notional"] == 450_000
    assert encoded_intent["implied_probability"] == 620_000
    assert encoded_intent["intent_id"] == "intent-0001"


def test_serialize_decision_encodes_a_cross_intents_resting_fields_as_null() -> None:
    """A `cross` intent's three issue-#46 fields --
    `execution_style`/`resting_ttl_seconds`/`cancel_on_move_ticks` -- carry
    the resting pair as JSON `null` (the `cross` default), proving
    `_intent_to_payload` serializes these three new keys.
    """
    intent = SelectorOrderIntent(
        intent_id="intent-0003",
        market_ticker="KXFED-24DEC",
        outcome="yes",
        action="buy",
        price=PricePips(4500),
        size=ContractCentis(100),
        max_notional=MoneyMicros(450_000),
        implied_probability=ProbabilityPpm(620_000),
        idempotency_key="idem-0003",
    )
    decision = SelectorDecision(
        intents=(intent,),
        reasons=("test: exercising the cross intent-encoding path",),
        forecast_id="fc-0001",
        market_ticker="KXFED-24DEC",
        calibration_map_version="calib-v1",
    )

    payload = json.loads(serialize_decision(decision))
    encoded_intent = payload["intents"][0]

    assert encoded_intent["execution_style"] == "cross"
    assert encoded_intent["resting_ttl_seconds"] is None
    assert encoded_intent["cancel_on_move_ticks"] is None


def test_serialize_decision_encodes_a_resting_intents_fields_verbatim() -> None:
    """A `rest_inside_spread` intent's three issue-#46 fields serialize
    verbatim -- `execution_style` as its string, and both resting integers
    unwrapped as bare ints (900 and 2).
    """
    intent = SelectorOrderIntent(
        intent_id="intent-0004",
        market_ticker="KXFED-24DEC",
        outcome="yes",
        action="buy",
        price=PricePips(4400),
        size=ContractCentis(100),
        max_notional=MoneyMicros(440_000),
        implied_probability=ProbabilityPpm(620_000),
        idempotency_key="idem-0004",
        execution_style="rest_inside_spread",
        resting_ttl_seconds=900,
        cancel_on_move_ticks=2,
    )
    decision = SelectorDecision(
        intents=(intent,),
        reasons=("test: exercising the resting intent-encoding path",),
        forecast_id="fc-0001",
        market_ticker="KXFED-24DEC",
        calibration_map_version="calib-v1",
    )

    payload = json.loads(serialize_decision(decision))
    encoded_intent = payload["intents"][0]

    assert encoded_intent["execution_style"] == "rest_inside_spread"
    assert encoded_intent["resting_ttl_seconds"] == 900
    assert encoded_intent["cancel_on_move_ticks"] == 2


def test_decision_carries_no_generated_timestamps(
    recorded_inputs_bundle_a: SelectorInputs,
) -> None:
    """The serialized decision's top-level keys are exactly the five declared,
    non-temporal `SelectorDecision` fields -- no generated timestamp field
    (e.g. a `created_at`/`decided_at`) is ever added.
    """
    decision = select(recorded_inputs_bundle_a)

    payload = json.loads(serialize_decision(decision))

    assert set(payload) == _EXPECTED_DECISION_KEYS
