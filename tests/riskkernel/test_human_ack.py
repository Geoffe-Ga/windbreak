"""Failing-first tests for hedgekit.riskkernel.human_ack (issue #34, RED).

Issue #34 gives the Risk Kernel a pending human-acknowledgement queue: an
over-threshold intent's worst-case cost must be explicitly acknowledged by a
human operator before it may proceed (SPEC's `human_ack_satisfied` check,
promoted from stub to real logic in `tests/riskkernel/test_checks.py`), and an
acknowledgement request that nobody answers lapses on a fixed ttl, releasing
whatever capital reservation was held against it.

`hedgekit/riskkernel/human_ack.py` does not exist yet, so every import below
fails collection with `ModuleNotFoundError` -- the expected Gate 1 RED state
for issue #34.

API-shape decisions pinned by this file (the implementation specialist must
build to these exactly):

* `HumanAckQueue.__init__` takes `writer: KernelLedgerWriter`,
  `releaser: Releaser` (any object exposing `.release(intent_id, *,
  reason)` -- `hedgekit.riskkernel.reservations.ReservationLedger` already
  satisfies this duck type, exercised directly below), and
  `ttl_seconds: int = DEFAULT_HUMAN_ACK_TTL_SECONDS`. There is no injected
  clock callable on the queue itself: every method takes an explicit `now`
  epoch-second argument instead (`request_ack`, `grant`, `expire_due`,
  `acknowledged_intent_ids`), keeping it a pure function of its arguments.
* `DEFAULT_HUMAN_ACK_TTL_SECONDS == 3_600` (one hour) -- a plausible
  operator-response window pinned here since the architect's plan did not
  fix an exact value.
* Events are plain, string-discriminated `hedgekit.ledger.events.Event`s:
  `"HumanAckRequested"` (`approval_id`, `intent_id`,
  `worst_case_cost_micros`, `requested_at`, `expires_at`),
  `"HumanAckGranted"` (`approval_id`, `intent_id`, `granted_at`), and
  `"HumanAckLapsed"` (`approval_id`, `intent_id`, `expired_at`).
"""

from __future__ import annotations

import dataclasses

import pytest

from hedgekit.numeric.types import MoneyMicros
from hedgekit.riskkernel.human_ack import (
    DEFAULT_HUMAN_ACK_TTL_SECONDS,
    AckLapsedError,
    DuplicateAckRequestError,
    HumanAckQueue,
    UnknownApprovalError,
)
from hedgekit.riskkernel.process import InMemoryKernelLedgerWriter
from hedgekit.riskkernel.reservations import ReservationLedger

#: A fixed, generous ttl every test not itself pinning `expires_at` arithmetic
#: uses, so its exact value never matters to those tests.
_TTL_SECONDS = 3_600


@dataclasses.dataclass
class _SpyReleaser:
    """A fake `Releaser` (duck-typed against `ReservationLedger.release`)
    that records every call instead of touching a real ledger.
    """

    released: list[tuple[str, str]] = dataclasses.field(default_factory=list)

    def release(self, intent_id: str, *, reason: str) -> None:
        """Record the release call.

        Args:
            intent_id: The intent whose reservation would be released.
            reason: The human-readable release reason.
        """
        self.released.append((intent_id, reason))


def _events_of_type(
    writer: InMemoryKernelLedgerWriter, event_type: str
) -> list[object]:
    """Return every recorded event of `event_type`, in recorded order.

    Args:
        writer: The in-memory writer to filter.
        event_type: The exact `Event.event_type` string to match.

    Returns:
        The matching events, in the order they were recorded.
    """
    return [event for event in writer.events if event.event_type == event_type]


# --- Sanity on the fixture constant -----------------------------------------------


def test_default_human_ack_ttl_seconds_constant_is_one_hour() -> None:
    """`DEFAULT_HUMAN_ACK_TTL_SECONDS` is exactly one hour."""
    assert DEFAULT_HUMAN_ACK_TTL_SECONDS == 3_600


# --- request_ack ---------------------------------------------------------------------


def test_request_ack_sets_expires_at_to_now_plus_ttl_and_records_the_event() -> None:
    """`request_ack` computes `expires_at == now + ttl_seconds` and records
    exactly one `HumanAckRequested` event carrying the same fields."""
    writer = InMemoryKernelLedgerWriter()
    queue = HumanAckQueue(
        writer=writer, releaser=_SpyReleaser(), ttl_seconds=_TTL_SECONDS
    )

    pending = queue.request_ack(
        intent_id="intent-1", worst_case_cost=MoneyMicros(5_000_000), now=1_000
    )

    assert pending.intent_id == "intent-1"
    assert pending.expires_at == 1_000 + _TTL_SECONDS
    requested = _events_of_type(writer, "HumanAckRequested")
    assert len(requested) == 1
    assert requested[0].payload["intent_id"] == "intent-1"
    assert requested[0].payload["approval_id"] == pending.approval_id
    assert requested[0].payload["expires_at"] == pending.expires_at


def test_request_ack_approval_ids_are_distinct_and_unguessably_long() -> None:
    """Two requests never share an approval id, and each id is long enough
    to resist guessing (not e.g. a small sequential counter)."""
    queue = HumanAckQueue(
        writer=InMemoryKernelLedgerWriter(),
        releaser=_SpyReleaser(),
        ttl_seconds=_TTL_SECONDS,
    )

    first = queue.request_ack(
        intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0
    )
    second = queue.request_ack(
        intent_id="intent-2", worst_case_cost=MoneyMicros(1), now=0
    )

    assert first.approval_id != second.approval_id
    assert len(first.approval_id) >= 16
    assert len(second.approval_id) >= 16


def test_request_ack_rejects_a_duplicate_pending_intent_id() -> None:
    """A second `request_ack` for an intent id with an already-pending
    (ungranted, unexpired) request raises `DuplicateAckRequestError`."""
    queue = HumanAckQueue(
        writer=InMemoryKernelLedgerWriter(),
        releaser=_SpyReleaser(),
        ttl_seconds=_TTL_SECONDS,
    )
    queue.request_ack(intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0)

    with pytest.raises(DuplicateAckRequestError):
        queue.request_ack(intent_id="intent-1", worst_case_cost=MoneyMicros(2), now=1)


# --- grant ---------------------------------------------------------------------------


def test_grant_before_expiry_records_event_and_acknowledges_the_intent() -> None:
    """Granting strictly before `expires_at` records `HumanAckGranted` and
    makes the intent id appear in `acknowledged_intent_ids`."""
    writer = InMemoryKernelLedgerWriter()
    queue = HumanAckQueue(
        writer=writer, releaser=_SpyReleaser(), ttl_seconds=_TTL_SECONDS
    )
    pending = queue.request_ack(
        intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0
    )

    queue.grant(approval_id=pending.approval_id, now=_TTL_SECONDS - 1)

    granted = _events_of_type(writer, "HumanAckGranted")
    assert len(granted) == 1
    assert granted[0].payload["intent_id"] == "intent-1"
    assert "intent-1" in queue.acknowledged_intent_ids(now=_TTL_SECONDS - 1)


def test_grant_at_exact_expiry_raises_ack_lapsed_error() -> None:
    """`now == expires_at` is the inclusive lapse boundary: granting there
    raises `AckLapsedError`, not a success."""
    queue = HumanAckQueue(
        writer=InMemoryKernelLedgerWriter(),
        releaser=_SpyReleaser(),
        ttl_seconds=_TTL_SECONDS,
    )
    pending = queue.request_ack(
        intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0
    )

    with pytest.raises(AckLapsedError):
        queue.grant(approval_id=pending.approval_id, now=_TTL_SECONDS)


def test_grant_after_expiry_raises_ack_lapsed_error() -> None:
    """Granting after `expires_at` also raises `AckLapsedError`."""
    queue = HumanAckQueue(
        writer=InMemoryKernelLedgerWriter(),
        releaser=_SpyReleaser(),
        ttl_seconds=_TTL_SECONDS,
    )
    pending = queue.request_ack(
        intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0
    )

    with pytest.raises(AckLapsedError):
        queue.grant(approval_id=pending.approval_id, now=_TTL_SECONDS + 1)


def test_grant_with_an_unknown_approval_id_raises_unknown_approval_error() -> None:
    """An approval id that was never issued raises `UnknownApprovalError` --
    distinct from the lapsed case, since the queue must tell "never existed"
    apart from "existed but expired"."""
    queue = HumanAckQueue(
        writer=InMemoryKernelLedgerWriter(),
        releaser=_SpyReleaser(),
        ttl_seconds=_TTL_SECONDS,
    )

    with pytest.raises(UnknownApprovalError):
        queue.grant(approval_id="never-issued", now=0)


# --- expire_due --------------------------------------------------------------------


def test_expire_due_lapses_every_due_ungranted_pending_request() -> None:
    """`expire_due` lapses every due, still-pending request: it records one
    `HumanAckLapsed` per lapsed request and releases each one's reservation
    with the exact reason `"human-ack lapsed"`."""
    writer = InMemoryKernelLedgerWriter()
    releaser = _SpyReleaser()
    queue = HumanAckQueue(writer=writer, releaser=releaser, ttl_seconds=_TTL_SECONDS)
    queue.request_ack(intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0)
    queue.request_ack(intent_id="intent-2", worst_case_cost=MoneyMicros(1), now=0)

    queue.expire_due(now=_TTL_SECONDS)

    lapsed = _events_of_type(writer, "HumanAckLapsed")
    assert {event.payload["intent_id"] for event in lapsed} == {"intent-1", "intent-2"}
    assert set(releaser.released) == {
        ("intent-1", "human-ack lapsed"),
        ("intent-2", "human-ack lapsed"),
    }


def test_expire_due_boundary_is_inclusive_of_exact_expiry() -> None:
    """`now == expires_at` is due (inclusive boundary), matching
    `ReservationLedger.expire_due`'s own inclusive convention."""
    writer = InMemoryKernelLedgerWriter()
    releaser = _SpyReleaser()
    queue = HumanAckQueue(writer=writer, releaser=releaser, ttl_seconds=_TTL_SECONDS)
    queue.request_ack(intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0)

    queue.expire_due(now=_TTL_SECONDS)

    assert releaser.released == [("intent-1", "human-ack lapsed")]


def test_expire_due_keeps_a_pending_request_one_second_before_expiry() -> None:
    """A request one second shy of `expires_at` is not yet due."""
    writer = InMemoryKernelLedgerWriter()
    releaser = _SpyReleaser()
    queue = HumanAckQueue(writer=writer, releaser=releaser, ttl_seconds=_TTL_SECONDS)
    queue.request_ack(intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0)

    queue.expire_due(now=_TTL_SECONDS - 1)

    assert releaser.released == []
    assert _events_of_type(writer, "HumanAckLapsed") == []


def test_expire_due_never_lapses_an_already_granted_ack() -> None:
    """A granted acknowledgement is immune to `expire_due`: no release, and
    the intent id stays acknowledged."""
    writer = InMemoryKernelLedgerWriter()
    releaser = _SpyReleaser()
    queue = HumanAckQueue(writer=writer, releaser=releaser, ttl_seconds=_TTL_SECONDS)
    pending = queue.request_ack(
        intent_id="intent-1", worst_case_cost=MoneyMicros(1), now=0
    )
    queue.grant(approval_id=pending.approval_id, now=100)

    queue.expire_due(now=_TTL_SECONDS)

    assert releaser.released == []
    assert "intent-1" in queue.acknowledged_intent_ids(now=_TTL_SECONDS)


# --- acknowledged_intent_ids ---------------------------------------------------------


def test_acknowledged_intent_ids_returns_a_frozenset_of_granted_intents() -> None:
    """`acknowledged_intent_ids` returns a `frozenset[str]` containing
    exactly the granted (never the merely-requested) intent ids."""
    queue = HumanAckQueue(
        writer=InMemoryKernelLedgerWriter(),
        releaser=_SpyReleaser(),
        ttl_seconds=_TTL_SECONDS,
    )
    queue.request_ack(
        intent_id="intent-unresolved", worst_case_cost=MoneyMicros(1), now=0
    )
    pending = queue.request_ack(
        intent_id="intent-granted", worst_case_cost=MoneyMicros(1), now=0
    )
    queue.grant(approval_id=pending.approval_id, now=1)

    result = queue.acknowledged_intent_ids(now=1)

    assert isinstance(result, frozenset)
    assert result == frozenset({"intent-granted"})


# --- Real ReservationLedger integration -----------------------------------------------


def test_expire_due_releases_a_real_active_reservation_with_the_lapsed_reason() -> None:
    """Against a real `ReservationLedger` holding an active reservation for
    the lapsing intent, `expire_due` causes a genuine `ReservationReleased`
    event carrying the exact `"human-ack lapsed"` reason -- not just a spy
    call."""
    reservation_writer = InMemoryKernelLedgerWriter()
    reservation_ledger = ReservationLedger(reservation_writer)
    reservation_ledger.reserve(
        "intent-1", MoneyMicros(500_000), "idem-1", expires_at=10_000
    )
    queue_writer = InMemoryKernelLedgerWriter()
    queue = HumanAckQueue(
        writer=queue_writer, releaser=reservation_ledger, ttl_seconds=_TTL_SECONDS
    )
    queue.request_ack(intent_id="intent-1", worst_case_cost=MoneyMicros(500_000), now=0)

    queue.expire_due(now=_TTL_SECONDS)

    released = [
        event
        for event in reservation_writer.events
        if event.event_type == "ReservationReleased"
    ]
    assert len(released) == 1
    assert released[0].payload["intent_id"] == "intent-1"
    assert released[0].payload["reason"] == "human-ack lapsed"
    assert reservation_ledger.total_reserved() == MoneyMicros(0)
