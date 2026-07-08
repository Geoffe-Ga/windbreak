"""Failing-first tests for reduce-only enforcement on closes (issue #39, RED).

`windbreak/order_gateway/reduce_only.py` does not exist yet, and
`windbreak/order_gateway/gateway.py` does not yet export `GatewayHaltedError` or
widen `OrderGateway`/`GatewayResult` with `position_source`/`position_snapshot`,
nor does `windbreak/order_gateway/ledger_writer.py` yet export
`ReduceOnlyRefused`/`ReduceOnlyViolation`, so the module-level import below
fails collection with an `ImportError` naming the first missing symbol
(currently `GatewayHaltedError` from `windbreak.order_gateway.gateway`) -- the
expected Gate 1 RED state for issue #39. This mirrors
`tests/order_gateway/test_submission.py`'s own documented RED state for issue
#38.

This module pins the SPEC S6.4/S11.2 reduce-only contract for
`SELL_TO_CLOSE`:

    * A close is admitted iff its requested size does not exceed the held
      position minus whatever is already in flight for that ticker
      (`closeable = held - inflight`); an oversized or positionless close is
      refused with `SubmitOutcome.REFUSED_REDUCE_ONLY`, `refusal_reason ==
      "reduce_only"`, a populated `position_snapshot`, and a ledgered
      `ReduceOnlyRefused` event -- all *before* the token is verified or
      consumed, so a refusal never burns the token's single use.
    * A successful close's size is added to the ticker's in-flight-closing
      total, shrinking the closeable remainder for the next concurrent close;
      an idempotent replay of an already-ACKed close does not re-run the
      check or double-count that remainder.
    * A post-fill net-short position (the venue filled more than was held)
      ledgers a `ReduceOnlyViolation` and halts the Gateway: `GatewayHaltedError`
      is raised for that call and every subsequent call, fail-closed.
    * The Gateway prefers a submitter's `submit_reduce_only` (the venue-side
      flag) when available, falling back to plain `submit` otherwise; either
      way the local size check still runs. `BUY_TO_OPEN` is untouched: it
      never consults the position source and never uses the reduce-only flag.
    * The exchange-status gate still runs first: a paused/closed exchange
      refuses before reduce-only is even evaluated, and the position source
      is never read. Omitting `position_source` is fully backward compatible
      (enforcement off), matching the pre-issue-#39 surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from tests.order_gateway.conftest import (
    DEFAULT_MARKET_TICKER,
    DEFAULT_NOW_EPOCH_S,
    KEY_MATERIAL,
    issue_matching_token,
    make_intent,
)
from windbreak.connector.models import ExchangeStatus, Position
from windbreak.numeric.types import ContractCentis, PricePips
from windbreak.order_gateway.client_order_id import client_order_id
from windbreak.order_gateway.gateway import (
    GatewayHaltedError,
    OrderGateway,
    SubmissionAck,
    SubmitOutcome,
)
from windbreak.order_gateway.ledger_writer import (
    InMemoryGatewayLedgerWriter,
    ReduceOnlyRefused,
    ReduceOnlyViolation,
)
from windbreak.order_gateway.reduce_only import PositionSnapshot
from windbreak.order_gateway.state_machine import OrderState
from windbreak.order_gateway.tokens import VerifyResult

if TYPE_CHECKING:
    from windbreak.order_gateway.gateway import (
        GatewayPositionSource,
        GatewayStatusSource,
        OrderSubmitter,
    )
    from windbreak.riskkernel.checks import OrderIntent
    from windbreak.tokens.verify import SignedApprovalToken

#: A fixed observation instant every stub `GatewayStatusSource` reading
#: stamps its `ExchangeStatus.fetched_at` with -- irrelevant to reduce-only
#: gating, but a real `datetime` is required to construct the value type.
_FIXED_FETCHED_AT = datetime(2024, 1, 1, tzinfo=UTC)

#: The `Position.average_price` every `_position` helper stamps -- irrelevant
#: to reduce-only math (which only ever reads `quantity`), but required to
#: construct the frozen value type.
_IRRELEVANT_AVERAGE_PRICE = PricePips(4600)


def _position(quantity_centis: int, *, ticker: str = DEFAULT_MARKET_TICKER) -> Position:
    """Build a `Position` for `ticker` holding `quantity_centis` net contracts.

    Args:
        quantity_centis: The net held quantity, in contract-centis.
        ticker: The market ticker the position is held in. Defaults to
            `DEFAULT_MARKET_TICKER`.

    Returns:
        A `Position` with a fixed, reduce-only-irrelevant `average_price`.
    """
    return Position(
        ticker=ticker,
        quantity=ContractCentis(quantity_centis),
        average_price=_IRRELEVANT_AVERAGE_PRICE,
    )


class _AlwaysOpenStatusSource:
    """A `GatewayStatusSource` stub that always reports the exchange open."""

    def get_exchange_status(self) -> ExchangeStatus | None:
        """Return a fixed `"open"` status reading.

        Returns:
            An `ExchangeStatus` with `status="open"`.
        """
        return ExchangeStatus(status="open", fetched_at=_FIXED_FETCHED_AT)


class _PausedStatusSource:
    """A `GatewayStatusSource` stub that always reports the exchange paused."""

    def get_exchange_status(self) -> ExchangeStatus | None:
        """Return a fixed `"paused"` status reading.

        Returns:
            An `ExchangeStatus` with `status="paused"`.
        """
        return ExchangeStatus(status="paused", fetched_at=_FIXED_FETCHED_AT)


class _StubPositionSource:
    """A mutable, call-recording test-double `GatewayPositionSource`.

    Structurally satisfies `GatewayPositionSource`: `get_positions` returns
    whatever tuple was last set and records every call, so a test can assert
    "the position source was never consulted" (`.calls == 0`) independently
    of the returned snapshot. `set_positions` lets a test grow the reported
    position mid-scenario (e.g. to prove a prior refusal never consumed the
    token) without swapping the `OrderGateway` under test.
    """

    def __init__(self, positions: tuple[Position, ...]) -> None:
        """Initialize with an initial positions snapshot and an empty call log.

        Args:
            positions: The initial tuple of positions to report.
        """
        self._positions = positions
        self.calls = 0

    def get_positions(self) -> tuple[Position, ...]:
        """Record the call and return the currently configured positions.

        Returns:
            The currently configured tuple of positions.
        """
        self.calls += 1
        return self._positions

    def set_positions(self, positions: tuple[Position, ...]) -> None:
        """Replace the configured positions snapshot.

        Args:
            positions: The new tuple of positions subsequent calls report.
        """
        self._positions = positions


class _PlainSubmitter:
    """An `OrderSubmitter` with no `submit_reduce_only` method (no-flag path).

    Records every `submit()` call and returns a `SubmissionAck` that fully
    fills the requested size, so the reduce-only in-flight bookkeeping and
    post-fill re-verification see a deterministic, non-net-short fill.
    """

    def __init__(self) -> None:
        """Initialize with an empty call log."""
        self.calls: list[OrderIntent] = []

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Record the call and return an ack that fully fills `intent.size`.

        Args:
            intent: The intent being submitted.
            token: The approval token accompanying it.

        Returns:
            A `SubmissionAck` whose `filled` mirrors `intent.size`.
        """
        del token
        self.calls.append(intent)
        return SubmissionAck(order_id="plain-order-1", filled=intent.size)


class _CapableSubmitter:
    """An `OrderSubmitter` that also implements `submit_reduce_only`.

    Records the two call streams (`submit_calls` vs. `reduce_only_calls`)
    separately, so a test can assert which path the Gateway took for a given
    intent: `submit_reduce_only` for an admissible close, plain `submit` for
    a `BUY_TO_OPEN`.
    """

    def __init__(self) -> None:
        """Initialize with two empty call logs."""
        self.submit_calls: list[OrderIntent] = []
        self.reduce_only_calls: list[OrderIntent] = []

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Record the call on the plain path and return a full-fill ack.

        Args:
            intent: The intent being submitted.
            token: The approval token accompanying it.

        Returns:
            A `SubmissionAck` whose `filled` mirrors `intent.size`.
        """
        del token
        self.submit_calls.append(intent)
        return SubmissionAck(order_id="capable-order-1", filled=intent.size)

    def submit_reduce_only(
        self, intent: OrderIntent, token: SignedApprovalToken
    ) -> SubmissionAck:
        """Record the call on the reduce-only-flagged path, full-fill ack.

        Args:
            intent: The intent being submitted.
            token: The approval token accompanying it.

        Returns:
            A `SubmissionAck` whose `filled` mirrors `intent.size`.
        """
        del token
        self.reduce_only_calls.append(intent)
        return SubmissionAck(order_id="capable-reduce-only-1", filled=intent.size)


class _ForcedFillSubmitter:
    """An `OrderSubmitter` returning a fixed, caller-chosen fill regardless of
    the requested size -- forcing a post-fill net-short mismatch under test,
    independent of any real venue behavior.
    """

    def __init__(self, filled: ContractCentis) -> None:
        """Initialize with the fixed fill every `submit()` call returns.

        Args:
            filled: The `SubmissionAck.filled` every call returns, regardless
                of the submitted intent's requested size.
        """
        self._filled = filled

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Return the fixed, forced fill, ignoring the requested size.

        Args:
            intent: The intent being submitted (its size is ignored).
            token: The approval token accompanying it.

        Returns:
            A `SubmissionAck` carrying the fixed forced fill.
        """
        del intent, token
        return SubmissionAck(order_id="forced-fill-1", filled=self._filled)


def _build_gateway(
    submitter: OrderSubmitter,
    *,
    position_source: GatewayPositionSource | None,
    ledger_writer: InMemoryGatewayLedgerWriter | None = None,
    status_source: GatewayStatusSource | None = None,
) -> OrderGateway:
    """Build an `OrderGateway` wired for reduce-only tests.

    Args:
        submitter: The seam orders are submitted through.
        position_source: The seam positions are read through, or `None` to
            leave reduce-only enforcement off (the pre-issue-#39 surface).
        ledger_writer: The seam ledger events are recorded through. Defaults
            to a fresh `InMemoryGatewayLedgerWriter`.
        status_source: The seam the exchange status is read through. Defaults
            to `_AlwaysOpenStatusSource`.

    Returns:
        A fully wired `OrderGateway` using the shared deterministic clock and
        signing key from `tests/order_gateway/conftest.py`.
    """
    return OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        clock=lambda: DEFAULT_NOW_EPOCH_S,
        ledger_writer=(
            ledger_writer
            if ledger_writer is not None
            else InMemoryGatewayLedgerWriter()
        ),
        status_source=(
            status_source if status_source is not None else _AlwaysOpenStatusSource()
        ),
        position_source=position_source,
    )


# --- 1. Exact-size close is admitted ------------------------------------------


def test_exact_size_close_acks() -> None:
    """A close exactly matching the held position (500 vs. 500) ACKs."""
    position_source = _StubPositionSource((_position(500),))
    submitter = _PlainSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(500),
        idempotency_key="idem-exact-500",
    )
    token = issue_matching_token(intent)

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.ACKED
    assert result.state is OrderState.ACKED
    assert result.ack is not None
    assert result.ack.filled == ContractCentis(500)
    assert len(submitter.calls) == 1


# --- 2. Under-position close is admitted --------------------------------------


def test_under_position_close_acks() -> None:
    """A close smaller than the held position (300 vs. 500) ACKs."""
    position_source = _StubPositionSource((_position(500),))
    submitter = _PlainSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(300),
        idempotency_key="idem-under-300",
    )
    token = issue_matching_token(intent)

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.ACKED
    assert result.ack is not None
    assert result.ack.filled == ContractCentis(300)
    assert len(submitter.calls) == 1


# --- 3. Oversized close is refused; the token is never consumed --------------


def test_oversized_close_refused_and_token_not_consumed() -> None:
    """An oversized close (600 vs. 500) is refused with a ledgered snapshot
    naming the exact numbers, and the *same* token later verifies OK and ACKs
    once the position source reports enough held quantity -- proof the
    refusal never burned the token's single use.
    """
    position_source = _StubPositionSource((_position(500),))
    submitter = _PlainSubmitter()
    ledger_writer = InMemoryGatewayLedgerWriter()
    gateway = _build_gateway(
        submitter, position_source=position_source, ledger_writer=ledger_writer
    )
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(600),
        idempotency_key="idem-oversized-600",
    )
    token = issue_matching_token(intent)

    refused = gateway.process_intent(intent, token)

    assert refused.outcome is SubmitOutcome.REFUSED_REDUCE_ONLY
    assert refused.state is OrderState.INTENT_CREATED
    assert refused.ack is None
    assert refused.refusal_reason == "reduce_only"
    assert isinstance(refused.position_snapshot, PositionSnapshot)
    assert refused.position_snapshot.ticker == DEFAULT_MARKET_TICKER
    assert refused.position_snapshot.held_centis == 500
    assert refused.position_snapshot.inflight_closing_centis == 0
    assert refused.position_snapshot.requested_close_centis == 600
    assert submitter.calls == []
    refusals = [
        event for event in ledger_writer.events if isinstance(event, ReduceOnlyRefused)
    ]
    assert len(refusals) == 1
    refusal_event = refusals[0]
    assert refusal_event.client_order_id == refused.client_order_id
    assert refusal_event.client_order_id == client_order_id(intent)
    assert refusal_event.ticker == DEFAULT_MARKET_TICKER
    assert refusal_event.held_centis == 500
    assert refusal_event.inflight_closing_centis == 0
    assert refusal_event.requested_close_centis == 600
    assert refusal_event.reason == "reduce_only"

    # The token's single use was never consumed: raising the reported held
    # quantity and re-presenting the *identical* (intent, token) pair now ACKs.
    position_source.set_positions((_position(600),))
    acked = gateway.process_intent(intent, token)

    assert acked.outcome is SubmitOutcome.ACKED
    assert acked.verify_result is VerifyResult.OK


# --- 4. No position for the ticker refuses ------------------------------------


def test_no_position_for_ticker_refuses() -> None:
    """A close against a ticker with no held position (0) is refused."""
    position_source = _StubPositionSource(())
    submitter = _PlainSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(100),
        idempotency_key="idem-no-position",
    )
    token = issue_matching_token(intent)

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.REFUSED_REDUCE_ONLY
    assert result.position_snapshot is not None
    assert result.position_snapshot.held_centis == 0
    assert result.position_snapshot.requested_close_centis == 100
    assert submitter.calls == []


# --- 5. Partial in-flight shrinks the closeable remainder ---------------------


def test_partial_inflight_shrinks_closeable_remainder() -> None:
    """A first 300-centis close against a 500 position leaves only 200
    closeable, so a second, concurrent 300-centis close is refused; the
    second close's token is likewise never consumed by the refusal.
    """
    position_source = _StubPositionSource((_position(500),))
    submitter = _PlainSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)

    first_intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(300),
        idempotency_key="idem-first-300",
    )
    first_token = issue_matching_token(first_intent)
    first_result = gateway.process_intent(first_intent, first_token)
    assert first_result.outcome is SubmitOutcome.ACKED

    second_intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(300),
        idempotency_key="idem-second-300",
    )
    second_token = issue_matching_token(second_intent)
    second_result = gateway.process_intent(second_intent, second_token)

    assert second_result.outcome is SubmitOutcome.REFUSED_REDUCE_ONLY
    assert second_result.position_snapshot is not None
    assert second_result.position_snapshot.held_centis == 500
    assert second_result.position_snapshot.inflight_closing_centis == 300
    assert second_result.position_snapshot.requested_close_centis == 300

    # The second token is intact: once the position grows enough to admit it,
    # the identical (intent, token) pair still ACKs.
    position_source.set_positions((_position(800),))
    retried = gateway.process_intent(second_intent, second_token)

    assert retried.outcome is SubmitOutcome.ACKED
    assert retried.verify_result is VerifyResult.OK


# --- 6. Sequential closes exhaust the position, then refuse -------------------


def test_sequential_closes_exhaust_position_then_refuse() -> None:
    """Three sequential closes (300, 200, 1) against a 500 position ACK the
    first two -- exhausting the position exactly -- and refuse the third.
    """
    position_source = _StubPositionSource((_position(500),))
    submitter = _PlainSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)

    scenarios = (
        (ContractCentis(300), SubmitOutcome.ACKED, "idem-seq-300"),
        (ContractCentis(200), SubmitOutcome.ACKED, "idem-seq-200"),
        (ContractCentis(1), SubmitOutcome.REFUSED_REDUCE_ONLY, "idem-seq-1"),
    )
    for size, expected_outcome, idem_key in scenarios:
        intent = make_intent(
            action="sell_to_close", size=size, idempotency_key=idem_key
        )
        token = issue_matching_token(intent)
        result = gateway.process_intent(intent, token)
        assert result.outcome is expected_outcome

    assert len(submitter.calls) == 2


# --- 7. Post-fill net-short halts the Gateway, fail-closed --------------------


def test_post_fill_net_short_halts_gateway_and_raises() -> None:
    """A submission that fills more than the held position (600 filled vs.
    500 held) ledgers a `ReduceOnlyViolation` and halts the Gateway:
    `GatewayHaltedError` is raised for that call and for every subsequent call.
    """
    position_source = _StubPositionSource((_position(500),))
    submitter = _ForcedFillSubmitter(filled=ContractCentis(600))
    ledger_writer = InMemoryGatewayLedgerWriter()
    gateway = _build_gateway(
        submitter, position_source=position_source, ledger_writer=ledger_writer
    )
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(500),
        idempotency_key="idem-forced-overfill",
    )
    token = issue_matching_token(intent)

    with pytest.raises(GatewayHaltedError):
        gateway.process_intent(intent, token)

    violations = [
        event
        for event in ledger_writer.events
        if isinstance(event, ReduceOnlyViolation)
    ]
    assert len(violations) == 1
    violation = violations[0]
    assert violation.client_order_id == client_order_id(intent)
    assert violation.ticker == DEFAULT_MARKET_TICKER
    assert violation.held_centis == 500
    assert violation.filled_centis == 600
    assert violation.net_centis == -100

    later_intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(1),
        idempotency_key="idem-after-halt",
    )
    later_token = issue_matching_token(later_intent)
    with pytest.raises(GatewayHaltedError):
        gateway.process_intent(later_intent, later_token)


# --- 8. No-flag path: local check still refuses an oversized close -----------


def test_incapable_submitter_still_refuses_oversized_close_locally() -> None:
    """A submitter with no `submit_reduce_only` method (the no-flag venue
    path) still refuses an oversized close via the Gateway's own local check.
    """
    position_source = _StubPositionSource((_position(500),))
    submitter = _PlainSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(600),
        idempotency_key="idem-no-flag-oversized",
    )
    token = issue_matching_token(intent)

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.REFUSED_REDUCE_ONLY
    assert submitter.calls == []


# --- 9. Flag path: capable submitter is flagged for closes, never for opens --


def test_reduce_only_flag_used_for_close_but_never_for_open() -> None:
    """A capable submitter's `submit_reduce_only` is called for an admissible
    close, and never for a `BUY_TO_OPEN`, which always uses plain `submit`.
    """
    position_source = _StubPositionSource((_position(500),))
    submitter = _CapableSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)

    close_intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(300),
        idempotency_key="idem-flag-close",
    )
    close_token = issue_matching_token(close_intent)
    close_result = gateway.process_intent(close_intent, close_token)

    assert close_result.outcome is SubmitOutcome.ACKED
    assert submitter.reduce_only_calls == [close_intent]
    assert submitter.submit_calls == []

    open_intent = make_intent(
        action="buy", size=ContractCentis(100), idempotency_key="idem-flag-open"
    )
    open_token = issue_matching_token(open_intent)
    open_result = gateway.process_intent(open_intent, open_token)

    assert open_result.outcome is SubmitOutcome.ACKED
    assert submitter.submit_calls == [open_intent]
    assert submitter.reduce_only_calls == [close_intent]


# --- 10. BUY_TO_OPEN bypasses reduce-only entirely ----------------------------


def test_buy_to_open_bypasses_reduce_only_and_never_reads_positions() -> None:
    """A `BUY_TO_OPEN` intent ACKs unchanged, never consults the position
    source, and always uses plain `submit` (never `submit_reduce_only`).
    """
    position_source = _StubPositionSource(())
    submitter = _CapableSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)
    intent = make_intent(
        action="buy", size=ContractCentis(200), idempotency_key="idem-buy-bypass"
    )
    token = issue_matching_token(intent)

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.ACKED
    assert result.position_snapshot is None
    assert position_source.calls == 0
    assert submitter.submit_calls == [intent]
    assert submitter.reduce_only_calls == []


# --- 11. No position_source wired: enforcement off, backward compatible ------


def test_no_position_source_wired_oversized_close_still_submits() -> None:
    """Omitting `position_source` (the pre-issue-#39 surface) leaves
    reduce-only enforcement off: an oversized close still submits and ACKs.
    """
    submitter = _PlainSubmitter()
    gateway = _build_gateway(submitter, position_source=None)
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(10_000),
        idempotency_key="idem-no-source",
    )
    token = issue_matching_token(intent)

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.ACKED
    assert result.position_snapshot is None
    assert len(submitter.calls) == 1


# --- 12. Exchange-status gate precedes reduce-only ----------------------------


def test_status_gate_precedes_reduce_only_and_position_source_unread() -> None:
    """A paused exchange refuses on exchange status before reduce-only even
    runs: the position source is never consulted for an oversized close.
    """
    position_source = _StubPositionSource((_position(500),))
    submitter = _PlainSubmitter()
    gateway = _build_gateway(
        submitter,
        position_source=position_source,
        status_source=_PausedStatusSource(),
    )
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(600),
        idempotency_key="idem-status-precedence",
    )
    token = issue_matching_token(intent)

    result = gateway.process_intent(intent, token)

    assert result.outcome is SubmitOutcome.REFUSED_EXCHANGE_STATUS
    assert result.refusal_reason == "paused"
    assert position_source.calls == 0
    assert submitter.calls == []


# --- 13. Replay of an already-ACKed close does not double-count in flight ----


def test_replay_of_acked_close_does_not_double_count_inflight() -> None:
    """Replaying an already-ACKed close returns `IDEMPOTENT_REPLAY` without
    re-running reduce-only, and a fresh, different close still sees the
    closeable remainder shrunk only *once* by the original close -- not
    twice by the replay.
    """
    position_source = _StubPositionSource((_position(500),))
    submitter = _PlainSubmitter()
    gateway = _build_gateway(submitter, position_source=position_source)
    intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(300),
        idempotency_key="idem-replay-close",
    )
    first_token = issue_matching_token(intent, kernel_sequence_number=1)
    acked = gateway.process_intent(intent, first_token)
    assert acked.outcome is SubmitOutcome.ACKED
    assert len(submitter.calls) == 1

    fresh_token = issue_matching_token(intent, kernel_sequence_number=2)
    replay = gateway.process_intent(intent, fresh_token)

    assert replay.outcome is SubmitOutcome.IDEMPOTENT_REPLAY
    assert replay.position_snapshot is None
    assert len(submitter.calls) == 1

    # A second, different close must see only the *first* close's 300 counted
    # as in-flight -- not double-counted by the replay -- so 500-300=200 is
    # still exactly closeable.
    second_intent = make_intent(
        action="sell_to_close",
        size=ContractCentis(200),
        idempotency_key="idem-replay-second",
    )
    second_token = issue_matching_token(second_intent)
    second_result = gateway.process_intent(second_intent, second_token)

    assert second_result.outcome is SubmitOutcome.ACKED
    assert len(submitter.calls) == 2
