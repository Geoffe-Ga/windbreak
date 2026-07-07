"""Deterministic client-order-id derivation for the Order Gateway (issue #38).

The Gateway needs a stable, content-addressed identity for every intent it
submits so that a resubmission of the *same* economic order -- even under a
fresh approval token -- is recognizably the same order, and so a crash-recovery
join (issue #40) can re-associate a persisted transition with the intent that
produced it. :func:`client_order_id` supplies that identity: a pure,
deterministic SHA-256 digest over the intent's field values, hashed through the
ledger's :func:`~hedgekit.ledger.events.canonical_json` so the encoding is
byte-stable regardless of mapping insertion order.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from hedgekit.ledger.events import canonical_json

if TYPE_CHECKING:
    from hedgekit.riskkernel.checks import OrderIntent


def client_order_id(intent: OrderIntent) -> str:
    """Derive the deterministic client-order-id for ``intent``.

    Returns the lowercase-hex SHA-256 digest (64 characters) of the canonical
    JSON encoding of all nine :class:`~hedgekit.riskkernel.checks.OrderIntent`
    fields. The scaled-integer money-path fields are serialized via their
    underlying ``.value`` int (never a float, SPEC S6.1); the identity fields
    are their plain strings. The function is pure and deterministic -- it reads
    no clock and draws no randomness -- so two independently constructed but
    field-equal intents always hash identically, and any single differing field
    yields a different digest.

    The exact nine-field set is load-bearing: the crash-recovery join in issue
    #40 re-derives this id from a persisted intent to re-associate it with its
    ledgered transitions, so adding, dropping, or reordering a field into the
    hash is a breaking change to that recovery contract, not a refactor.

    Args:
        intent: The order intent to derive an id for.

    Returns:
        The 64-character, lowercase-hex SHA-256 client-order-id.
    """
    fields: dict[str, object] = {
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
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()
