"""Failing-first tests for `client_order_id` and Gateway-level idempotent
resubmission (issue #38, RED).

`windbreak/order_gateway/client_order_id.py` does not exist yet, and
`windbreak/order_gateway/gateway.py`/`windbreak/order_gateway/ledger_writer.py`
do not yet expose `SubmitOutcome`/`GatewayResult.outcome`/the ledger-writer
surface, so every import below fails collection -- the expected Gate 1 RED
state for issue #38.

This module pins:

    * `client_order_id` is a pure, deterministic SHA-256 hex digest (64
      lowercase hex characters) of all nine `OrderIntent` fields: identical
      across repeated calls, identical for independently constructed but
      field-equal intents, and different when *any single* field changes.
    * Resubmitting the *same* intent under N>=3 distinct, freshly minted
      (and independently verifying) approval tokens submits to the exchange
      exactly once: the first call is `ACKED`, every later call is
      `IDEMPOTENT_REPLAY` returning the identical cached `SubmissionAck`,
      never calling the submitter again and never ledgering a new
      `OrderTransitionLedgered` event.
    * A genuinely *different* intent (one changed field, its own matching
      token) is a real, independent second submission.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.order_gateway.conftest import (
    DEFAULT_NOW_EPOCH_S,
    KEY_MATERIAL,
    issue_matching_token,
    make_intent,
)
from windbreak.numeric.types import (
    ContractCentis,
    MoneyMicros,
    PricePips,
    ProbabilityPpm,
)
from windbreak.order_gateway.client_order_id import client_order_id
from windbreak.order_gateway.gateway import (
    OrderGateway,
    PaperSubmitter,
    SubmissionAck,
    SubmitOutcome,
)
from windbreak.order_gateway.ledger_writer import (
    InMemoryGatewayLedgerWriter,
    OrderTransitionLedgered,
)
from windbreak.order_gateway.state_machine import OrderState
from windbreak.riskkernel.checks import OrderIntent
from windbreak.tokens.verify import InMemorySingleUseRegistry

if TYPE_CHECKING:
    from windbreak.connector.paper import PaperExchange
    from windbreak.tokens.verify import SignedApprovalToken

#: Every `OrderIntent` field name, in declaration order -- drives the
#: per-field-sensitivity parametrization below.
_ALL_INTENT_FIELDS: tuple[str, ...] = tuple(
    field.name for field in dataclasses.fields(OrderIntent)
)

#: The scaled-integer unit types a perturbed field's replacement value must be
#: rewrapped in, so `dataclasses.replace` never receives a bare, wrongly typed
#: int for a unit field.
_UNIT_TYPES: tuple[type, ...] = (PricePips, ContractCentis, MoneyMicros, ProbabilityPpm)

#: Bounded, float-free Hypothesis strategies for building independently
#: constructed but field-equal `OrderIntent`s: short, non-empty text for the
#: identity/string fields, and small positive integers for every scaled-unit
#: field's underlying `.value`.
_TEXT_FIELD = st.text(min_size=1, max_size=12)
_UNIT_VALUE = st.integers(min_value=1, max_value=1_000_000)


class _SpySubmitter:
    """A test-double `OrderSubmitter` recording every `submit()` call."""

    def __init__(self) -> None:
        """Initialize with an empty call log."""
        self.calls: list[tuple[OrderIntent, SignedApprovalToken]] = []

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Record the call and return a fixed, deterministic `SubmissionAck`.

        Args:
            intent: The intent being submitted.
            token: The approval token accompanying it.

        Returns:
            A `SubmissionAck` whose `filled` mirrors `intent.size`.
        """
        self.calls.append((intent, token))
        return SubmissionAck(order_id="spy-order-1", filled=intent.size)


def _build_intent(
    *,
    intent_id: str,
    market_ticker: str,
    outcome: str,
    action: str,
    price_value: int,
    size_value: int,
    max_notional_value: int,
    implied_probability_value: int,
    idempotency_key: str,
) -> OrderIntent:
    """Construct an `OrderIntent` from plain field values.

    Every scaled-unit argument is wrapped in its `OrderIntent`-declared type
    here, so two independent calls with the same plain values build two
    distinct, but field-equal, `OrderIntent` objects.

    Args:
        intent_id: The intent's unique identifier.
        market_ticker: The exchange ticker the intent targets.
        outcome: The market outcome the intent trades.
        action: The trade action.
        price_value: The limit price's underlying integer, in pips.
        size_value: The contract count's underlying integer, in centis.
        max_notional_value: The notional cap's underlying integer, in micros.
        implied_probability_value: The implied probability's underlying
            integer, in ppm.
        idempotency_key: The caller-supplied idempotency key.

    Returns:
        A fully populated `OrderIntent`.
    """
    return OrderIntent(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        price=PricePips(price_value),
        size=ContractCentis(size_value),
        max_notional=MoneyMicros(max_notional_value),
        implied_probability=ProbabilityPpm(implied_probability_value),
        idempotency_key=idempotency_key,
    )


def _perturbed_value(intent: OrderIntent, field_name: str) -> object:
    """Return a value for `field_name` guaranteed to differ from `intent`'s.

    A string field is suffixed; a scaled-unit field's underlying integer is
    incremented and rewrapped in the exact same unit type, so
    `dataclasses.replace` never receives a bare, mistyped int.

    Args:
        intent: The intent whose current field value is being perturbed.
        field_name: The `OrderIntent` field to perturb.

    Returns:
        A replacement value of the same type as the current field value, but
        never equal to it.

    Raises:
        TypeError: If the current field's type is neither `str` nor one of
            the four `windbreak.numeric` unit types.
    """
    current = getattr(intent, field_name)
    if isinstance(current, str):
        return current + "-different"
    if isinstance(current, _UNIT_TYPES):
        return type(current)(current.value + 1)
    raise TypeError(f"unsupported field type for {field_name!r}: {type(current)!r}")


# --- client_order_id: deterministic, equal-for-equal, sensitive-to-any-field --


def test_client_order_id_is_a_64_character_lowercase_hex_digest() -> None:
    """`client_order_id` returns a 64-character, lowercase-hex SHA-256 digest."""
    intent = make_intent()

    digest = client_order_id(intent)

    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_client_order_id_is_deterministic_across_repeated_calls() -> None:
    """Calling `client_order_id` twice on the same intent yields the same id."""
    intent = make_intent()

    first = client_order_id(intent)
    second = client_order_id(intent)

    assert first == second


@given(
    intent_id=_TEXT_FIELD,
    market_ticker=_TEXT_FIELD,
    outcome=_TEXT_FIELD,
    action=_TEXT_FIELD,
    price_value=_UNIT_VALUE,
    size_value=_UNIT_VALUE,
    max_notional_value=_UNIT_VALUE,
    implied_probability_value=_UNIT_VALUE,
    idempotency_key=_TEXT_FIELD,
)
def test_client_order_id_is_identical_for_independently_built_equal_intents(
    intent_id: str,
    market_ticker: str,
    outcome: str,
    action: str,
    price_value: int,
    size_value: int,
    max_notional_value: int,
    implied_probability_value: int,
    idempotency_key: str,
) -> None:
    """Two `OrderIntent`s built independently (never the same object, never
    copied) from identical field values still hash to the identical
    `client_order_id` -- proving the id is a pure function of field values,
    not of object identity.
    """
    first = _build_intent(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        price_value=price_value,
        size_value=size_value,
        max_notional_value=max_notional_value,
        implied_probability_value=implied_probability_value,
        idempotency_key=idempotency_key,
    )
    second = _build_intent(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        price_value=price_value,
        size_value=size_value,
        max_notional_value=max_notional_value,
        implied_probability_value=implied_probability_value,
        idempotency_key=idempotency_key,
    )

    assert first is not second
    assert first == second
    assert client_order_id(first) == client_order_id(second)


@pytest.mark.parametrize("field_name", _ALL_INTENT_FIELDS)
def test_client_order_id_changes_when_any_single_field_changes(field_name: str) -> None:
    """Perturbing exactly one `OrderIntent` field yields a different
    `client_order_id` -- exercised over all nine fields in turn.
    """
    intent = make_intent()
    perturbed = dataclasses.replace(
        intent, **{field_name: _perturbed_value(intent, field_name)}
    )

    assert client_order_id(intent) != client_order_id(perturbed)


# --- Gateway-level idempotent resubmission: one exchange order, N tokens -----


def test_process_intent_resubmission_with_distinct_tokens_rests_exactly_one_order(
    paper_exchange: PaperExchange,
) -> None:
    """Submitting the *same* intent three times, each under its own freshly
    minted (distinct-signature, independently verifying) token, submits to
    the real exchange exactly once: the first call is `ACKED`, both later
    calls are `IDEMPOTENT_REPLAY` returning the identical cached
    `SubmissionAck`, and exactly one order rests on the exchange.
    """
    intent = make_intent()
    tokens = [
        issue_matching_token(intent, kernel_sequence_number=n) for n in range(1, 4)
    ]
    submitter = PaperSubmitter(paper_exchange)
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
    )

    results = [gateway.process_intent(intent, token) for token in tokens]

    assert results[0].outcome is SubmitOutcome.ACKED
    assert results[0].ack is not None
    assert results[0].ack.order_id == "paper-order-1"
    assert results[0].ack.filled == ContractCentis(50)
    for later in results[1:]:
        assert later.outcome is SubmitOutcome.IDEMPOTENT_REPLAY
        assert later.state is OrderState.ACKED
        assert later.ack == results[0].ack
    assert len(paper_exchange.get_open_orders()) == 1


def test_resubmission_calls_submitter_once_and_ledgers_no_new_transitions() -> None:
    """The same three-distinct-tokens resubmission, observed through a spy
    submitter and an in-memory ledger writer: `submit()` is called exactly
    once, and the replayed calls ledger zero additional
    `OrderTransitionLedgered` events beyond the first submission's four.
    """
    intent = make_intent()
    tokens = [
        issue_matching_token(intent, kernel_sequence_number=n) for n in range(10, 13)
    ]
    spy = _SpySubmitter()
    ledger_writer = InMemoryGatewayLedgerWriter()
    gateway = OrderGateway(
        spy,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=ledger_writer,
    )

    results = [gateway.process_intent(intent, token) for token in tokens]

    assert results[0].outcome is SubmitOutcome.ACKED
    assert all(
        result.outcome is SubmitOutcome.IDEMPOTENT_REPLAY for result in results[1:]
    )
    assert len(spy.calls) == 1
    transitions = [
        event
        for event in ledger_writer.events
        if isinstance(event, OrderTransitionLedgered)
    ]
    assert len(transitions) == 4


def test_process_intent_distinct_intent_is_a_real_second_submission(
    paper_exchange: PaperExchange,
) -> None:
    """A genuinely different intent (one changed field, its own matching
    token) is a real, independent second submission: its own
    `client_order_id`, its own `ACKED` outcome, and its own resting order
    alongside the first intent's.
    """
    intent_a = make_intent()
    token_a = issue_matching_token(intent_a)
    intent_b = dataclasses.replace(intent_a, idempotency_key="idem-0002")
    token_b = issue_matching_token(intent_b)
    submitter = PaperSubmitter(paper_exchange)
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
    )

    result_a = gateway.process_intent(intent_a, token_a)
    result_b = gateway.process_intent(intent_b, token_b)

    assert result_a.outcome is SubmitOutcome.ACKED
    assert result_b.outcome is SubmitOutcome.ACKED
    assert result_a.client_order_id != result_b.client_order_id
    assert result_a.ack is not None
    assert result_b.ack is not None
    assert result_a.ack.order_id == "paper-order-1"
    assert result_b.ack.order_id == "paper-order-2"
    assert len(paper_exchange.get_open_orders()) == 2
