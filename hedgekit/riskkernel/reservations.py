"""Capital reservations and the approval pipeline (SPEC S5.3/S10.6).

This module ships the Risk Kernel's single-writer capital ledger and the
pipeline that turns an approved intent into reserved-and-signed approval token:

    * :class:`ReservationLedger` -- a single-lock ledger assigning monotonic
      per-approval sequence numbers, remembering every intent id and
      idempotency key it has *ever* seen (even past release), supporting
      decrease-only adjustment and time-bounded expiry, and recording exactly
      one :class:`~hedgekit.ledger.events.Event` per mutation.
    * :class:`ApprovalPipeline` -- stamps ledger-sourced state onto a copy of
      the caller's context, evaluates the check pipeline, and -- only when no
      check vetoes -- reserves the worst-case cost and issues a single-use
      approval token, all atomically under the ledger's one lock so concurrent
      approvals can never jointly over-reserve past a headroom limit.

Every monetary quantity is a :mod:`hedgekit.numeric` scaled integer, never a
float (SPEC S6.1). The clock is always injected via ``context.now_epoch_s`` --
never read from :func:`time.time` -- so behavior is fully deterministic.
"""

from __future__ import annotations

import dataclasses
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hedgekit.ledger.events import Event
from hedgekit.numeric import RoundingDirection, money_from_price_and_count
from hedgekit.numeric.types import MoneyMicros
from hedgekit.riskkernel import checks
from hedgekit.riskkernel.checks import _order_cost
from hedgekit.riskkernel.tokens import DEFAULT_TOKEN_TTL_SECONDS
from hedgekit.tokens.verify import ApprovalTokenClaims

if TYPE_CHECKING:
    from collections.abc import Iterator

    from hedgekit.riskkernel.checks import Decision, OrderIntent
    from hedgekit.riskkernel.context import EvaluationContext
    from hedgekit.riskkernel.process import KernelLedgerWriter
    from hedgekit.riskkernel.tokens import TokenIssuer
    from hedgekit.tokens.verify import SignedApprovalToken

#: Component label stamped on every event this module records.
_COMPONENT = "riskkernel"

#: Payload schema version stamped on every event this module records.
_PAYLOAD_SCHEMA_VERSION = 1

#: Event-type discriminators for the four events this module records.
_RESERVATION_CREATED_EVENT = "ReservationCreated"
_RESERVATION_RELEASED_EVENT = "ReservationReleased"
_RESERVATION_ADJUSTED_EVENT = "ReservationAdjusted"
_APPROVAL_ISSUED_EVENT = "ApprovalTokenIssued"


@dataclass(frozen=True, slots=True)
class Reservation:
    """A single capital reservation (active until released or expired).

    Attributes:
        intent_id: The reserving intent's unique identifier.
        amount: The reserved capital, in micros.
        idempotency_key: The caller-supplied idempotency key.
        expires_at: The reservation's expiry, in epoch seconds.
        sequence_number: The ledger's monotonic sequence number for this
            reservation (the source of a token's ``kernel_sequence_number``).
    """

    intent_id: str
    amount: MoneyMicros
    idempotency_key: str
    expires_at: int
    sequence_number: int


class DuplicateReservationError(Exception):
    """Raised when an intent id or idempotency key was ever reserved before."""


class ReservationLedger:
    """A single-writer capital-reservation ledger (SPEC S5.3).

    One re-entrant :class:`threading.RLock` guards every mutation, so concurrent
    callers serialize and no two reservations can jointly over-reserve. Intent
    ids and idempotency keys, once used, are remembered forever -- even after
    the reservation is released -- so an approval token is never issued twice
    for the same intent or key.
    """

    def __init__(self, writer: KernelLedgerWriter) -> None:
        """Initialize an empty ledger writing to ``writer``.

        Args:
            writer: The seam every reservation event is recorded through.
        """
        self._writer = writer
        self._lock = threading.RLock()
        self._active: dict[str, Reservation] = {}
        self._seen_intent_ids: set[str] = set()
        self._seen_idempotency_keys: set[str] = set()
        self._sequence = 0

    @property
    def writer(self) -> KernelLedgerWriter:
        """Return the ledger's event writer (read-only)."""
        return self._writer

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the ledger's single mutation lock across a compound operation.

        The lock is re-entrant, so a caller holding it may still invoke any
        ledger method (e.g. :meth:`reserve`, :meth:`total_reserved`) within the
        ``with`` block without deadlocking. This is the seam the
        :class:`ApprovalPipeline` uses to make evaluate-then-reserve atomic.

        Yields:
            ``None``; the lock is held for the duration of the ``with`` block.
        """
        with self._lock:
            yield

    def reserve(
        self,
        intent_id: str,
        amount: MoneyMicros,
        idempotency_key: str,
        *,
        expires_at: int,
    ) -> Reservation:
        """Reserve ``amount`` against ``intent_id``, assigning a sequence number.

        Args:
            intent_id: The reserving intent's unique identifier.
            amount: The capital to reserve, in micros.
            idempotency_key: The caller-supplied idempotency key.
            expires_at: The reservation's expiry, in epoch seconds.

        Returns:
            The created :class:`Reservation`.

        Raises:
            DuplicateReservationError: If ``intent_id`` or ``idempotency_key``
                was ever reserved before, even by a since-released reservation.
        """
        with self._lock:
            if intent_id in self._seen_intent_ids:
                raise DuplicateReservationError(
                    f"intent id already reserved: {intent_id}"
                )
            if idempotency_key in self._seen_idempotency_keys:
                raise DuplicateReservationError(
                    f"idempotency key already reserved: {idempotency_key}"
                )
            self._sequence += 1
            reservation = Reservation(
                intent_id=intent_id,
                amount=amount,
                idempotency_key=idempotency_key,
                expires_at=expires_at,
                sequence_number=self._sequence,
            )
            self._active[intent_id] = reservation
            self._seen_intent_ids.add(intent_id)
            self._seen_idempotency_keys.add(idempotency_key)
            self._record(
                _RESERVATION_CREATED_EVENT,
                {
                    "intent_id": intent_id,
                    "amount": amount.value,
                    "idempotency_key": idempotency_key,
                    "expires_at": expires_at,
                    "sequence_number": reservation.sequence_number,
                },
            )
            return reservation

    def release(self, intent_id: str, *, reason: str) -> None:
        """Release ``intent_id``'s active reservation, keeping its id remembered.

        Args:
            intent_id: The reservation to release.
            reason: A short human-readable reason for the release.
        """
        with self._lock:
            self._release_locked(intent_id, reason)

    def release_all_active(self, *, reason: str) -> None:
        """Release every currently-active reservation under one lock (issue #35).

        The kill switch's capital-release primitive: it frees all reserved
        capital in a single locked pass so a killed kernel holds no live
        reservation, recording one
        :class:`~hedgekit.ledger.events.Event` (``ReservationReleased``) per
        released intent via :meth:`_release_locked`. It deliberately never
        touches ``_seen_intent_ids`` / ``_seen_idempotency_keys``: every id and
        key stays permanently remembered, so no stale pre-kill intent can be
        replayed after a re-arm.

        Args:
            reason: The reason recorded on each release event. The kill path
                passes a hold-only reason (never a sell/close/submit/dump
                action), preserving the position-hold invariant.
        """
        with self._lock:
            for intent_id in list(self._active):
                self._release_locked(intent_id, reason)

    def adjust(self, intent_id: str, remaining_amount: MoneyMicros) -> None:
        """Decrease an active reservation to ``remaining_amount`` (decrease-only).

        Args:
            intent_id: The active reservation to adjust.
            remaining_amount: The new, strictly-smaller reserved amount.

        Raises:
            ValueError: If ``intent_id`` has no active reservation, or
                ``remaining_amount`` is not a strict decrease
                (``0 < new < current``).
        """
        with self._lock:
            reservation = self._active.get(intent_id)
            if reservation is None:
                raise ValueError(f"no active reservation for {intent_id}")
            if not 0 < remaining_amount.value < reservation.amount.value:
                raise ValueError(
                    f"adjust for {intent_id} must strictly decrease "
                    f"{reservation.amount.value} to a positive amount, "
                    f"got {remaining_amount.value}"
                )
            self._active[intent_id] = dataclasses.replace(
                reservation, amount=remaining_amount
            )
            self._record(
                _RESERVATION_ADJUSTED_EVENT,
                {"intent_id": intent_id, "remaining_amount": remaining_amount.value},
            )

    def expire_due(self, now_epoch_s: int) -> None:
        """Release every reservation whose expiry is at or before ``now_epoch_s``.

        Args:
            now_epoch_s: The current time, in epoch seconds; a reservation with
                ``expires_at <= now_epoch_s`` is due (inclusive boundary).
        """
        with self._lock:
            due = [
                reservation.intent_id
                for reservation in self._active.values()
                if reservation.expires_at <= now_epoch_s
            ]
            for intent_id in due:
                self._release_locked(intent_id, "expired")

    def total_reserved(self) -> MoneyMicros:
        """Return the summed amount of all active reservations, in micros."""
        with self._lock:
            return MoneyMicros(
                sum(reservation.amount.value for reservation in self._active.values())
            )

    def used_intent_ids(self) -> frozenset[str]:
        """Return every intent id ever reserved, even if since released."""
        with self._lock:
            return frozenset(self._seen_intent_ids)

    def used_idempotency_keys(self) -> frozenset[str]:
        """Return every idempotency key ever reserved, even if since released."""
        with self._lock:
            return frozenset(self._seen_idempotency_keys)

    def _release_locked(self, intent_id: str, reason: str) -> None:
        """Drop a reservation and record its release, assuming the lock is held.

        Args:
            intent_id: The reservation to release.
            reason: The reason recorded on the release event.
        """
        self._active.pop(intent_id, None)
        self._record(
            _RESERVATION_RELEASED_EVENT,
            {"intent_id": intent_id, "reason": reason},
        )

    def _record(self, event_type: str, payload: dict[str, object]) -> None:
        """Record one event through the writer, assuming the lock is held.

        Args:
            event_type: The event discriminator.
            payload: The event's integer/string payload.
        """
        self._writer.record(
            Event(
                event_type=event_type,
                component=_COMPONENT,
                payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
                payload=payload,
            )
        )


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """The result of running an intent through the approval pipeline.

    Attributes:
        decision: The check pipeline's verdict.
        token: The signed approval token when the intent was approved, or
            ``None`` when it was vetoed.
    """

    decision: Decision
    token: SignedApprovalToken | None


class ApprovalPipeline:
    """Evaluates, reserves, and issues an approval token atomically (SPEC S10.6)."""

    def __init__(
        self,
        ledger: ReservationLedger,
        issuer: TokenIssuer,
        *,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        config_hash: str,
    ) -> None:
        """Initialize the pipeline.

        Args:
            ledger: The single-writer reservation ledger.
            issuer: The approval-token issuer.
            ttl_seconds: The issued token's lifetime, in seconds. Defaults to
                :data:`~hedgekit.riskkernel.tokens.DEFAULT_TOKEN_TTL_SECONDS`.
            config_hash: The active configuration revision hash, stamped into
                every issued token's claims.
        """
        self._ledger = ledger
        self._issuer = issuer
        self._ttl_seconds = ttl_seconds
        self._config_hash = config_hash

    def approve(
        self, intent: OrderIntent, context: EvaluationContext
    ) -> ApprovalOutcome:
        """Evaluate ``intent`` and, if approved, reserve capital and issue a token.

        The whole evaluate-then-reserve sequence runs under the ledger's single
        lock, so concurrent approvals can never jointly over-reserve past a
        headroom limit: each sees the reservations every earlier-committed
        approval already made. A veto reserves nothing, issues no token, and
        consumes no sequence number.

        Args:
            intent: The order intent to approve.
            context: The caller-supplied evaluation context.

        Returns:
            An :class:`ApprovalOutcome`; ``token`` is ``None`` when any check
            vetoed, else a freshly signed single-use token.
        """
        with self._ledger.transaction():
            effective = self._effective_context(context)
            decision = checks.evaluate_intent(intent, effective)
            if decision.vetoed:
                return ApprovalOutcome(decision=decision, token=None)
            return self._reserve_and_issue(intent, effective, decision)

    def _effective_context(self, context: EvaluationContext) -> EvaluationContext:
        """Stamp current ledger state onto a copy of ``context``.

        Replaces the account's ``pending_kernel_reservations`` with the ledger's
        live total and the context's uniqueness sets with the ledger's, so every
        check sees ledger-truth rather than caller-supplied values. The caller's
        original context object is never mutated.

        Args:
            context: The caller-supplied context.

        Returns:
            A new context carrying ledger-sourced reservation and uniqueness
            state.
        """
        account = dataclasses.replace(
            context.account,
            pending_kernel_reservations=self._ledger.total_reserved(),
        )
        return dataclasses.replace(
            context,
            account=account,
            used_intent_ids=self._ledger.used_intent_ids(),
            used_idempotency_keys=self._ledger.used_idempotency_keys(),
        )

    def _reserve_and_issue(
        self,
        intent: OrderIntent,
        context: EvaluationContext,
        decision: Decision,
    ) -> ApprovalOutcome:
        """Reserve the worst-case cost and issue a signed token for ``intent``.

        The reserved amount is the full worst-case cost the checks proved (via
        the shared :func:`~hedgekit.riskkernel.checks._order_cost`, whose
        fail-closed handling of an unprovable fee bound is already covered by
        the check suite). The combined fee cap the claims carry is recovered as
        ``cost - notional - rounding_buffer`` -- exactly the trading-plus-
        settlement fee bound -- so no fee bound is read (and narrowed) twice.

        Args:
            intent: The approved order intent.
            context: The effective (ledger-stamped) evaluation context.
            decision: The non-vetoing check-pipeline decision.

        Returns:
            An :class:`ApprovalOutcome` carrying the decision and the issued
            token.
        """
        amount = _order_cost(intent, context)
        notional = money_from_price_and_count(
            intent.price, intent.size, rounding=RoundingDirection.OVERSTATE_COST
        )
        max_fee_micros = amount - notional - context.limits.rounding_buffer
        expires_at = context.now_epoch_s + self._ttl_seconds
        reservation = self._ledger.reserve(
            intent.intent_id, amount, intent.idempotency_key, expires_at=expires_at
        )
        claims = ApprovalTokenClaims(
            intent_id=intent.intent_id,
            market_ticker=intent.market_ticker,
            outcome=intent.outcome,
            action=intent.action,
            limit_price_pips=intent.price,
            count_centis=intent.size,
            max_fee_micros=max_fee_micros,
            expires_at=expires_at,
            idempotency_key=intent.idempotency_key,
            config_hash=self._config_hash,
            kernel_sequence_number=reservation.sequence_number,
        )
        token = self._issuer.issue(claims)
        self._ledger.writer.record(
            Event(
                event_type=_APPROVAL_ISSUED_EVENT,
                component=_COMPONENT,
                payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
                payload={
                    "intent_id": intent.intent_id,
                    "sequence_number": reservation.sequence_number,
                    "expires_at": expires_at,
                },
            )
        )
        return ApprovalOutcome(decision=decision, token=token)
