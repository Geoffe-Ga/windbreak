"""Failing-first tests for `windbreak.order_gateway.state_machine` (issue #37, RED).

`windbreak/order_gateway/state_machine.py` does not exist yet, so importing it
fails collection with `ModuleNotFoundError: No module named
'windbreak.order_gateway.state_machine'` -- the expected Gate 1 RED state for
issue #37.

This module hand-authors an independent, from-the-issue-spec `(state, event)
-> target` table (`_EXPECTED_TRANSITIONS`) and exhaustively checks it against
every one of the 14 * 13 == 182 possible `(OrderState, OrderEvent)` pairs: a
pair present in the table must both appear in `LEGAL_TRANSITIONS` with the
matching target *and* have `transition()` return that target; every absent
pair must be absent from `LEGAL_TRANSITIONS` *and* have `transition()` raise
`IllegalTransitionError` naming both the offending state and event. Per the issue,
the count of legal edges is never hardcoded as a magic number -- the
exhaustive sweep proves the table is exactly right without one.
"""

from __future__ import annotations

import itertools

import pytest

from windbreak.order_gateway.state_machine import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    OrderEvent,
    OrderState,
    transition,
)


def test_order_state_members_match_the_pinned_14() -> None:
    """`OrderState` names exactly the 14 pinned members."""
    assert {state.name for state in OrderState} == {
        "INTENT_CREATED",
        "APPROVED",
        "SUBMISSION_REQUESTED",
        "SUBMITTED",
        "ACKED",
        "PARTIAL_FILL",
        "FILLED",
        "CANCEL_REQUESTED",
        "CANCELLED",
        "EXPIRED",
        "REJECTED",
        "RECONCILED",
        "DISPUTED",
        "SETTLEMENT_REVERSED",
    }


def test_order_event_members_match_the_pinned_13() -> None:
    """`OrderEvent` names exactly the 13 pinned members."""
    assert {event.name for event in OrderEvent} == {
        "APPROVE",
        "REQUEST_SUBMISSION",
        "SUBMIT",
        "ACK",
        "PARTIAL_FILL",
        "FILL",
        "REQUEST_CANCEL",
        "CANCEL",
        "EXPIRE",
        "REJECT",
        "RECONCILE",
        "DISPUTE",
        "SETTLEMENT_REVERSE",
    }


# --- The independently hand-authored oracle: every legal (state, event) edge ---

_EXPECTED_TRANSITIONS: dict[tuple[OrderState, OrderEvent], OrderState] = {
    (OrderState.INTENT_CREATED, OrderEvent.APPROVE): OrderState.APPROVED,
    (OrderState.APPROVED, OrderEvent.REQUEST_SUBMISSION): (
        OrderState.SUBMISSION_REQUESTED
    ),
    (OrderState.SUBMISSION_REQUESTED, OrderEvent.SUBMIT): OrderState.SUBMITTED,
    (OrderState.SUBMITTED, OrderEvent.ACK): OrderState.ACKED,
    (OrderState.SUBMITTED, OrderEvent.REJECT): OrderState.REJECTED,
    (OrderState.ACKED, OrderEvent.PARTIAL_FILL): OrderState.PARTIAL_FILL,
    (OrderState.ACKED, OrderEvent.FILL): OrderState.FILLED,
    (OrderState.ACKED, OrderEvent.REQUEST_CANCEL): OrderState.CANCEL_REQUESTED,
    (OrderState.ACKED, OrderEvent.EXPIRE): OrderState.EXPIRED,
    (OrderState.PARTIAL_FILL, OrderEvent.PARTIAL_FILL): OrderState.PARTIAL_FILL,
    (OrderState.PARTIAL_FILL, OrderEvent.FILL): OrderState.FILLED,
    (OrderState.PARTIAL_FILL, OrderEvent.REQUEST_CANCEL): (OrderState.CANCEL_REQUESTED),
    (OrderState.PARTIAL_FILL, OrderEvent.EXPIRE): OrderState.EXPIRED,
    (OrderState.CANCEL_REQUESTED, OrderEvent.CANCEL): OrderState.CANCELLED,
    (OrderState.CANCEL_REQUESTED, OrderEvent.FILL): OrderState.FILLED,
    (OrderState.CANCEL_REQUESTED, OrderEvent.PARTIAL_FILL): (
        OrderState.CANCEL_REQUESTED
    ),
    (OrderState.FILLED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.FILLED, OrderEvent.DISPUTE): OrderState.DISPUTED,
    (OrderState.CANCELLED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.CANCELLED, OrderEvent.DISPUTE): OrderState.DISPUTED,
    (OrderState.EXPIRED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.EXPIRED, OrderEvent.DISPUTE): OrderState.DISPUTED,
    (OrderState.REJECTED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.REJECTED, OrderEvent.DISPUTE): OrderState.DISPUTED,
    (OrderState.DISPUTED, OrderEvent.RECONCILE): OrderState.RECONCILED,
    (OrderState.RECONCILED, OrderEvent.SETTLEMENT_REVERSE): (
        OrderState.SETTLEMENT_REVERSED
    ),
}


def _pair_id(pair: tuple[OrderState, OrderEvent]) -> str:
    """Render a `(state, event)` pair as a short, readable pytest id."""
    state, event = pair
    return f"{state.name}--{event.name}"


_ALL_PAIRS = list(itertools.product(OrderState, OrderEvent))


@pytest.mark.parametrize(
    "state,event", _ALL_PAIRS, ids=[_pair_id(p) for p in _ALL_PAIRS]
)
def test_transition_matches_the_full_14x13_legal_table(
    state: OrderState, event: OrderEvent
) -> None:
    """Exhaustively sweeps all 182 `(OrderState, OrderEvent)` pairs: a pair in
    the hand-authored oracle transitions to its pinned target (and
    `LEGAL_TRANSITIONS` agrees); every other pair is absent from
    `LEGAL_TRANSITIONS` and raises `IllegalTransitionError` naming both inputs.
    """
    expected_target = _EXPECTED_TRANSITIONS.get((state, event))

    if expected_target is not None:
        assert LEGAL_TRANSITIONS[(state, event)] == expected_target
        assert transition(state, event) == expected_target
    else:
        assert (state, event) not in LEGAL_TRANSITIONS
        with pytest.raises(IllegalTransitionError) as exc_info:
            transition(state, event)
        assert exc_info.value.state == state
        assert exc_info.value.event == event


def test_legal_transitions_mapping_is_read_only() -> None:
    """`LEGAL_TRANSITIONS` rejects item assignment: it is a read-only view
    (e.g. `MappingProxyType`), not a plain mutable dict a caller could corrupt.
    """
    with pytest.raises(TypeError):
        LEGAL_TRANSITIONS[(OrderState.INTENT_CREATED, OrderEvent.APPROVE)] = (
            OrderState.FILLED
        )


def test_intent_created_plus_fill_raises_per_the_issue_example() -> None:
    """The issue's own worked example: `INTENT_CREATED` has no `FILL` edge."""
    with pytest.raises(IllegalTransitionError) as exc_info:
        transition(OrderState.INTENT_CREATED, OrderEvent.FILL)

    assert exc_info.value.state is OrderState.INTENT_CREATED
    assert exc_info.value.event is OrderEvent.FILL


def test_illegal_transition_message_names_both_state_and_event() -> None:
    """The exception message names both the offending state and event, so an
    operator reading a log line can diagnose it without a debugger.
    """
    with pytest.raises(IllegalTransitionError) as exc_info:
        transition(OrderState.FILLED, OrderEvent.SUBMIT)

    message = str(exc_info.value)
    assert "FILLED" in message
    assert "SUBMIT" in message


def test_required_submission_chain_reaches_acked() -> None:
    """The gateway's own required chain (APPROVE, REQUEST_SUBMISSION, SUBMIT,
    ACK) walks `INTENT_CREATED` all the way to `ACKED`.
    """
    state = OrderState.INTENT_CREATED
    for event in (
        OrderEvent.APPROVE,
        OrderEvent.REQUEST_SUBMISSION,
        OrderEvent.SUBMIT,
        OrderEvent.ACK,
    ):
        state = transition(state, event)

    assert state is OrderState.ACKED


def test_settlement_reversed_has_no_outgoing_legal_edges() -> None:
    """`SETTLEMENT_REVERSED` is terminal: no event legally leaves it."""
    for event in OrderEvent:
        assert (OrderState.SETTLEMENT_REVERSED, event) not in LEGAL_TRANSITIONS
