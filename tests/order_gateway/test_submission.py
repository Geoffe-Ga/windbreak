"""Failing-first tests for exchange-status gating, the Gateway ledger, and
idempotent client-order-ids (issue #38, RED).

`hedgekit/order_gateway/client_order_id.py` and
`hedgekit/order_gateway/ledger_writer.py` do not exist yet, and
`hedgekit/order_gateway/gateway.py` does not yet export `SubmitOutcome` or
widen `GatewayResult`/`OrderGateway` with `status_source`/`ledger_writer`, so
every import below fails collection -- the expected Gate 1 RED state for
issue #38.

This module pins:

    * A wired `status_source` gates submission on the exchange being
      `"open"`: `"paused"`/`"closed"`/unreachable (`None`) all refuse
      *before* the token is ever verified or consumed, and *before* the
      submitter is ever called, ledgering exactly one `SubmissionRefused`
      event.
    * A refusal never burns the token's single use: the identical token
      later verifies and submits once the exchange reopens.
    * A rejected (non-`OK`) token verification never ledgers any
      `OrderTransitionLedgered` event.
    * The first successful submission walks the full
      `APPROVE -> REQUEST_SUBMISSION -> (submit) -> SUBMIT -> ACK` chain,
      ledgering one `OrderTransitionLedgered` event *before* each next
      action, in order.
    * A ledger-write failure propagates before the next action runs (the
      submitter is never called, no resting order is left on the real paper
      exchange), and does not poison the Gateway's idempotency cache.
    * Omitting `status_source`/`ledger_writer` is fully backward compatible
      with the pre-issue-#38 happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from hedgekit.connector.models import ExchangeStatus
from hedgekit.numeric.types import ContractCentis
from hedgekit.order_gateway.client_order_id import client_order_id
from hedgekit.order_gateway.gateway import (
    OrderGateway,
    PaperSubmitter,
    SubmissionAck,
    SubmitOutcome,
)
from hedgekit.order_gateway.ledger_writer import (
    InMemoryGatewayLedgerWriter,
    OrderTransitionLedgered,
    SubmissionRefused,
)
from hedgekit.order_gateway.state_machine import OrderState
from hedgekit.order_gateway.tokens import VerifyResult
from hedgekit.riskkernel.signing import SigningKeyHandle
from hedgekit.riskkernel.tokens import TokenIssuer
from hedgekit.tokens.verify import InMemorySingleUseRegistry
from tests.order_gateway.conftest import (
    DEFAULT_NOW_EPOCH_S,
    KEY_MATERIAL,
    issue_matching_token,
    make_claims_for_intent,
    make_intent,
)

if TYPE_CHECKING:
    from typing import Literal

    from hedgekit.connector.paper import PaperExchange
    from hedgekit.order_gateway.gateway import GatewayStatusSource
    from hedgekit.riskkernel.checks import OrderIntent
    from hedgekit.tokens.verify import SignedApprovalToken

#: A fixed observation instant every `_StubStatusSource` reading stamps its
#: `ExchangeStatus.fetched_at` with -- irrelevant to gating, but a real
#: `datetime` is required to construct the value type.
_FIXED_FETCHED_AT = datetime(2024, 1, 1, tzinfo=UTC)


class _StubStatusSource:
    """A mutable test-double `GatewayStatusSource` (issue #38).

    Structurally satisfies `GatewayStatusSource`: `get_exchange_status`
    returns an `ExchangeStatus` built from whatever status was last set, or
    `None` when the source is "unreachable". `set_status` lets a test flip
    the reading mid-scenario (e.g. paused -> open) without swapping the
    `OrderGateway` under test.
    """

    def __init__(self, status: Literal["open", "paused", "closed"] | None) -> None:
        """Initialize with an initial status reading.

        Args:
            status: The initial status the source reports, or `None` to
                simulate the exchange being unreachable.
        """
        self._status: Literal["open", "paused", "closed"] | None = status

    def get_exchange_status(self) -> ExchangeStatus | None:
        """Return the currently configured status reading.

        Returns:
            An `ExchangeStatus` carrying the configured status, or `None`
            when the source is configured as unreachable.
        """
        if self._status is None:
            return None
        return ExchangeStatus(status=self._status, fetched_at=_FIXED_FETCHED_AT)

    def set_status(self, status: Literal["open", "paused", "closed"] | None) -> None:
        """Flip the configured status reading.

        Args:
            status: The new status subsequent `get_exchange_status` calls
                report, or `None` to simulate the exchange becoming
                unreachable.
        """
        self._status = status


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


class _SpyPaperSubmitter:
    """An `OrderSubmitter` delegating to a real `PaperSubmitter` while
    recording every call, so a test can assert both "the submitter was never
    called" (via `.calls`) and "the real exchange records no side effect"
    (via the exchange's own `get_open_orders()`) independently.
    """

    def __init__(self, exchange: PaperExchange) -> None:
        """Bind the spy to a real paper exchange it delegates every call to.

        Args:
            exchange: The paper exchange the delegate submitter places on.
        """
        self._delegate = PaperSubmitter(exchange)
        self.calls: list[tuple[OrderIntent, SignedApprovalToken]] = []

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Record the call, then delegate to the real `PaperSubmitter`.

        Args:
            intent: The intent being submitted.
            token: The approval token accompanying it.

        Returns:
            The delegate's real `SubmissionAck`.
        """
        self.calls.append((intent, token))
        return self._delegate.submit(intent, token)


class _ExplodingWriter:
    """A `GatewayLedgerWriter` test-double that raises `RuntimeError` on
    exactly its `raise_on_call`-th `record()` call, then behaves like a
    normal, working writer on every later call -- proving a mid-sequence
    ledger failure neither corrupts later writes nor silently swallows the
    error.
    """

    def __init__(self, *, raise_on_call: int) -> None:
        """Initialize, tracking calls against the configured failure point.

        Args:
            raise_on_call: The 1-based call number that raises. Every other
                call succeeds silently.
        """
        self._raise_on_call = raise_on_call
        self._calls = 0

    def record(self, event: object) -> None:
        """Record `event`, raising on exactly the configured call number.

        Every call past the configured failure point succeeds silently
        (the event is accepted and discarded), so the writer behaves like a
        normal, working writer once it has raised exactly once.

        Args:
            event: The ledger event to record.

        Raises:
            RuntimeError: On exactly the `raise_on_call`-th call.
        """
        self._calls += 1
        if self._calls == self._raise_on_call:
            raise RuntimeError(f"simulated ledger failure on call {self._calls}")


def _accepts_status_source(source: GatewayStatusSource) -> GatewayStatusSource:
    """Identity helper pinning `GatewayStatusSource` as the structural type
    both a real `PaperExchange` and `_StubStatusSource` must satisfy.

    Args:
        source: Any object structurally satisfying `GatewayStatusSource`.

    Returns:
        `source`, unchanged.
    """
    return source


# --- Exchange-status gating: refuse before verify, before submit --------------


@pytest.mark.parametrize(
    "configured_status,expected_reason",
    [("paused", "paused"), ("closed", "closed"), (None, "unknown")],
    ids=["paused", "closed", "unreachable"],
)
def test_process_intent_refuses_when_exchange_not_open(
    configured_status: Literal["paused", "closed"] | None, expected_reason: str
) -> None:
    """A non-`"open"` (or unreachable) exchange status refuses the intent
    before verification: `verify_result` is `None`, the state stays
    `INTENT_CREATED`, the submitter is never called, and the ledger holds
    exactly one matching `SubmissionRefused` event.
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    spy = _SpySubmitter()
    ledger_writer = InMemoryGatewayLedgerWriter()
    status_source = _StubStatusSource(configured_status)
    gateway = OrderGateway(
        spy,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=ledger_writer,
        status_source=_accepts_status_source(status_source),
    )

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.REFUSED_EXCHANGE_STATUS
    assert result.verify_result is None
    assert result.state is OrderState.INTENT_CREATED
    assert result.ack is None
    assert result.refusal_reason == expected_reason
    assert result.client_order_id == client_order_id(intent)
    assert spy.calls == []
    assert len(ledger_writer.events) == 1
    refusal = ledger_writer.events[0]
    assert isinstance(refusal, SubmissionRefused)
    assert refusal.component == "order_gateway"
    assert refusal.payload_schema_version == 1
    assert refusal.payload["reason"] == expected_reason
    assert refusal.payload["client_order_id"] == result.client_order_id


def test_process_intent_refusal_does_not_consume_the_tokens_single_use(
    paper_exchange: PaperExchange,
) -> None:
    """A refused-by-exchange-status attempt never touches the token's
    single-use registry slot: the identical token later verifies `OK` and
    submits once the exchange reopens.
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    submitter = PaperSubmitter(paper_exchange)
    status_source = _StubStatusSource("paused")
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        status_source=status_source,
    )

    refused = gateway.process_intent(intent, token)
    status_source.set_status("open")
    acked = gateway.process_intent(intent, token)

    assert refused.outcome is SubmitOutcome.REFUSED_EXCHANGE_STATUS
    assert acked.outcome is SubmitOutcome.ACKED
    assert acked.verify_result is VerifyResult.OK
    assert acked.state is OrderState.ACKED
    assert acked.ack is not None
    assert acked.ack.order_id == "paper-order-1"
    assert acked.ack.filled == ContractCentis(50)


# --- Idempotent replay is independent of current exchange status --------------


@pytest.mark.parametrize(
    "later_status",
    ["paused", "closed", None],
    ids=["paused", "closed", "unreachable"],
)
def test_process_intent_replays_cached_ack_when_exchange_no_longer_open(
    later_status: Literal["paused", "closed"] | None,
    paper_exchange: PaperExchange,
) -> None:
    """An intent ACKED while the exchange was open replays idempotently once
    the exchange stops being open. A pure replay never touches the exchange,
    so a later resubmission under a fresh valid token must return
    `IDEMPOTENT_REPLAY` with the real cached ack -- never a misleading
    `REFUSED_EXCHANGE_STATUS` -- and must place no second order and ledger no
    fresh `SubmissionRefused`. This pins the ordering fix: the idempotency-cache
    lookup runs before the exchange-status gate, so a caller retrying to learn
    "was my order placed?" against a now-paused/closed/unreachable exchange
    gets the truthful cached ack for the order still resting on the exchange.
    """
    intent = make_intent()
    first_token = issue_matching_token(intent, kernel_sequence_number=1)
    submitter = _SpyPaperSubmitter(paper_exchange)
    ledger_writer = InMemoryGatewayLedgerWriter()
    status_source = _StubStatusSource("open")
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=ledger_writer,
        status_source=status_source,
    )

    acked = gateway.process_intent(intent, first_token)
    assert acked.outcome is SubmitOutcome.ACKED
    assert len(submitter.calls) == 1
    assert len(paper_exchange.get_open_orders()) == 1

    # The exchange stops being "open"; a plain resubmission ("did this go
    # through?") must still replay the real cached ack, not refuse on status.
    status_source.set_status(later_status)
    fresh_token = issue_matching_token(intent, kernel_sequence_number=2)
    replay = gateway.process_intent(intent, fresh_token)

    assert replay.outcome is SubmitOutcome.IDEMPOTENT_REPLAY
    assert replay.state is OrderState.ACKED
    assert replay.verify_result is VerifyResult.OK
    assert replay.ack == acked.ack
    assert replay.refusal_reason is None
    # No second exchange order and no extra submit call: the exchange is untouched.
    assert len(submitter.calls) == 1
    assert len(paper_exchange.get_open_orders()) == 1
    # The replay ledgers no fresh refusal and no new transition beyond the
    # first submission's four.
    assert not any(
        isinstance(event, SubmissionRefused) for event in ledger_writer.events
    )
    transitions = [
        event
        for event in ledger_writer.events
        if isinstance(event, OrderTransitionLedgered)
    ]
    assert len(transitions) == 4


# --- Rejected token verification: no transition events ------------------------


def test_process_intent_rejected_token_ledgers_no_transition_events(
    paper_exchange: PaperExchange,
) -> None:
    """A wrong-issuer (bad-signature) token, presented while the exchange is
    open, is `REJECTED_TOKEN`: the submitter is never called and the ledger
    holds zero `OrderTransitionLedgered` events.
    """
    intent = make_intent()
    claims = make_claims_for_intent(intent)
    wrong_issuer = TokenIssuer(SigningKeyHandle(b"z" * 32))
    token = wrong_issuer.issue(claims)
    spy = _SpySubmitter()
    ledger_writer = InMemoryGatewayLedgerWriter()
    gateway = OrderGateway(
        spy,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=ledger_writer,
        status_source=_StubStatusSource("open"),
    )

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.REJECTED_TOKEN
    assert result.verify_result is VerifyResult.BAD_SIGNATURE
    assert result.state is OrderState.INTENT_CREATED
    assert result.ack is None
    assert result.client_order_id == client_order_id(intent)
    assert spy.calls == []
    transitions = [
        event
        for event in ledger_writer.events
        if isinstance(event, OrderTransitionLedgered)
    ]
    assert transitions == []


# --- Happy path: four transitions, written before each next action -----------


def test_process_intent_happy_path_acks_and_ledgers_four_ordered_transitions(
    paper_exchange: PaperExchange,
) -> None:
    """A verified intent against a real, open `PaperExchange` reaches
    `ACKED` with the hand-derived fill, and the ledger holds exactly the four
    `OrderTransitionLedgered` events the state chain walks, in order, each
    carrying the matching `client_order_id`.
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    submitter = PaperSubmitter(paper_exchange)
    ledger_writer = InMemoryGatewayLedgerWriter()
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=ledger_writer,
        status_source=_accepts_status_source(paper_exchange),
    )

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.ACKED
    assert result.verify_result is VerifyResult.OK
    assert result.state is OrderState.ACKED
    assert result.client_order_id == client_order_id(intent)
    assert isinstance(result.ack, SubmissionAck)
    assert result.ack.order_id == "paper-order-1"
    assert result.ack.filled == ContractCentis(50)

    transitions = [
        event
        for event in ledger_writer.events
        if isinstance(event, OrderTransitionLedgered)
    ]
    assert len(transitions) == 4
    expected = (
        ("INTENT_CREATED", "APPROVE", "APPROVED"),
        ("APPROVED", "REQUEST_SUBMISSION", "SUBMISSION_REQUESTED"),
        ("SUBMISSION_REQUESTED", "SUBMIT", "SUBMITTED"),
        ("SUBMITTED", "ACK", "ACKED"),
    )
    for event, (from_state, event_name, to_state) in zip(
        transitions, expected, strict=True
    ):
        assert event.component == "order_gateway"
        assert event.payload_schema_version == 1
        assert event.payload["from_state"] == from_state
        assert event.payload["event"] == event_name
        assert event.payload["to_state"] == to_state
        assert event.payload["client_order_id"] == result.client_order_id


# --- Ledger-write failure: propagates before the next action ------------------


def test_process_intent_ledger_failure_on_first_write_propagates_and_never_submits(
    paper_exchange: PaperExchange,
) -> None:
    """A `record()` failure on the very first ledger write (the
    `APPROVE` transition, written before submission is even requested)
    propagates out of `process_intent`, and the real exchange records no
    resting order because the submitter is never reached.
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    submitter = _SpyPaperSubmitter(paper_exchange)
    exploding = _ExplodingWriter(raise_on_call=1)
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=exploding,
        status_source=_StubStatusSource("open"),
    )

    with pytest.raises(RuntimeError):
        gateway.process_intent(intent, token)

    assert submitter.calls == []
    assert paper_exchange.get_open_orders() == ()


def test_ledger_failure_on_second_write_does_not_poison_idempotency_cache(
    paper_exchange: PaperExchange,
) -> None:
    """A `record()` failure on the second ledger write (the
    `REQUEST_SUBMISSION` transition, written before `submit()` runs)
    likewise propagates before the submitter is ever called and leaves no
    resting order. The failing writer then recovers (it raises on exactly
    one call), so a later attempt for the *same* intent under a *fresh*
    token still reaches `ACKED` -- proving the failed attempt's
    `client_order_id` was never cached as already-submitted.
    """
    intent = make_intent()
    first_token = issue_matching_token(intent)
    submitter = _SpyPaperSubmitter(paper_exchange)
    exploding = _ExplodingWriter(raise_on_call=2)
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=exploding,
        status_source=_StubStatusSource("open"),
    )

    with pytest.raises(RuntimeError):
        gateway.process_intent(intent, first_token)

    assert submitter.calls == []
    assert paper_exchange.get_open_orders() == ()

    fresh_token = issue_matching_token(intent, kernel_sequence_number=99)
    result = gateway.process_intent(intent, fresh_token)

    assert result.outcome is SubmitOutcome.ACKED
    assert result.outcome is not SubmitOutcome.IDEMPOTENT_REPLAY
    assert len(submitter.calls) == 1
    assert result.ack is not None
    assert result.ack.order_id == "paper-order-1"
    assert result.ack.filled == ContractCentis(50)


def test_ledger_failure_after_submit_caches_ack_so_retry_never_double_places(
    paper_exchange: PaperExchange,
) -> None:
    """A `record()` failure on the third ledger write (the `SUBMIT`
    transition, written *after* the exchange placement already happened)
    propagates, but the order is already resting on the exchange. Because the
    ack is cached the instant `submit()` returns -- before the post-submit
    `SUBMIT`/`ACK` writes -- a retry for the *same* intent under a *fresh*
    token short-circuits to `IDEMPOTENT_REPLAY` and never places a duplicate,
    so exactly one order ever rests. This pins the fail-safe: a post-submit
    ledger failure must not leave an order on the exchange yet absent from the
    idempotency cache (which would double-submit on retry).
    """
    intent = make_intent()
    first_token = issue_matching_token(intent)
    submitter = _SpyPaperSubmitter(paper_exchange)
    exploding = _ExplodingWriter(raise_on_call=3)
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=exploding,
        status_source=_StubStatusSource("open"),
    )

    with pytest.raises(RuntimeError):
        gateway.process_intent(intent, first_token)

    # The placement DID happen (submit ran once, one order rests) even though
    # the post-submit ledger write failed.
    assert len(submitter.calls) == 1
    assert len(paper_exchange.get_open_orders()) == 1

    fresh_token = issue_matching_token(intent, kernel_sequence_number=99)
    result = gateway.process_intent(intent, fresh_token)

    # The retry is an idempotent replay of the already-placed order, NOT a
    # second submission: still exactly one call and one resting order.
    assert result.outcome is SubmitOutcome.IDEMPOTENT_REPLAY
    assert len(submitter.calls) == 1
    assert len(paper_exchange.get_open_orders()) == 1
    assert result.ack is not None
    assert result.ack.order_id == "paper-order-1"


# --- Backward compatibility: no status_source / ledger_writer wired ----------


def test_order_gateway_without_status_source_or_ledger_writer_still_acks(
    paper_exchange: PaperExchange,
) -> None:
    """Omitting both `status_source` and `ledger_writer` (the pre-issue-#38
    surface) still reaches `ACKED` against a real `PaperExchange` -- the
    tracer invariant proving issue #38 is additive, not breaking.
    """
    intent = make_intent()
    # No clock is injected, so the gateway verifies against the real wall clock;
    # the token's expiry must be far in the future to stay valid (mirrors
    # test_gateway.py::test_order_gateway_default_registry_and_clock_still_verify_ok).
    far_future_expiry = DEFAULT_NOW_EPOCH_S + 10_000_000_000
    token = issue_matching_token(intent, expires_at=far_future_expiry)
    submitter = PaperSubmitter(paper_exchange)
    gateway = OrderGateway(submitter, verification_key=KEY_MATERIAL)

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.ACKED
    assert result.state is OrderState.ACKED
    assert result.ack is not None
    assert result.ack.order_id == "paper-order-1"
    assert result.ack.filled == ContractCentis(50)
