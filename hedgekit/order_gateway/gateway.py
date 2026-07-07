"""The Order Gateway (Process C) submission surface and CLI (SPEC S5.1-S5.3).

This module wires the Gateway's runtime surface:

    * :class:`SubmissionAck` / :class:`GatewayResult`: frozen, slotted value
      types carrying a submission receipt and the Gateway's per-intent verdict.
    * :class:`OrderSubmitter`: the structural protocol the Gateway submits
      through, with a real :class:`PaperSubmitter` adapting an
      :class:`~hedgekit.riskkernel.checks.OrderIntent` to the paper exchange's
      :class:`~hedgekit.connector.paper.PaperOrderIntent`.
    * :class:`OrderGateway`: verifies each single-use approval token *before*
      submitting, walking the real :mod:`~hedgekit.order_gateway.state_machine`
      lifecycle only on a verified token, and holding the verification key in a
      private, never-exposed attribute (mirroring the signing side's no-leak
      guarantee, issue #31). The real submission path (issue #38) refuses a
      *brand-new* intent when the exchange is not ``"open"`` (before verifying,
      consuming, or submitting), derives a content-addressed
      :func:`~hedgekit.order_gateway.client_order_id.client_order_id` per intent
      to make resubmission idempotent, and ledgers every state transition
      *before* taking the next action. An *already-acked* intent replays its
      cached ack regardless of the exchange's current status (a pure replay
      never touches the exchange), so the idempotency-cache lookup runs before
      the status gate. Limit-only submission is *structural*:
      an :class:`~hedgekit.riskkernel.checks.OrderIntent` (and the paper
      exchange's :class:`~hedgekit.connector.paper.PaperOrderIntent`) always
      carries a :class:`~hedgekit.numeric.PricePips` price -- there is no
      market-order variant to reject at runtime.
    * :func:`build_parser` / :func:`main`: a bounded ``--max-beats`` /
      ``--heartbeat-interval`` heartbeat CLI mirroring
      :mod:`hedgekit.riskkernel.process`'s conventions.

Per the SPEC S5.3 import boundary, this package is the *sole* legitimate
importer of the exchange order-submission client
(:mod:`hedgekit.connector.paper`); the boundary is enforced by the AST scanner
in ``tests/architecture/test_import_boundaries.py`` and the matching
``plans/architecture/.importlinter`` contract.

Every quantity on the money path is a :mod:`hedgekit.numeric` scaled integer,
never a float (SPEC S6.1); the heartbeat interval is whole seconds.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from hedgekit.connector.paper import PaperOrderIntent
from hedgekit.ledger.events import RecoveryCompleted
from hedgekit.logging_setup import configure_logging
from hedgekit.numeric import ContractCentis
from hedgekit.order_gateway.client_order_id import client_order_id
from hedgekit.order_gateway.ledger_writer import (
    GatewayLedgerWriter,
    LoggingGatewayLedgerWriter,
    ReduceOnlyRefused,
    ReduceOnlyViolation,
    SubmissionRefused,
    apply_and_ledger,
)
from hedgekit.order_gateway.recovery import (
    RecoveryReport,
    TrackedOrder,
    build_unaccounted_halt,
    fold_ledger_states,
    is_closing_action,
    ledger_shows_halt,
    pending_intents,
)
from hedgekit.order_gateway.reduce_only import (
    PositionSnapshot,
    held_for_ticker,
    is_close_admissible,
    is_net_short_after_fill,
)
from hedgekit.order_gateway.state_machine import OrderEvent, OrderState
from hedgekit.order_gateway.tokens import VerifyResult, verify_and_consume
from hedgekit.tokens.verify import InMemorySingleUseRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType
    from typing import Literal

    from hedgekit.connector.models import ExchangeStatus, OpenOrder, Position
    from hedgekit.connector.paper import PaperExchange
    from hedgekit.ledger.store import LedgerRecord
    from hedgekit.order_gateway.recovery import (
        LedgerReaderProtocol,
        ReconciliationSourceProtocol,
    )
    from hedgekit.order_gateway.wal import WalRecord, WriteAheadLogProtocol
    from hedgekit.riskkernel.checks import OrderIntent
    from hedgekit.tokens.verify import SignedApprovalToken, SingleUseRegistry

#: Component label stamped on every log record this process emits.
_COMPONENT = "order_gateway"

#: The environment variable the CLI reads its hex-encoded verification key from.
#: SPEC S10.6 approval tokens are symmetric, so this is the same variable
#: ``hedgekit.riskkernel.signing.SigningKeyHandle.from_env`` signs under -- the
#: same 32 bytes both sign (Risk Kernel) and verify (here).
_KEY_ENV_VAR = "HEDGEKIT_APPROVAL_TOKEN_KEY"

#: Whole seconds between heartbeats when ``--heartbeat-interval`` is omitted. An
#: integer (not a float): the Gateway stays on the no-floats path (SPEC S6.1).
_DEFAULT_HEARTBEAT_INTERVAL = 5

#: The two admissible market outcomes, each mapping to the paper exchange's
#: ``Literal["yes", "no"]`` side. Any other outcome is unroutable.
_ADMISSIBLE_OUTCOMES: frozenset[str] = frozenset({"yes", "no"})

_LOGGER = logging.getLogger("hedgekit.order_gateway")


def _default_clock() -> int:
    """Return the current wall clock as whole epoch seconds.

    Casts :func:`time.time` to an ``int`` so the Gateway's clock stays off the
    banned float path (SPEC S6.1); token ``expires_at`` values are integral.

    Returns:
        The current time, in whole epoch seconds.
    """
    return int(time.time())


@dataclass(frozen=True, slots=True)
class SubmissionAck:
    """The receipt returned by an :class:`OrderSubmitter` after submitting.

    Attributes:
        order_id: The venue's identifier for the resulting resting order, or
            ``None`` when the submission left no resting order behind.
        filled: The quantity filled immediately on submission, in contract-centis.
    """

    order_id: str | None
    filled: ContractCentis


class OrderSubmitter(Protocol):
    """The structural seam through which the Gateway submits a verified order."""

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Submit ``intent`` (authorized by ``token``) and return its receipt.

        Args:
            intent: The verified order intent to submit.
            token: The approval token that authorized it.

        Returns:
            The :class:`SubmissionAck` receipt of the submission.
        """
        ...


def _outcome_to_side(outcome: str) -> Literal["yes", "no"]:
    """Map an intent outcome onto the paper exchange's order side.

    Args:
        outcome: The intent's market outcome.

    Returns:
        ``"yes"`` or ``"no"``, the :class:`~hedgekit.connector.paper.PaperOrderIntent`
        side.

    Raises:
        ValueError: If ``outcome`` is neither ``"yes"`` nor ``"no"``.
    """
    if outcome == "yes":
        return "yes"
    if outcome == "no":
        return "no"
    raise ValueError(
        f"unroutable outcome {outcome!r}; expected one of "
        f"{sorted(_ADMISSIBLE_OUTCOMES)}"
    )


class PaperSubmitter:
    """An :class:`OrderSubmitter` backed by a :class:`PaperExchange`.

    Adapts an :class:`~hedgekit.riskkernel.checks.OrderIntent` into a
    :class:`~hedgekit.connector.paper.PaperOrderIntent`, places it, and projects
    the resulting :class:`~hedgekit.connector.paper.PaperPlacement` into a
    :class:`SubmissionAck`.
    """

    def __init__(self, exchange: PaperExchange) -> None:
        """Bind the submitter to a paper exchange.

        Args:
            exchange: The paper exchange orders are placed against.
        """
        self._exchange = exchange

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Place ``intent`` on the paper exchange and return its receipt.

        The immediate taker-walk fill total (summed across the emitted per-level
        fills) becomes :attr:`SubmissionAck.filled`; the resting remainder's
        order id (or ``None`` when nothing rested) becomes
        :attr:`SubmissionAck.order_id`.

        Args:
            intent: The verified order intent to submit.
            token: The approval token that authorized it (passed through to the
                exchange, which accepts and ignores it in paper mode).

        Returns:
            The :class:`SubmissionAck` receipt.

        Raises:
            ValueError: If ``intent.outcome`` is neither ``"yes"`` nor ``"no"``.
        """
        paper_intent = PaperOrderIntent(
            ticker=intent.market_ticker,
            side=_outcome_to_side(intent.outcome),
            price=intent.price,
            quantity=intent.size,
        )
        placement = self._exchange.place_order(paper_intent, token)
        filled = ContractCentis(sum(fill.quantity.value for fill in placement.fills))
        order_id = (
            placement.resting_order.id if placement.resting_order is not None else None
        )
        return SubmissionAck(order_id=order_id, filled=filled)


class SubmitOutcome(Enum):
    """The disposition of one :meth:`OrderGateway.process_intent` call (issue #38).

    Attributes:
        ACKED: A first submission walked the full state chain to ``ACKED``.
        IDEMPOTENT_REPLAY: A resubmission of an already-submitted intent
            returned the cached ack without submitting again.
        REFUSED_EXCHANGE_STATUS: The exchange was not ``"open"`` (or was
            unreachable), so the intent was refused before verification.
        REFUSED_REDUCE_ONLY: A close exceeded its closeable headroom (held net
            of in-flight closes), so it was refused before verification (issue
            #39).
        REFUSED_RECOVERY_PENDING: Crash recovery has not yet completed, so a
            brand-new intent was refused before verification, without consuming
            its token (issue #40). The identical token ACKs once ``recover()``
            has run.
        REJECTED_TOKEN: Token verification returned a non-``OK`` verdict.
    """

    ACKED = auto()
    IDEMPOTENT_REPLAY = auto()
    REFUSED_EXCHANGE_STATUS = auto()
    REFUSED_REDUCE_ONLY = auto()
    REFUSED_RECOVERY_PENDING = auto()
    REJECTED_TOKEN = auto()


class GatewayStatusSource(Protocol):
    """The seam the Gateway reads the exchange's trading status through."""

    def get_exchange_status(self) -> ExchangeStatus | None:
        """Return the exchange's current trading status.

        Returns:
            The current :class:`~hedgekit.connector.models.ExchangeStatus`, or
            ``None`` when the status is unknown (the exchange is unreachable),
            which the Gateway treats as a refusal.
        """
        ...


class GatewayPositionSource(Protocol):
    """The seam the Gateway reads live open positions through (issue #39).

    Consulted only when reduce-only enforcement is on (a source is wired) and
    only for a *closing* intent; a :class:`~hedgekit.connector.models.Position`
    tuple feeds the reduce-only admission math in
    :mod:`hedgekit.order_gateway.reduce_only`.

    Timing contract (load-bearing, see
    :func:`~hedgekit.order_gateway.reduce_only.is_net_short_after_fill`): the
    returned ``held`` must *lag* the fill of a close currently being placed --
    the in-process ``_inflight_closing`` tally, not this source, accounts for a
    close between its placement and the source catching up. A source that
    reflected a just-placed fill immediately would false-positive the post-fill
    net-short check on every normal full close. Enforcement is off by default
    (no source wired); durable, fill-reconciled position reads are issue #40's
    job.
    """

    def get_positions(self) -> tuple[Position, ...]:
        """Return the currently held open positions.

        Returns:
            The open positions, one (or more) row per held ticker.
        """
        ...


@runtime_checkable
class ReduceOnlyCapableSubmitter(Protocol):
    """An :class:`OrderSubmitter` that can flag a close reduce-only venue-side.

    Runtime-checkable so the Gateway can prefer :meth:`submit_reduce_only` for a
    closing intent when the wired submitter supports it, falling back to plain
    :meth:`OrderSubmitter.submit` otherwise. The Gateway's own local size check
    runs regardless of which path is taken.
    """

    def submit_reduce_only(
        self, intent: OrderIntent, token: SignedApprovalToken
    ) -> SubmissionAck:
        """Submit ``intent`` with the venue's reduce-only flag set.

        Args:
            intent: The verified closing intent to submit.
            token: The approval token that authorized it.

        Returns:
            The :class:`SubmissionAck` receipt of the submission.
        """
        ...


class GatewayHaltedError(Exception):
    """Raised when a post-fill net-short invariant breach halts the Gateway.

    A close that filled more than was held would leave the position net-short
    (SPEC S11.5). The Gateway fails closed: it ledgers a
    :class:`~hedgekit.order_gateway.ledger_writer.ReduceOnlyViolation`, latches a
    halted flag, and raises this for the offending call and every subsequent
    one. It is deliberately never caught on the money path, so the halt cannot
    be silently suppressed.
    """


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """The Gateway's verdict for one processed intent.

    Attributes:
        verify_result: The token-verification verdict, or ``None`` when the
            intent was refused before verification (exchange not open).
        state: The lifecycle state the order reached (``INTENT_CREATED`` when
            verification failed or the exchange was not open and no submission
            occurred; ``ACKED`` on the happy path or an idempotent replay).
        ack: The submission receipt on the happy path or a replay, else
            ``None``.
        outcome: The call's disposition (issue #38), or ``None`` on a bare
            pre-issue-#38 construction.
        refusal_reason: The exchange status (or ``"unknown"``) that caused a
            ``REFUSED_EXCHANGE_STATUS`` outcome, ``"reduce_only"`` on a
            ``REFUSED_REDUCE_ONLY`` outcome, or ``"recovery_pending"`` on a
            ``REFUSED_RECOVERY_PENDING`` outcome (issue #40), else ``None``.
        client_order_id: The content-addressed id derived for the intent, or
            ``None`` on a bare pre-issue-#38 construction.
        position_snapshot: The reduce-only justification snapshot on a
            ``REFUSED_REDUCE_ONLY`` outcome (the held/in-flight/requested counts
            the refusal was computed from), else ``None`` (issue #39).
    """

    verify_result: VerifyResult | None
    state: OrderState
    ack: SubmissionAck | None
    outcome: SubmitOutcome | None = None
    refusal_reason: str | None = None
    client_order_id: str | None = None
    position_snapshot: PositionSnapshot | None = None


class _UnwiredSubmitter:
    """A placeholder :class:`OrderSubmitter` for the credential-free heartbeat CLI.

    The bounded heartbeat loop (:meth:`OrderGateway.run`) never submits, so the
    CLI wires the Gateway to this stand-in rather than a real trading client --
    mirroring how :mod:`hedgekit.riskkernel.process`'s ``main`` wires a
    ``LoggingKernelLedgerWriter`` stand-in. A future submission client carrying
    trade credentials replaces it; until then any actual call fails closed.
    """

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Fail closed: this stand-in must never actually submit an order.

        Args:
            intent: The intent that would be submitted.
            token: The approval token that would authorize it.

        Returns:
            Never returns normally.

        Raises:
            NotImplementedError: Always -- no trade-credential client is wired.
        """
        del intent, token
        raise NotImplementedError("no order-submission client is wired for the CLI")


class OrderGateway:
    """Verifies approval tokens and submits the orders they authorize.

    The verification key is held only in a private slot: no public, non-callable
    attribute ever exposes the raw bytes, mirroring
    :class:`~hedgekit.riskkernel.signing.SigningKeyHandle`'s no-leak guarantee
    (issue #31). ``__slots__`` keeps the instance ``__dict__``-free as defense in
    depth.

    A ``None`` ``status_source`` turns exchange-status gating *off*, which is
    correct for the credential-free heartbeat CLI and the issue #37 tests;
    production wiring **must** supply a real source so a paused/closed exchange
    refuses submission. Likewise a ``None`` ``position_source`` turns reduce-only
    enforcement *off* (issue #39, backward compatible); production wiring **must**
    supply a real source so a ``SELL_TO_CLOSE`` can never grow a position past
    flat, and a post-fill net-short halts the Gateway fail-closed
    (:class:`GatewayHaltedError`). The ``client_order_id -> ack`` cache is
    per-instance and in-memory: it makes resubmission idempotent within one
    process lifetime, but
    restart durability is issue #40's job -- the content-addressed
    :func:`~hedgekit.order_gateway.client_order_id.client_order_id` is the
    enabler for that later crash-recovery join.
    """

    __slots__ = (
        "_accepting_approvals",
        "_acks",
        "_clock",
        "_halted",
        "_inflight_closing",
        "_ledger_reader",
        "_ledger_writer",
        "_position_source",
        "_reconciliation_source",
        "_registry",
        "_status_source",
        "_submitter",
        "_tracked",
        "_verification_key",
        "_wal",
    )

    def __init__(
        self,
        submitter: OrderSubmitter,
        *,
        verification_key: bytes,
        registry: SingleUseRegistry | None = None,
        clock: Callable[[], int] | None = None,
        ledger_writer: GatewayLedgerWriter | None = None,
        status_source: GatewayStatusSource | None = None,
        position_source: GatewayPositionSource | None = None,
        wal: WriteAheadLogProtocol | None = None,
        ledger_reader: LedgerReaderProtocol | None = None,
        reconciliation_source: ReconciliationSourceProtocol | None = None,
    ) -> None:
        """Initialize the Gateway.

        Args:
            submitter: The seam verified orders are submitted through.
            verification_key: The shared HMAC key approval tokens verify under.
                Stored privately and never exposed.
            registry: The single-use registry gating replay. Defaults to a fresh
                :class:`~hedgekit.tokens.verify.InMemorySingleUseRegistry`.
            clock: A zero-argument callable returning the current epoch second,
                injected so verification is deterministic under test. Defaults to
                :func:`_default_clock` (real wall clock).
            ledger_writer: The seam every transition and refusal is recorded
                through. Defaults to a fresh :class:`LoggingGatewayLedgerWriter`.
            status_source: The seam the exchange trading status is read through.
                ``None`` (the default) turns status gating off; production
                wiring must supply a real source.
            position_source: The seam live positions are read through for
                reduce-only enforcement (issue #39). ``None`` (the default)
                turns reduce-only enforcement off (backward compatible with the
                pre-issue-#39 surface); production wiring must supply a real
                source so a close can never grow a position past flat.
            wal: The durable write-ahead log intents and acks are journalled
                through for crash recovery (issue #40). ``None`` (the default)
                disables journalling.
            ledger_reader: The seam the durable ledger is folded back through on
                :meth:`recover`. ``None`` (the default) leaves nothing to fold.
            reconciliation_source: The seam the venue's live open orders/fills
                are read through on :meth:`recover` and by the Reconciler.
                ``None`` (the default) leaves nothing to reconcile against.

        When *any* of ``wal``/``ledger_reader``/``reconciliation_source`` is
        wired, the Gateway starts with ``accepting_approvals`` ``False`` and
        refuses every brand-new intent ``REFUSED_RECOVERY_PENDING`` until
        :meth:`recover` completes (issue #40). Wiring none of them preserves the
        pre-issue-#40 surface exactly: approvals are accepted immediately and
        :meth:`recover` is a harmless no-op.
        """
        self._submitter = submitter
        self._verification_key = verification_key
        self._registry: SingleUseRegistry = (
            registry if registry is not None else InMemorySingleUseRegistry()
        )
        self._clock = clock if clock is not None else _default_clock
        self._ledger_writer: GatewayLedgerWriter = (
            ledger_writer if ledger_writer is not None else LoggingGatewayLedgerWriter()
        )
        self._status_source = status_source
        self._position_source = position_source
        self._wal = wal
        self._ledger_reader = ledger_reader
        self._reconciliation_source = reconciliation_source
        self._acks: dict[str, SubmissionAck] = {}
        self._tracked: dict[str, TrackedOrder] = {}
        self._inflight_closing: dict[str, ContractCentis] = {}
        self._halted = False
        self._accepting_approvals = not self._recovery_wired

    @property
    def _recovery_wired(self) -> bool:
        """Return whether any crash-recovery dependency is wired (issue #40).

        Returns:
            ``True`` iff a ``wal``, ``ledger_reader``, or
            ``reconciliation_source`` was supplied at construction.
        """
        return (
            self._wal is not None
            or self._ledger_reader is not None
            or self._reconciliation_source is not None
        )

    @property
    def accepting_approvals(self) -> bool:
        """Return whether the Gateway is accepting brand-new intents (issue #40).

        ``False`` from construction until :meth:`recover` completes when any
        recovery dependency is wired, and permanently ``False`` once a halt
        stands. ``True`` on a Gateway with no recovery dependencies.

        Returns:
            Whether a brand-new intent would be admitted rather than refused
            ``REFUSED_RECOVERY_PENDING``.
        """
        return self._accepting_approvals

    @property
    def halted(self) -> bool:
        """Return whether the Gateway has fail-closed halted (issue #39/#40).

        Returns:
            ``True`` once a reduce-only net-short breach or a reconciliation
            mismatch has latched the Gateway halted; there is no un-halt.
        """
        return self._halted

    def process_intent(
        self, intent: OrderIntent, token: SignedApprovalToken
    ) -> GatewayResult:
        """Process ``intent`` under ``token``, gating, verifying, then submitting.

        The pipeline is strictly check-then-act, and the idempotency cache is
        consulted *before* the exchange-status gate so a pure replay (which
        never touches the exchange) is never blocked by the exchange's current
        status. Concretely:

        1. A *brand-new* intent (one whose
           :func:`~hedgekit.order_gateway.client_order_id.client_order_id` is
           not already cached) is refused *before* the token is verified or
           consumed -- and before the submitter is ever called -- when a status
           source is wired and the exchange is not ``"open"`` (or is
           unreachable), ledgering one :class:`SubmissionRefused`. A closed
           exchange therefore never burns a brand-new intent's token.
        2. An *already-acked* intent bypasses that status gate entirely: because
           its order is already resting on the exchange, replaying its cached
           ack requires no exchange interaction, so a later "did this go
           through?" retry against a now-paused/closed/unreachable exchange
           still returns the truthful cached ack rather than a misleading
           ``REFUSED_EXCHANGE_STATUS``.
        3. In both admitted cases the token is verified (and its single use
           consumed) *before* any cached ack is disclosed: a non-``OK`` verdict
           short-circuits with no submission and no ack disclosure. Only on an
           ``OK`` verdict does a cached intent replay its ack; a first
           submission walks the real
           ``APPROVE -> REQUEST_SUBMISSION -> (submit) -> SUBMIT -> ACK`` chain,
           ledgering each transition before the next action.

        The cache is read for *membership only* before verification (to route
        replay vs. refuse); the cached ack itself is never returned until the
        accompanying token has verified ``OK``, preserving the no-pre-auth-ack
        -disclosure guarantee.

        Between the status gate and verification, a brand-new *closing* intent
        also passes the reduce-only gate (issue #39, when a position source is
        wired): an oversized close is refused -- likewise before the token is
        verified or consumed -- so the exchange-status refusal still takes
        precedence over reduce-only. If a prior call latched a post-fill
        net-short halt, this call fails closed immediately.

        Args:
            intent: The order intent to process.
            token: The accompanying single-use approval token.

        Returns:
            The :class:`GatewayResult` verdict.

        Raises:
            GatewayHaltedError: If a prior post-fill net-short breach latched the
                Gateway halted, or if this call itself breaches that invariant
                (issue #39). Once halted, every subsequent call fails closed.
        """
        if self._halted:
            raise GatewayHaltedError(
                "gateway halted after a prior reduce-only net-short violation"
            )
        coid = client_order_id(intent)
        refusal = self._gate_before_verify(intent, coid)
        if refusal is not None:
            return refusal
        verify_result = verify_and_consume(
            token,
            intent,
            key=self._verification_key,
            now_epoch_s=self._clock(),
            registry=self._registry,
        )
        if verify_result is not VerifyResult.OK:
            return GatewayResult(
                verify_result=verify_result,
                state=OrderState.INTENT_CREATED,
                ack=None,
                outcome=SubmitOutcome.REJECTED_TOKEN,
                client_order_id=coid,
            )
        if coid in self._acks:
            return self._replay(coid)
        return self._submit_new(intent, token, coid)

    def _gate_before_verify(
        self, intent: OrderIntent, coid: str
    ) -> GatewayResult | None:
        """Run every pre-verification gate, returning the first refusal or None.

        The gates run in fixed precedence -- recovery-pending, then exchange
        status, then reduce-only -- and each refuses *before* the token is
        verified or consumed, so no refused intent ever burns its token's single
        use. An already-acked intent (its ``coid`` is cached) bypasses the status
        and reduce-only gates so a pure replay is never blocked by the exchange's
        current status.

        Args:
            intent: The order intent being processed.
            coid: The intent's content-addressed client-order-id.

        Returns:
            The first gate's refusal :class:`GatewayResult`, or ``None`` to admit
            the intent to token verification.
        """
        if not self._accepting_approvals:
            return self._refuse_recovery_pending(coid)
        if self._status_source is not None and coid not in self._acks:
            status = self._status_source.get_exchange_status()
            if status is None or status.status != "open":
                return self._refuse_status(coid, status)
        if coid not in self._acks:
            return self._reduce_only_gate(intent, coid)
        return None

    def _refuse_status(self, coid: str, status: ExchangeStatus | None) -> GatewayResult:
        """Ledger a refusal for a non-open exchange and return the verdict.

        Args:
            coid: The intent's content-addressed client-order-id.
            status: The observed exchange status, or ``None`` when unreachable.

        Returns:
            A ``REFUSED_EXCHANGE_STATUS`` :class:`GatewayResult` still in
            ``INTENT_CREATED`` with no ack; the token is never consumed.
        """
        reason = status.status if status is not None else "unknown"
        self._ledger_writer.record(
            SubmissionRefused(
                component=_COMPONENT,
                client_order_id=coid,
                reason=reason,
            )
        )
        return GatewayResult(
            verify_result=None,
            state=OrderState.INTENT_CREATED,
            ack=None,
            outcome=SubmitOutcome.REFUSED_EXCHANGE_STATUS,
            refusal_reason=reason,
            client_order_id=coid,
        )

    def _refuse_recovery_pending(self, coid: str) -> GatewayResult:
        """Ledger a recovery-pending refusal and return the verdict (issue #40).

        Args:
            coid: The intent's content-addressed client-order-id.

        Returns:
            A ``REFUSED_RECOVERY_PENDING`` :class:`GatewayResult` still in
            ``INTENT_CREATED`` with no ack; the token is never verified or
            consumed, so the identical token ACKs once :meth:`recover` has run.
        """
        self._ledger_writer.record(
            SubmissionRefused(
                component=_COMPONENT,
                client_order_id=coid,
                reason="recovery_pending",
            )
        )
        return GatewayResult(
            verify_result=None,
            state=OrderState.INTENT_CREATED,
            ack=None,
            outcome=SubmitOutcome.REFUSED_RECOVERY_PENDING,
            refusal_reason="recovery_pending",
            client_order_id=coid,
        )

    def _inflight_for(self, ticker: str) -> int:
        """Return the in-flight-closing total for ``ticker``, in centis.

        Args:
            ticker: The market ticker to read the in-flight-closing total for.

        Returns:
            The sum of closes already in flight for ``ticker``, or ``0`` when
            none are.
        """
        current = self._inflight_closing.get(ticker)
        return current.value if current is not None else 0

    def _reduce_only_gate(self, intent: OrderIntent, coid: str) -> GatewayResult | None:
        """Refuse a brand-new close that would exceed its closeable headroom.

        A no-op (returns ``None``, and never reads the position source) unless
        reduce-only enforcement is on (a source is wired) *and* the intent is a
        close. Otherwise it reads live positions, computes the held quantity net
        of in-flight closes, and refuses when the requested close overshoots --
        *before* the token is verified or consumed, so a refusal never burns the
        token's single use (mirroring :meth:`_refuse_status`).

        Args:
            intent: The intent being processed.
            coid: The intent's content-addressed client-order-id.

        Returns:
            A ``REFUSED_REDUCE_ONLY`` :class:`GatewayResult` when the close is
            inadmissible, else ``None`` to admit it to verification.
        """
        if self._position_source is None or not is_closing_action(intent.action):
            return None
        positions = self._position_source.get_positions()
        held = held_for_ticker(positions, intent.market_ticker)
        inflight = self._inflight_for(intent.market_ticker)
        if is_close_admissible(intent.size.value, held, inflight):
            return None
        snapshot = PositionSnapshot(
            ticker=intent.market_ticker,
            held_centis=held,
            inflight_closing_centis=inflight,
            requested_close_centis=intent.size.value,
        )
        return self._refuse_reduce_only(coid, snapshot)

    def _refuse_reduce_only(
        self, coid: str, snapshot: PositionSnapshot
    ) -> GatewayResult:
        """Ledger a reduce-only refusal and return the verdict.

        Args:
            coid: The intent's content-addressed client-order-id.
            snapshot: The held/in-flight/requested justification the refusal was
                computed from, ledgered verbatim and returned on the verdict.

        Returns:
            A ``REFUSED_REDUCE_ONLY`` :class:`GatewayResult` still in
            ``INTENT_CREATED`` with no ack; the token is never consumed.
        """
        self._ledger_writer.record(
            ReduceOnlyRefused(
                component=_COMPONENT,
                client_order_id=coid,
                ticker=snapshot.ticker,
                held_centis=snapshot.held_centis,
                inflight_closing_centis=snapshot.inflight_closing_centis,
                requested_close_centis=snapshot.requested_close_centis,
                reason="reduce_only",
            )
        )
        return GatewayResult(
            verify_result=None,
            state=OrderState.INTENT_CREATED,
            ack=None,
            outcome=SubmitOutcome.REFUSED_REDUCE_ONLY,
            refusal_reason="reduce_only",
            client_order_id=coid,
            position_snapshot=snapshot,
        )

    def _place(
        self, intent: OrderIntent, token: SignedApprovalToken, *, closing: bool
    ) -> SubmissionAck:
        """Submit ``intent``, preferring the reduce-only flag for a close.

        Args:
            intent: The verified intent to submit.
            token: The approval token authorizing it.
            closing: Whether ``intent`` closes a position; when the wired
                submitter is reduce-only capable, a close is flagged
                venue-side via :meth:`ReduceOnlyCapableSubmitter.submit_reduce_only`.

        Returns:
            The :class:`SubmissionAck` receipt.
        """
        if closing and isinstance(self._submitter, ReduceOnlyCapableSubmitter):
            return self._submitter.submit_reduce_only(intent, token)
        return self._submitter.submit(intent, token)

    def _record_close_fill(
        self, intent: OrderIntent, coid: str, ack: SubmissionAck
    ) -> None:
        """Book a just-placed close's in-flight tally and re-verify net-flat.

        Adds the close's size to the ticker's in-flight-closing total (shrinking
        the closeable remainder for the next concurrent close), then -- when a
        position source is wired -- re-reads the held quantity and halts the
        Gateway fail-closed if the venue overshot the position into a net-short
        (SPEC S11.5).

        Within-process, the in-flight tally is only ever *incremented* here; it
        is *retired* out-of-band -- on :meth:`recover` (rebuilt from the WAL:
        only closes whose venue order is still resting keep counting) and by the
        Reconciler when a close is confirmed filled (issue #40). The bias is
        fail-safe (a momentarily stale tally over-refuses legitimate closes and
        never admits a net-short). The halt latch is likewise durable across a
        restart: :meth:`recover` folds a persisted ``ReduceOnlyViolation`` and
        stays halted (issue #40). This class assumes strictly *sequential*
        :meth:`process_intent` calls (overlapping order lifecycles, not
        concurrent threads); the read-then-write here is not synchronized, so a
        real multi-threaded caller would need external locking.

        Args:
            intent: The close that was just placed.
            coid: The intent's content-addressed client-order-id.
            ack: The submission receipt whose ``filled`` is re-verified.

        Raises:
            GatewayHaltedError: If the fill left the position net-short.
        """
        ticker = intent.market_ticker
        self._inflight_closing[ticker] = ContractCentis(
            self._inflight_for(ticker) + intent.size.value
        )
        if self._position_source is None:
            return
        held = held_for_ticker(self._position_source.get_positions(), ticker)
        if is_net_short_after_fill(held, ack.filled.value):
            self._halt_on_violation(intent, coid, ack, held)

    def _halt_on_violation(
        self, intent: OrderIntent, coid: str, ack: SubmissionAck, held: int
    ) -> None:
        """Ledger a net-short violation, latch the halt, and fail closed.

        Args:
            intent: The close whose fill breached the net-flat invariant.
            coid: The intent's content-addressed client-order-id.
            ack: The submission receipt carrying the overshooting fill.
            held: The net held quantity re-read after the fill, in centis.

        Raises:
            GatewayHaltedError: Always -- the Gateway is now halted, fail-closed.
        """
        self._ledger_writer.record(
            ReduceOnlyViolation(
                component=_COMPONENT,
                client_order_id=coid,
                ticker=intent.market_ticker,
                held_centis=held,
                filled_centis=ack.filled.value,
                net_centis=held - ack.filled.value,
            )
        )
        self._halted = True
        raise GatewayHaltedError(
            "reduce-only net-short violation: close "
            f"{coid} filled {ack.filled.value} against held {held}"
        )

    def _replay(self, coid: str) -> GatewayResult:
        """Return the cached ack for an already-submitted intent, unchanged.

        Args:
            coid: The intent's content-addressed client-order-id, known to be
                present in the ack cache.

        Returns:
            An ``IDEMPOTENT_REPLAY`` :class:`GatewayResult` carrying the cached
            ack; the submitter is not called and no transition is ledgered.
        """
        return GatewayResult(
            verify_result=VerifyResult.OK,
            state=OrderState.ACKED,
            ack=self._acks[coid],
            outcome=SubmitOutcome.IDEMPOTENT_REPLAY,
            client_order_id=coid,
        )

    def _submit_new(
        self, intent: OrderIntent, token: SignedApprovalToken, coid: str
    ) -> GatewayResult:
        """Walk the full submission chain, ledgering before each next action.

        The two *pre-submit* transitions are recorded before the action they
        authorize: the ``REQUEST_SUBMISSION`` write lands before ``submit`` is
        called, so a ledger failure there cannot leave a resting order behind
        (the exchange is never touched). The ack is cached the instant
        ``submit`` returns -- before the ``SUBMIT``/``ACK`` writes -- because
        those two writes only *record* a placement that already happened and
        authorize no further exchange action. Caching immediately makes a
        *post-submit* ledger failure replay-safe: the order is real and cached,
        so a retry short-circuits to ``IDEMPOTENT_REPLAY`` rather than placing a
        duplicate. Caching only after the ``ACK`` write would instead leave an
        order resting on the exchange but absent from the cache, and a retry
        would double-submit.

        A *closing* intent additionally books its size into the ticker's
        in-flight-closing tally and re-verifies the post-fill position, after
        the ack is cached, so the placed order is recorded even if the re-verify
        then halts the Gateway (issue #39). An opening intent skips all of that.

        Crash durability (issue #40): the intent is journalled to the write-ahead
        log *before* the ``REQUEST_SUBMISSION`` transition, and the ack the
        instant ``submit`` returns (before the ``SUBMIT`` write), so a crash at
        any durable point along the chain leaves a fresh Gateway's
        :meth:`recover` enough truth to reconcile without double-submitting.
        Both writes are no-ops when no write-ahead log is wired.

        Args:
            intent: The order intent to submit.
            token: The approval token authorizing it.
            coid: The intent's content-addressed client-order-id.

        Returns:
            An ``ACKED`` :class:`GatewayResult` carrying the submission receipt.

        Raises:
            GatewayHaltedError: If a closing intent's fill left the position
                net-short, halting the Gateway fail-closed.
        """
        self._wal_append_intent(intent, coid)
        state = apply_and_ledger(
            self._ledger_writer,
            OrderState.INTENT_CREATED,
            OrderEvent.APPROVE,
            client_order_id=coid,
        )
        state = apply_and_ledger(
            self._ledger_writer,
            state,
            OrderEvent.REQUEST_SUBMISSION,
            client_order_id=coid,
        )
        closing = is_closing_action(intent.action)
        ack = self._place(intent, token, closing=closing)
        self._wal_append_ack(coid, ack)
        self._acks[coid] = ack
        self._track_if_resting(coid, intent, ack)
        state = apply_and_ledger(
            self._ledger_writer, state, OrderEvent.SUBMIT, client_order_id=coid
        )
        state = apply_and_ledger(
            self._ledger_writer, state, OrderEvent.ACK, client_order_id=coid
        )
        if closing:
            self._record_close_fill(intent, coid, ack)
        return GatewayResult(
            verify_result=VerifyResult.OK,
            state=state,
            ack=ack,
            outcome=SubmitOutcome.ACKED,
            client_order_id=coid,
        )

    def _wal_append_intent(self, intent: OrderIntent, coid: str) -> None:
        """Journal ``intent`` to the write-ahead log (no-op if none wired).

        Args:
            intent: The intent to journal.
            coid: The intent's content-addressed client-order-id.
        """
        if self._wal is not None:
            self._wal.append_intent(intent, coid)

    def _wal_append_ack(self, coid: str, ack: SubmissionAck) -> None:
        """Journal a placement's ack to the write-ahead log (no-op if none).

        Args:
            coid: The intent's content-addressed client-order-id.
            ack: The submission receipt whose venue id and fill are journalled.
        """
        if self._wal is not None:
            self._wal.append_ack(coid, ack.order_id, ack.filled)

    def _track_if_resting(
        self, coid: str, intent: OrderIntent, ack: SubmissionAck
    ) -> None:
        """Record a tracked order for a placement that left something resting.

        A fully-filled placement (``ack.order_id is None``) rests nothing and is
        never tracked; a resting placement is recorded so the Reconciler can
        later diff it against the venue (issue #40).

        Args:
            coid: The intent's content-addressed client-order-id.
            intent: The placed intent, supplying the order's economic profile.
            ack: The submission receipt carrying the venue id and taker fill.
        """
        if ack.order_id is None:
            return
        self._tracked[ack.order_id] = TrackedOrder(
            client_order_id=coid,
            order_id=ack.order_id,
            ticker=intent.market_ticker,
            side=_outcome_to_side(intent.outcome),
            price_pips=intent.price.value,
            size_centis=intent.size.value,
            action=intent.action,
            filled_centis=ack.filled.value,
        )

    def recover(self) -> RecoveryReport:
        """Reconcile the durable ledger/WAL against the venue, then open the gate.

        Runs the SPEC S11.4 recovery sequence in order: load the ledger, fold its
        durable halt latch (a prior ``ReduceOnlyViolation`` or
        ``ReconciliationHalted`` keeps the Gateway halted -- there is no un-halt
        event), rehydrate the ack cache / tracked orders / in-flight-closing
        tally from the write-ahead log, then diff the venue's resting orders. A
        resting order with no durable ack halts recovery (``foreign_open_order``
        or ``ambiguous_match``) rather than guess. Only a clean reconciliation
        ledgers a :class:`~hedgekit.ledger.events.RecoveryCompleted` and flips
        ``accepting_approvals`` to ``True``; recovery never leaves the Gateway
        accepting while a halt stands. A Gateway with no recovery dependencies
        wired is a harmless no-op that stays open.

        Returns:
            The :class:`~hedgekit.order_gateway.recovery.RecoveryReport` summary.
        """
        if not self._recovery_wired:
            return RecoveryReport(orders_reconciled=0, halted=False)
        records = self._ledger_reader.read_all() if self._ledger_reader else []
        if ledger_shows_halt(records):
            self.mark_halted()
            return RecoveryReport(orders_reconciled=0, halted=True)
        wal_records = self._wal.read_all() if self._wal is not None else ()
        open_orders = (
            self._reconciliation_source.get_open_orders()
            if self._reconciliation_source is not None
            else ()
        )
        reconciled = self._rehydrate_from_wal(records, wal_records, open_orders)
        halt = build_unaccounted_halt(
            open_orders, frozenset(self._tracked), pending_intents(wal_records)
        )
        if halt is not None:
            self._ledger_writer.record(halt)
            self.mark_halted()
            return RecoveryReport(orders_reconciled=reconciled, halted=True)
        self._ledger_writer.record(
            RecoveryCompleted(
                component=_COMPONENT, orders_reconciled=reconciled, halted=False
            )
        )
        self._accepting_approvals = True
        return RecoveryReport(orders_reconciled=reconciled, halted=False)

    def _rehydrate_from_wal(
        self,
        records: list[LedgerRecord],
        wal_records: tuple[WalRecord, ...],
        open_orders: tuple[OpenOrder, ...],
    ) -> int:
        """Rebuild in-memory state from the write-ahead log and ledger.

        Rehydrates the ack cache, tracked resting orders, and in-flight-closing
        tally, and completes each adopted order's ledgered lifecycle to
        ``ACKED``.

        Args:
            records: The durable ledger records.
            wal_records: The write-ahead log records.
            open_orders: The venue's currently resting orders.

        Returns:
            The number of acked orders rehydrated.
        """
        open_ids = frozenset(order.id for order in open_orders)
        intents: dict[str, OrderIntent] = {}
        for rec in wal_records:
            if rec.kind == "intent" and rec.intent is not None:
                intents[rec.client_order_id] = rec.intent
        ledger_states = fold_ledger_states(records)
        reconciled = 0
        for rec in wal_records:
            if rec.kind != "ack":
                continue
            self._rehydrate_ack(rec, intents, open_ids, ledger_states)
            reconciled += 1
        return reconciled

    def _rehydrate_ack(
        self,
        rec: WalRecord,
        intents: dict[str, OrderIntent],
        open_ids: frozenset[str],
        ledger_states: dict[str, OrderState],
    ) -> None:
        """Rehydrate one ack: cache it, complete its ledger, track it if resting.

        Args:
            rec: The ack write-ahead record.
            intents: The journalled intents, keyed by client-order-id.
            open_ids: The venue order ids still resting.
            ledger_states: Each coid's latest ledgered lifecycle state.
        """
        coid = rec.client_order_id
        self._acks[coid] = SubmissionAck(order_id=rec.order_id, filled=rec.filled)
        self._complete_ledger_to_acked(coid, ledger_states.get(coid))
        order_id = rec.order_id
        intent = intents.get(coid)
        if order_id is not None and order_id in open_ids and intent is not None:
            self._track_recovered_order(coid, order_id, intent, rec.filled.value)

    def _track_recovered_order(
        self, coid: str, order_id: str, intent: OrderIntent, filled_centis: int
    ) -> None:
        """Rebuild a tracked resting order and its in-flight-closing share.

        A still-resting close keeps shrinking the closeable remainder across a
        restart; a settled close (its venue order gone) is retired by simply not
        being rehydrated here (issue #39/#40).

        Args:
            coid: The order's content-addressed client-order-id.
            order_id: The venue's resting-order id.
            intent: The journalled intent supplying the economic profile.
            filled_centis: The quantity attributed to the order at placement.
        """
        self._tracked[order_id] = TrackedOrder(
            client_order_id=coid,
            order_id=order_id,
            ticker=intent.market_ticker,
            side=_outcome_to_side(intent.outcome),
            price_pips=intent.price.value,
            size_centis=intent.size.value,
            action=intent.action,
            filled_centis=filled_centis,
        )
        if is_closing_action(intent.action):
            self._inflight_closing[intent.market_ticker] = ContractCentis(
                self._inflight_for(intent.market_ticker) + intent.size.value
            )

    def _complete_ledger_to_acked(self, coid: str, state: OrderState | None) -> None:
        """Advance an adopted order's ledgered lifecycle up to ``ACKED``.

        Walks only the legal remaining edges from the durable state, so the
        recovered chain stays a legal, continuous state-machine history. A coid
        already at (or past) ``ACKED``, or with no ledgered transition, is a
        no-op.

        Args:
            coid: The order's content-addressed client-order-id.
            state: The coid's latest ledgered state, or ``None``.
        """
        if state is OrderState.SUBMISSION_REQUESTED:
            submitted = apply_and_ledger(
                self._ledger_writer, state, OrderEvent.SUBMIT, client_order_id=coid
            )
            apply_and_ledger(
                self._ledger_writer, submitted, OrderEvent.ACK, client_order_id=coid
            )
        elif state is OrderState.SUBMITTED:
            apply_and_ledger(
                self._ledger_writer, state, OrderEvent.ACK, client_order_id=coid
            )

    def tracked_orders(self) -> tuple[TrackedOrder, ...]:
        """Return every currently tracked resting order (Reconciler seam).

        Returns:
            The Gateway-placed orders still believed to be resting on the venue,
            the unit the :class:`~hedgekit.order_gateway.reconciler.Reconciler`
            diffs against the venue's live truth (issue #40).
        """
        return tuple(self._tracked.values())

    def mark_halted(self) -> None:
        """Latch the Gateway fail-closed (Reconciler/recovery seam, issue #40).

        Sets the halt latch and stops accepting approvals; there is no un-halt.
        """
        self._halted = True
        self._accepting_approvals = False

    def retire_tracked_order(self, order: TrackedOrder) -> None:
        """Drop a reconciled order from tracking, returning close headroom.

        Called by the Reconciler when a tracked order is confirmed filled: the
        order stops being tracked, and a *closing* order's size is returned to
        the ticker's closeable headroom (its in-flight-closing tally is retired,
        issue #39/#40).

        Args:
            order: The tracked order to retire.
        """
        self._tracked.pop(order.order_id, None)
        if is_closing_action(order.action):
            self._retire_inflight(order.ticker, order.size_centis)

    def _retire_inflight(self, ticker: str, size_centis: int) -> None:
        """Return ``size_centis`` of closing headroom to ``ticker``'s tally.

        Args:
            ticker: The market ticker whose in-flight-closing tally shrinks.
            size_centis: The retired close's size, in contract-centis.
        """
        remaining = max(self._inflight_for(ticker) - size_centis, 0)
        if remaining > 0:
            self._inflight_closing[ticker] = ContractCentis(remaining)
        else:
            self._inflight_closing.pop(ticker, None)

    def _emit_heartbeat(self, beat: int) -> None:
        """Log one Gateway heartbeat.

        Args:
            beat: The 1-based heartbeat sequence number.
        """
        _LOGGER.info(
            "order gateway heartbeat beat=%d",
            beat,
            extra={"component": _COMPONENT},
        )

    def run(
        self,
        *,
        max_beats: int | None = None,
        heartbeat_interval: int = _DEFAULT_HEARTBEAT_INTERVAL,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Emit heartbeats until the beat budget or stop event ends the loop.

        The loop is always bounded by ``max_beats`` and/or ``stop_event`` -- there
        is never an unbounded sleep -- so it terminates deterministically under
        test and shuts down cleanly on a signal in production.

        Args:
            max_beats: Maximum number of heartbeats to emit before returning.
                ``None`` runs until ``stop_event`` is set.
            heartbeat_interval: Whole seconds to wait between beats. ``0`` waits
                not at all.
            stop_event: Optional event that, once set, ends the loop after the
                current beat. Defaults to a fresh, never-set event.
        """
        if stop_event is None:
            stop_event = threading.Event()
        beat = 0
        while (max_beats is None or beat < max_beats) and not stop_event.is_set():
            beat += 1
            self._emit_heartbeat(beat)
            stop_event.wait(heartbeat_interval)


def _non_negative_int(raw: str) -> int:
    """Parse a non-negative int for use as an argparse ``type``.

    Args:
        raw: The raw command-line token.

    Returns:
        The parsed integer value.

    Raises:
        argparse.ArgumentTypeError: If ``raw`` is negative.
    """
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the ``hedgekit.order_gateway`` bounded-heartbeat CLI parser.

    Both options parse as non-negative integers (a negative value is an argparse
    usage error, exit code 2), matching :mod:`hedgekit.riskkernel.process`'s
    convention. The interval is whole seconds (SPEC S6.1, float-free).

    Returns:
        A parser exposing ``--heartbeat-interval`` and ``--max-beats``.
    """
    parser = argparse.ArgumentParser(
        prog="hedgekit-order-gateway",
        description="Order Gateway (Process C) bounded heartbeat loop.",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=_non_negative_int,
        default=_DEFAULT_HEARTBEAT_INTERVAL,
        help="Whole seconds between heartbeats (default: %(default)s).",
    )
    parser.add_argument(
        "--max-beats",
        type=_non_negative_int,
        default=None,
        help="Stop after this many heartbeats (default: run until signalled).",
    )
    return parser


def _install_signal_handlers(stop_event: threading.Event) -> None:
    """Install SIGINT/SIGTERM handlers that request a graceful shutdown.

    Args:
        stop_event: The event set when a shutdown signal is delivered, so an
            in-flight :meth:`OrderGateway.run` unwinds after its current beat.
    """

    def _handle(_signum: int, _frame: FrameType | None) -> None:
        """Request shutdown by setting the stop event."""
        stop_event.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the bounded Order Gateway heartbeat loop.

    Sources the hex-encoded verification key from the :data:`_KEY_ENV_VAR`
    environment variable (the same symmetric key the Risk Kernel signs under),
    then runs the credential-free bounded heartbeat loop. Negative CLI arguments
    are rejected by argparse (exit code 2) before any key loading occurs.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code (always 0 on a clean run).
    """
    args = build_parser().parse_args(argv)
    configure_logging(level=logging.INFO)
    verification_key = bytes.fromhex(os.environ[_KEY_ENV_VAR])
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    gateway = OrderGateway(_UnwiredSubmitter(), verification_key=verification_key)
    gateway.run(
        max_beats=args.max_beats,
        heartbeat_interval=args.heartbeat_interval,
        stop_event=stop_event,
    )
    return 0
