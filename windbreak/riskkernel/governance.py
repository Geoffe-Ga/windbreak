"""Operator-facing equity-floor governance for the Risk Kernel (SPEC S5.1).

The floor is the equity level the Risk Kernel's :class:`_FloorInvariant` check
defends. This module governs how that floor moves:

    * :meth:`FloorGovernance.raise_floor` -- an operator may *raise* the floor
      immediately (never a risk increase), from the CLI or the dashboard.
    * :meth:`FloorGovernance.request_lower` / :meth:`FloorGovernance.confirm_lower`
      -- *lowering* the floor is a two-step, un-shortenable 48-hour cool-off
      plus a single-use nonce, and may be requested only from the CLI. A
      completed lowering demotes any live mode down to ``PAPER``.
    * :meth:`FloorGovernance.observe_equity` -- independent of operator action,
      the floor *ratchets* upward automatically as equity makes fresh highs (a
      fixed ppm share of each new gain, applied with no delay), and fires an
      advisory profit-sweep alert once the gain since the last high-water mark
      crosses a configured threshold. Every fresh high-water crossing also
      ledgers an ``EquityHighWaterMarkAdvanced`` fact carrying the new absolute
      mark, so :meth:`from_events` can reconstruct the mark *exactly* rather
      than inferring it from the ratchet.

Every event is a plain, string-discriminated
:class:`~windbreak.ledger.events.Event` (mirroring
:mod:`~windbreak.riskkernel.reservations`), never a new dataclass subclass. Every
monetary quantity is a :class:`~windbreak.numeric.types.MoneyMicros`, never a
float (SPEC S6.1), and the clock is always the injected ``clock`` callable,
never :func:`time.time`, so behavior is fully deterministic and replayable.
"""

from __future__ import annotations

import enum
import hmac
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from windbreak.alerts import AlertType
from windbreak.ledger.events import Event
from windbreak.numeric.types import MoneyMicros
from windbreak.riskkernel.checks import _ppm_of
from windbreak.riskkernel.modes import Mode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from windbreak.alerts import AlertDispatcher
    from windbreak.riskkernel.modes import ModeStateMachine
    from windbreak.riskkernel.process import KernelLedgerWriter

#: The floor-lower cool-off, in seconds: exactly 48 hours (SPEC S5.1). This is a
#: floor, not a default an operator may shorten -- a shorter value is only ever
#: used to make cool-off arithmetic tractable under test.
DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS = 172_800

#: Component label stamped on every event this module records.
_COMPONENT = "riskkernel"

#: Payload schema version stamped on every event this module records.
_PAYLOAD_SCHEMA_VERSION = 1

#: Bytes of entropy behind a floor-lower nonce; ``secrets.token_hex`` renders it
#: as twice this many hex characters, so 16 bytes is a 32-character nonce.
_NONCE_BYTES = 16

#: Event-type discriminators for the six events this module records.
_FLOOR_RAISED_EVENT = "FloorRaised"
_FLOOR_LOWER_REQUESTED_EVENT = "FloorLowerRequested"
_FLOOR_LOWER_REFUSED_EVENT = "FloorLowerRefused"
_FLOOR_LOWER_CONFIRMED_EVENT = "FloorLowerConfirmed"
_FLOOR_RATCHET_APPLIED_EVENT = "FloorRatchetApplied"
_EQUITY_HIGH_WATER_MARK_ADVANCED_EVENT = "EquityHighWaterMarkAdvanced"

#: Refusal reasons recorded on a ``FloorLowerRefused`` event.
_REFUSED_FORBIDDEN_ORIGIN = "forbidden_origin"
_REFUSED_COOL_OFF_ACTIVE = "cool_off_active"
_REFUSED_NONCE_MISMATCH = "nonce_mismatch"

#: The runtime modes a completed lowering demotes out of, one rung at a time.
_LIVE_MODES: tuple[Mode, ...] = (Mode.LIVE, Mode.LIVE_MICRO)


class ChangeOrigin(enum.Enum):
    """Who requested a floor change: the operator CLI or the dashboard.

    Only :attr:`CLI` may *lower* the floor; both origins may *raise* it.
    """

    CLI = enum.auto()
    DASHBOARD = enum.auto()


class ForbiddenOriginError(Exception):
    """Raised when a floor lowering is requested from a forbidden origin."""


class CoolOffActiveError(Exception):
    """Raised when a lowering is confirmed before its cool-off has elapsed."""


class NonceMismatchError(Exception):
    """Raised when a lowering is confirmed with the wrong nonce."""


class LoweringAlreadyPendingError(Exception):
    """Raised when a second lowering is requested while one is pending."""


class NoPendingLowerError(Exception):
    """Raised when a lowering is confirmed with none pending."""


@dataclass(frozen=True, slots=True)
class PendingFloorLower:
    """A requested-but-unconfirmed floor lowering awaiting its cool-off.

    Attributes:
        nonce: The single-use confirmation nonce issued at request time.
        ready_at: The earliest epoch second the lowering may be confirmed
            (``requested_at + cool_off_seconds``), fixed once at request time.
        target_floor_micros: The floor the lowering will apply on confirmation.
    """

    nonce: str
    ready_at: int
    target_floor_micros: MoneyMicros


def _money(payload: dict[str, object], key: str) -> MoneyMicros:
    """Read a micros integer from a replayed event payload as ``MoneyMicros``.

    Args:
        payload: The event payload to read.
        key: The payload key naming a micros integer.

    Returns:
        The value wrapped as :class:`MoneyMicros`.
    """
    return MoneyMicros(cast("int", payload[key]))


def _pending_from_payload(payload: dict[str, object]) -> PendingFloorLower:
    """Reconstruct a :class:`PendingFloorLower` from a requested-event payload.

    Args:
        payload: A ``FloorLowerRequested`` event's payload.

    Returns:
        The pending lowering the payload records.
    """
    return PendingFloorLower(
        nonce=cast("str", payload["nonce"]),
        ready_at=cast("int", payload["ready_at"]),
        target_floor_micros=_money(payload, "target_floor_micros"),
    )


class FloorGovernance:
    """Governs raises, cool-off-gated lowerings, and the profit ratchet.

    The current floor, the equity high-water mark, and any pending lowering are
    all derived from -- and exactly reconstructable via :meth:`from_events`
    from -- the events recorded through the injected writer. Every fresh
    high-water crossing ledgers an ``EquityHighWaterMarkAdvanced`` event
    carrying the new *absolute* mark, so replay reconstructs the mark exactly
    from that event alone, fully decoupled from whether the ratchet fired. The
    ``FloorRatchetApplied`` events drive only the floor on replay, never the
    mark.

    Backward-compatibility caveat: histories recorded *before* this fix
    (issue #125) carry no ``EquityHighWaterMarkAdvanced`` events, so they
    replay with a zero high-water mark -- a deliberately more conservative
    fallback than the old ratchet-sum reconstruction. Because the floor moves
    only upward and the profit-sweep advisory is non-blocking, a zeroed mark
    can only re-fire that advisory or over-ratchet the floor upward on the next
    crossing; it can never weaken protection. The ``max(replayed, ratchet_sum)``
    hybrid was rejected to keep the mark fully decoupled from the ratchet.
    """

    def __init__(
        self,
        *,
        initial_floor: MoneyMicros,
        ratchet_ppm: int,
        profit_sweep_threshold: MoneyMicros,
        mode_machine: ModeStateMachine,
        dispatcher: AlertDispatcher,
        writer: KernelLedgerWriter,
        clock: Callable[[], int],
        cool_off_seconds: int = DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS,
    ) -> None:
        """Initialize floor governance.

        Args:
            initial_floor: The starting equity floor, in micros.
            ratchet_ppm: The share of each fresh equity gain the floor ratchets
                up by, in parts per million.
            profit_sweep_threshold: The gain-since-high-water-mark above which a
                profit-sweep advisory fires, in micros.
            mode_machine: The operating-mode state machine a completed lowering
                demotes out of the live modes.
            dispatcher: The alert dispatcher request/confirm/advisory alerts fan
                out through.
            writer: The seam every governance event is recorded through.
            clock: A zero-argument callable returning the current epoch second.
            cool_off_seconds: The floor-lower cool-off, in seconds. Defaults to
                :data:`DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS` (48 hours).
        """
        self._floor = initial_floor
        self._ratchet_ppm = ratchet_ppm
        self._profit_sweep_threshold = profit_sweep_threshold
        self._mode_machine = mode_machine
        self._dispatcher = dispatcher
        self._writer = writer
        self._clock = clock
        self._cool_off_seconds = cool_off_seconds
        self._high_water_mark = MoneyMicros(0)
        self._pending: PendingFloorLower | None = None

    @classmethod
    def from_events(
        cls,
        events: Iterable[Event],
        *,
        ratchet_ppm: int,
        profit_sweep_threshold: MoneyMicros,
        mode_machine: ModeStateMachine,
        dispatcher: AlertDispatcher,
        writer: KernelLedgerWriter,
        clock: Callable[[], int],
        cool_off_seconds: int = DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS,
    ) -> FloorGovernance:
        """Rebuild governance by replaying its floor/high-water/pending state.

        The floor and any still-live pending lowering are derived purely from
        ``events``: a lowering that was requested *and* confirmed before the
        replay is not resurrected as pending, and the original ``ready_at``
        still gates any re-confirmation. The high-water mark is recovered
        *exactly* -- and independently of the ratchet -- by absolutely setting
        it from each ``EquityHighWaterMarkAdvanced`` event's ``new_mark_micros``.
        Pre-#125 histories carrying no such event replay with a zero mark; see
        the class docstring for that conservative-only backward-compat caveat.

        Args:
            events: The event history to replay state from.
            ratchet_ppm: The profit-ratchet share, in ppm.
            profit_sweep_threshold: The profit-sweep advisory threshold, in
                micros.
            mode_machine: The operating-mode state machine to adopt.
            dispatcher: The alert dispatcher the rebuilt instance uses.
            writer: The writer the rebuilt instance records new events to.
            clock: The rebuilt instance's injected clock.
            cool_off_seconds: The floor-lower cool-off, in seconds.

        Returns:
            A :class:`FloorGovernance` whose floor, high-water mark, and pending
            lowering reflect ``events``.
        """
        governance = cls(
            initial_floor=MoneyMicros(0),
            ratchet_ppm=ratchet_ppm,
            profit_sweep_threshold=profit_sweep_threshold,
            mode_machine=mode_machine,
            dispatcher=dispatcher,
            writer=writer,
            clock=clock,
            cool_off_seconds=cool_off_seconds,
        )
        for event in events:
            governance._absorb_replayed_event(event)
        return governance

    @property
    def current_floor_micros(self) -> MoneyMicros:
        """Return the current equity floor, in micros."""
        return self._floor

    @property
    def pending_lower(self) -> PendingFloorLower | None:
        """Return the pending lowering, or ``None`` if none is pending."""
        return self._pending

    def raise_floor(
        self, new_floor: MoneyMicros, *, origin: ChangeOrigin = ChangeOrigin.CLI
    ) -> None:
        """Raise the floor to ``new_floor`` immediately, recording it.

        Permitted from either origin, since a raise can never increase risk.

        Args:
            new_floor: The strictly-higher floor to apply, in micros.
            origin: Who requested the raise. Defaults to :attr:`ChangeOrigin.CLI`.

        Raises:
            ValueError: If ``new_floor`` is not strictly above the current floor
                -- lowering the floor must go through :meth:`request_lower`.
        """
        if new_floor <= self._floor:
            raise ValueError(
                f"raise_floor requires a strictly higher floor than {self._floor}; "
                f"use request_lower to lower it to {new_floor}"
            )
        previous = self._floor
        self._floor = new_floor
        self._record(
            _FLOOR_RAISED_EVENT,
            {
                "previous_floor_micros": previous.value,
                "new_floor_micros": new_floor.value,
                "origin": origin.name,
            },
        )

    def request_lower(
        self, new_floor: MoneyMicros, *, origin: ChangeOrigin = ChangeOrigin.CLI
    ) -> PendingFloorLower:
        """Begin a cool-off-gated lowering, issuing a single-use nonce.

        The ``ready_at`` deadline is computed once, here, so no later clock move
        can shorten the cool-off.

        Args:
            new_floor: The floor the lowering will apply on confirmation.
            origin: Who requested the lowering. Only :attr:`ChangeOrigin.CLI`
                may lower; a dashboard request records a refusal and raises.

        Returns:
            The created :class:`PendingFloorLower`.

        Raises:
            ForbiddenOriginError: If ``origin`` is not the CLI. A
                ``FloorLowerRefused`` event is recorded first.
            LoweringAlreadyPendingError: If a lowering is already pending; the
                existing pending lowering (nonce and ``ready_at``) is untouched.
        """
        if origin is not ChangeOrigin.CLI:
            self._record_refused(
                _REFUSED_FORBIDDEN_ORIGIN,
                {"target_floor_micros": new_floor.value, "origin": origin.name},
            )
            raise ForbiddenOriginError(
                f"floor lowering may only be requested from the CLI, not {origin.name}"
            )
        if self._pending is not None:
            raise LoweringAlreadyPendingError(
                "a floor lowering is already pending confirmation"
            )
        requested_at = self._clock()
        ready_at = requested_at + self._cool_off_seconds
        pending = PendingFloorLower(
            nonce=secrets.token_hex(_NONCE_BYTES),
            ready_at=ready_at,
            target_floor_micros=new_floor,
        )
        self._pending = pending
        self._record(
            _FLOOR_LOWER_REQUESTED_EVENT,
            {
                "nonce": pending.nonce,
                "target_floor_micros": new_floor.value,
                "requested_at": requested_at,
                "ready_at": ready_at,
                "origin": origin.name,
            },
        )
        self._dispatcher.dispatch(
            AlertType.FLOOR_CHANGE_REQUEST,
            f"floor lowering to {new_floor} requested; confirmable at {ready_at}",
        )
        return pending

    def confirm_lower(self, *, nonce: str) -> None:
        """Confirm a pending lowering once its cool-off has elapsed.

        On success, applies the floor, demotes any live mode down to ``PAPER``,
        fires a ``FLOOR_CHANGE_REQUEST`` alert, and clears the pending lowering.

        Args:
            nonce: The single-use nonce issued by :meth:`request_lower`.

        Raises:
            NoPendingLowerError: If no lowering is pending.
            CoolOffActiveError: If confirmed before ``ready_at``; a refusal is
                recorded and the pending lowering is untouched.
            NonceMismatchError: If ``nonce`` does not match; a refusal is
                recorded and the pending lowering is untouched.
        """
        pending = self._pending
        if pending is None:
            raise NoPendingLowerError("no floor lowering is pending confirmation")
        self._reject_unless_confirmable(nonce, pending)
        self._apply_confirmed_lower(pending)

    def observe_equity(self, equity: MoneyMicros) -> None:
        """Ratchet the floor and advise on a fresh equity high-water mark.

        A no-op unless ``equity`` is strictly above the current high-water mark;
        the ratchet never lowers the floor and never fires twice for the same
        peak. Every fresh crossing records an ``EquityHighWaterMarkAdvanced``
        event carrying the new absolute mark -- unconditionally, unlike the
        ratchet -- so :meth:`from_events` can reconstruct the mark exactly.

        Args:
            equity: The observed worst-case equity, in micros.
        """
        if equity <= self._high_water_mark:
            return
        gain = equity - self._high_water_mark
        self._maybe_advise_profit_sweep(gain)
        self._maybe_ratchet(gain)
        self._record_mark_advance(self._high_water_mark, equity)
        self._high_water_mark = equity

    def _reject_unless_confirmable(
        self, nonce: str, pending: PendingFloorLower
    ) -> None:
        """Record a refusal and raise if the pending lowering cannot confirm.

        Args:
            nonce: The nonce supplied to :meth:`confirm_lower`.
            pending: The pending lowering being confirmed.

        Raises:
            CoolOffActiveError: If the cool-off has not yet elapsed.
            NonceMismatchError: If ``nonce`` does not match the pending nonce.
        """
        if self._clock() < pending.ready_at:
            self._record_refused(
                _REFUSED_COOL_OFF_ACTIVE,
                {"nonce": pending.nonce, "ready_at": pending.ready_at},
            )
            raise CoolOffActiveError(
                f"floor lowering is not confirmable until {pending.ready_at}"
            )
        if not hmac.compare_digest(nonce, pending.nonce):
            self._record_refused(_REFUSED_NONCE_MISMATCH, {"reason_detail": "nonce"})
            raise NonceMismatchError("floor-lowering nonce does not match")

    def _apply_confirmed_lower(self, pending: PendingFloorLower) -> None:
        """Apply a confirmed lowering: floor, demotion, alert, event, clear.

        Args:
            pending: The pending lowering being confirmed.
        """
        previous = self._floor
        self._floor = pending.target_floor_micros
        demoted_to = self._demote_out_of_live()
        self._dispatcher.dispatch(
            AlertType.FLOOR_CHANGE_REQUEST,
            f"floor lowered to {self._floor}; mode is {demoted_to}",
        )
        self._record(
            _FLOOR_LOWER_CONFIRMED_EVENT,
            {
                "previous_floor_micros": previous.value,
                "new_floor_micros": self._floor.value,
                "demoted_to": demoted_to,
            },
        )
        self._pending = None

    def _demote_out_of_live(self) -> str:
        """Demote one rung at a time until out of the live modes.

        A no-op from ``PAPER``/``RESEARCH`` or any safety mode.

        Returns:
            The name of the mode landed on after demotion.
        """
        while self._mode_machine.mode in _LIVE_MODES:
            self._mode_machine.demote_one_rung()
        return self._mode_machine.mode.name

    def _maybe_advise_profit_sweep(self, gain: MoneyMicros) -> None:
        """Fire a profit-sweep advisory when ``gain`` exceeds the threshold.

        Args:
            gain: The fresh gain since the previous high-water mark, in micros.
        """
        if gain > self._profit_sweep_threshold:
            self._dispatcher.dispatch(
                AlertType.PROFIT_SWEEP_ADVISORY,
                f"profit of {gain} since the high-water mark exceeds the "
                f"sweep threshold {self._profit_sweep_threshold}",
            )

    def _maybe_ratchet(self, gain: MoneyMicros) -> None:
        """Ratchet the floor up by the floored ppm share of ``gain``.

        Args:
            gain: The fresh gain since the previous high-water mark, in micros.
        """
        increment = _ppm_of(gain.value, self._ratchet_ppm)
        if increment <= 0:
            return
        previous = self._floor
        self._floor = self._floor + MoneyMicros(increment)
        self._record(
            _FLOOR_RATCHET_APPLIED_EVENT,
            {
                "previous_floor_micros": previous.value,
                "new_floor_micros": self._floor.value,
                "gain_micros": gain.value,
                "increment_micros": increment,
            },
        )

    def _record_mark_advance(
        self, previous: MoneyMicros, new_mark: MoneyMicros
    ) -> None:
        """Record an ``EquityHighWaterMarkAdvanced`` event for a fresh crossing.

        The mark is recorded absolutely (not as a delta) so replay can restore
        it exactly regardless of whether the ratchet fired for this crossing.

        Args:
            previous: The high-water mark being superseded, in micros.
            new_mark: The fresh, strictly-higher high-water mark, in micros.
        """
        self._record(
            _EQUITY_HIGH_WATER_MARK_ADVANCED_EVENT,
            {
                "previous_mark_micros": previous.value,
                "new_mark_micros": new_mark.value,
            },
        )

    def _absorb_replayed_event(self, event: Event) -> None:
        """Fold one replayed event into the floor/high-water/pending state.

        ``FloorRatchetApplied`` drives only the floor; the high-water mark is
        set absolutely from ``EquityHighWaterMarkAdvanced`` alone. Unrecognized
        event types (including refusals, which never mutate state) are ignored.

        Args:
            event: The recorded event to replay.
        """
        payload = event.payload
        event_type = event.event_type
        if event_type in (_FLOOR_RAISED_EVENT, _FLOOR_RATCHET_APPLIED_EVENT):
            self._floor = _money(payload, "new_floor_micros")
        elif event_type == _EQUITY_HIGH_WATER_MARK_ADVANCED_EVENT:
            self._high_water_mark = _money(payload, "new_mark_micros")
        elif event_type == _FLOOR_LOWER_REQUESTED_EVENT:
            self._pending = _pending_from_payload(payload)
        elif event_type == _FLOOR_LOWER_CONFIRMED_EVENT:
            self._floor = _money(payload, "new_floor_micros")
            self._pending = None

    def _record_refused(self, reason: str, context: dict[str, object]) -> None:
        """Record a ``FloorLowerRefused`` event carrying ``reason`` and context.

        Args:
            reason: The refusal reason (one of the ``_REFUSED_*`` constants).
            context: Extra reason-specific fields to include in the payload.
        """
        self._record(_FLOOR_LOWER_REFUSED_EVENT, {"reason": reason, **context})

    def _record(self, event_type: str, payload: dict[str, object]) -> None:
        """Record one governance event through the writer.

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
