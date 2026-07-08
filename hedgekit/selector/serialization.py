"""Canonical serialization for selector decisions (SPEC S9.1, issue #43).

SPEC S9.1 requires a selector decision to serialize to a *byte-identical*
form on every run, in-process or from a fresh interpreter, so the ledger can
hash over it and the golden-determinism harness can diff it against a committed
fixture. This module projects a :class:`~hedgekit.selector.types.SelectorDecision`
into a plain, JSON-safe mapping and defers the byte-stable encoding to
:func:`~hedgekit.ledger.events.canonical_json` (sorted keys, no whitespace) --
never re-inventing ``json.dumps`` here.

Every numeric leaf is a bare ``int``: the scaled-integer unit types wrapping an
intent's price/size/notional/probability are unwrapped to their ``.value``
(SPEC S6.1, no floats on the money path), mirroring the "field name verbatim,
unwrap the unit type" convention in
:func:`hedgekit.forecast.records.forecast_record_to_payload`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hedgekit.ledger.events import canonical_json

if TYPE_CHECKING:
    from hedgekit.selector.types import NormalizedOrderIntent, SelectorDecision


def _intent_to_payload(intent: NormalizedOrderIntent) -> dict[str, object]:
    """Project one normalized order intent into a JSON-safe mapping.

    Each field is mapped by its dataclass name verbatim; the four scaled-integer
    unit fields (``price``/``size``/``max_notional``/``implied_probability``)
    are unwrapped to their bare ``.value`` int, so no float ever appears (SPEC
    S6.1). This mirrors the unwrap-the-unit-type convention in
    :func:`hedgekit.forecast.records.forecast_record_to_payload`.

    Args:
        intent: The normalized order intent to project.

    Returns:
        A JSON-serializable mapping of the intent's fields, with numeric units
        unwrapped to plain ints.
    """
    return {
        "intent_id": intent.intent_id,
        "market_ticker": intent.market_ticker,
        "outcome": intent.outcome,
        "action": intent.action,
        "price": intent.price.value,
        "size": intent.size.value,
        "max_notional": intent.max_notional.value,
        "implied_probability": intent.implied_probability.value,
        "idempotency_key": intent.idempotency_key,
    }


def serialize_decision(decision: SelectorDecision) -> str:
    """Serialize a selector decision to canonical, byte-stable JSON.

    The payload carries exactly the five declared, non-temporal
    :class:`~hedgekit.selector.types.SelectorDecision` fields -- no generated
    timestamp is ever added -- and delegates the encoding to
    :func:`~hedgekit.ledger.events.canonical_json` so the bytes are a stable
    function of the decision's contents alone (SPEC S9.1).

    Args:
        decision: The selector decision to serialize.

    Returns:
        The canonical JSON encoding of ``decision``: sorted keys, no
        whitespace, no float leaf.
    """
    payload: dict[str, object] = {
        "intents": [_intent_to_payload(intent) for intent in decision.intents],
        "reasons": list(decision.reasons),
        "forecast_id": decision.forecast_id,
        "market_ticker": decision.market_ticker,
        "calibration_map_version": decision.calibration_map_version,
    }
    return canonical_json(payload)
