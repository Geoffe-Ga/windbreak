"""Pending human-acknowledgement queue for the Risk Kernel (SPEC S10.3).

An over-threshold intent's worst-case cost must be explicitly acknowledged by a
human operator before it may proceed (the ``human_ack_satisfied`` pre-trade
check). This module holds the acknowledgement lifecycle:

    * :meth:`HumanAckQueue.request_ack` -- opens a pending acknowledgement with
      a single-use, unguessable approval id and a fixed ttl.
    * :meth:`HumanAckQueue.grant` -- an operator grants a still-live request,
      after which the intent id is acknowledged.
    * :meth:`HumanAckQueue.expire_due` -- any request nobody answers within its
      ttl lapses, releasing whatever capital reservation was held against it via
      the injected :class:`Releaser`.
    * :meth:`HumanAckQueue.acknowledged_intent_ids` -- the set the check reads.

The queue holds no clock: every method takes an explicit ``now`` epoch second,
keeping it a pure function of its arguments (SPEC S6.1 -- integer epoch seconds,
never a float). Approval ids that have lapsed or been granted are *remembered*,
not dropped, so :meth:`grant` can tell an id that never existed apart from one
that existed but has since lapsed. Every mutation records exactly one
string-discriminated :class:`~windbreak.ledger.events.Event`.
"""

from __future__ import annotations

import enum
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from windbreak.ledger.events import Event

if TYPE_CHECKING:
    from windbreak.numeric.types import MoneyMicros
    from windbreak.riskkernel.process import KernelLedgerWriter

#: The default operator-response window for a human acknowledgement: one hour.
DEFAULT_HUMAN_ACK_TTL_SECONDS = 3_600

#: Component label stamped on every event this module records.
_COMPONENT = "riskkernel"

#: Payload schema version stamped on every event this module records.
_PAYLOAD_SCHEMA_VERSION = 1

#: Bytes of entropy behind an approval id; ``secrets.token_hex`` renders it as
#: twice this many hex characters, so 16 bytes is a 32-character id.
_APPROVAL_ID_BYTES = 16

#: The reason a lapsed acknowledgement releases its held reservation with.
_LAPSED_RELEASE_REASON = "human-ack lapsed"

#: Event-type discriminators for the three events this module records.
_ACK_REQUESTED_EVENT = "HumanAckRequested"
_ACK_GRANTED_EVENT = "HumanAckGranted"
_ACK_LAPSED_EVENT = "HumanAckLapsed"


class AckLapsedError(Exception):
    """Raised when a lapsed (ttl-expired) acknowledgement is granted."""


class UnknownApprovalError(Exception):
    """Raised when an approval id that was never issued is granted."""


class DuplicateAckRequestError(Exception):
    """Raised when an intent already has a pending acknowledgement request."""


class Releaser(Protocol):
    """The seam a lapsed acknowledgement releases its reservation through.

    Structurally satisfied by
    :meth:`windbreak.riskkernel.reservations.ReservationLedger.release`.
    """

    def release(self, intent_id: str, *, reason: str) -> None:
        """Release the reservation held against ``intent_id``.

        Args:
            intent_id: The intent whose reservation is released.
            reason: A short human-readable reason for the release.
        """
        ...


class _AckStatus(enum.Enum):
    """The lifecycle state of a single acknowledgement request."""

    PENDING = enum.auto()
    GRANTED = enum.auto()
    LAPSED = enum.auto()


@dataclass(frozen=True, slots=True)
class PendingHumanAck:
    """A requested human acknowledgement awaiting a grant or its ttl.

    Attributes:
        approval_id: The single-use, unguessable id an operator grants against.
        intent_id: The intent whose worst-case cost needs acknowledgement.
        worst_case_cost: The intent's worst-case cost, in micros.
        requested_at: The epoch second the request was opened.
        expires_at: The epoch second the request lapses at (inclusive).
    """

    approval_id: str
    intent_id: str
    worst_case_cost: MoneyMicros
    requested_at: int
    expires_at: int


@dataclass(slots=True)
class _AckRecord:
    """A queue entry pairing a pending acknowledgement with its live status.

    Attributes:
        pending: The immutable acknowledgement request.
        status: The request's current lifecycle state.
    """

    pending: PendingHumanAck
    status: _AckStatus


class HumanAckQueue:
    """A pending human-acknowledgement queue over a single ledger writer.

    Every issued approval id is remembered for the life of the queue, so a
    grant can distinguish an id that never existed from one that has lapsed.
    """

    def __init__(
        self,
        *,
        writer: KernelLedgerWriter,
        releaser: Releaser,
        ttl_seconds: int = DEFAULT_HUMAN_ACK_TTL_SECONDS,
    ) -> None:
        """Initialize an empty queue.

        Args:
            writer: The seam every acknowledgement event is recorded through.
            releaser: The seam a lapsed acknowledgement releases its held
                reservation through.
            ttl_seconds: The operator-response window, in seconds. Defaults to
                :data:`DEFAULT_HUMAN_ACK_TTL_SECONDS` (one hour).
        """
        self._writer = writer
        self._releaser = releaser
        self._ttl_seconds = ttl_seconds
        self._records: dict[str, _AckRecord] = {}

    def request_ack(
        self, intent_id: str, worst_case_cost: MoneyMicros, now: int
    ) -> PendingHumanAck:
        """Open a pending acknowledgement for ``intent_id``.

        Args:
            intent_id: The intent whose worst-case cost needs acknowledgement.
            worst_case_cost: The intent's worst-case cost, in micros.
            now: The current epoch second.

        Returns:
            The created :class:`PendingHumanAck`, whose ``expires_at`` is
            ``now + ttl_seconds``.

        Raises:
            DuplicateAckRequestError: If ``intent_id`` already has a pending
                (ungranted, unlapsed) acknowledgement.
        """
        if self._has_pending_intent(intent_id):
            raise DuplicateAckRequestError(
                f"intent already has a pending acknowledgement: {intent_id}"
            )
        approval_id = secrets.token_hex(_APPROVAL_ID_BYTES)
        expires_at = now + self._ttl_seconds
        pending = PendingHumanAck(
            approval_id=approval_id,
            intent_id=intent_id,
            worst_case_cost=worst_case_cost,
            requested_at=now,
            expires_at=expires_at,
        )
        self._records[approval_id] = _AckRecord(
            pending=pending, status=_AckStatus.PENDING
        )
        self._record(
            _ACK_REQUESTED_EVENT,
            {
                "approval_id": approval_id,
                "intent_id": intent_id,
                "worst_case_cost_micros": worst_case_cost.value,
                "requested_at": now,
                "expires_at": expires_at,
            },
        )
        return pending

    def grant(self, *, approval_id: str, now: int) -> None:
        """Grant a still-live acknowledgement, acknowledging its intent.

        Args:
            approval_id: The id issued by :meth:`request_ack`.
            now: The current epoch second.

        Raises:
            UnknownApprovalError: If ``approval_id`` was never issued.
            AckLapsedError: If the acknowledgement is no longer pending, or is
                at/past its inclusive ``expires_at`` boundary.
        """
        record = self._records.get(approval_id)
        if record is None:
            raise UnknownApprovalError(f"unknown approval id: {approval_id}")
        if record.status is not _AckStatus.PENDING or now >= record.pending.expires_at:
            raise AckLapsedError(f"acknowledgement has lapsed: {approval_id}")
        record.status = _AckStatus.GRANTED
        self._record(
            _ACK_GRANTED_EVENT,
            {
                "approval_id": approval_id,
                "intent_id": record.pending.intent_id,
                "granted_at": now,
            },
        )

    def expire_due(self, now: int) -> None:
        """Lapse and release every due, still-pending acknowledgement.

        The boundary is inclusive (``now >= expires_at`` is due), matching
        :meth:`windbreak.riskkernel.reservations.ReservationLedger.expire_due`.
        A granted acknowledgement never lapses.

        Args:
            now: The current epoch second.
        """
        for approval_id, record in self._records.items():
            if record.status is _AckStatus.PENDING and now >= record.pending.expires_at:
                record.status = _AckStatus.LAPSED
                self._record(
                    _ACK_LAPSED_EVENT,
                    {
                        "approval_id": approval_id,
                        "intent_id": record.pending.intent_id,
                        "expired_at": now,
                    },
                )
                self._releaser.release(
                    record.pending.intent_id, reason=_LAPSED_RELEASE_REASON
                )

    def acknowledged_intent_ids(self, now: int) -> frozenset[str]:
        """Return the intent ids with a granted acknowledgement.

        Args:
            now: The current epoch second (unused: a granted acknowledgement
                never lapses, so it stays acknowledged regardless of ``now``).

        Returns:
            A frozenset of every granted intent id.
        """
        del now  # A granted acknowledgement never lapses; `now` cannot exclude it.
        return frozenset(
            record.pending.intent_id
            for record in self._records.values()
            if record.status is _AckStatus.GRANTED
        )

    def _has_pending_intent(self, intent_id: str) -> bool:
        """Return whether ``intent_id`` already has a pending acknowledgement.

        Args:
            intent_id: The intent id to look for.

        Returns:
            ``True`` if a still-pending record names ``intent_id``.
        """
        return any(
            record.status is _AckStatus.PENDING
            and record.pending.intent_id == intent_id
            for record in self._records.values()
        )

    def _record(self, event_type: str, payload: dict[str, object]) -> None:
        """Record one acknowledgement event through the writer.

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
