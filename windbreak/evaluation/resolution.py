"""Market-resolution outcomes for the evaluation harness (SPEC-EPIC_07, #49).

A *resolution* is the ground-truth answer a binary event market settled to:
``YES`` or ``NO``. The evaluation harness scores each forecast against the
resolution of the market it named, so this module owns the single small typed
vocabulary (:class:`ResolutionOutcome`) plus the loader
(:func:`resolutions_from_fixture`) that turns the raw JSON ``resolutions`` block
of a known-answer fixture into a ticker-keyed mapping of those outcomes.

This module is deliberately dependency-free within the package: it imports
nothing from :mod:`windbreak.evaluation.registry` or
:mod:`windbreak.evaluation.report`. That keeps the intra-package dependency
one-way (report -> registry -> resolution) and lets the registry reference
:class:`ResolutionOutcome` in type position without risking an import cycle.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from typing import Any

#: JSON key holding the list of ``{market_ticker, outcome}`` resolution entries.
_RESOLUTIONS_KEY = "resolutions"
#: JSON field naming the market a resolution belongs to.
_TICKER_FIELD = "market_ticker"
#: JSON field carrying the settled outcome token (``"yes"`` / ``"no"``).
_OUTCOME_FIELD = "outcome"
#: JSON key holding the ordered settlement-event stream (issue #50).
_SETTLEMENT_EVENTS_KEY = "settlement_events"
#: JSON field carrying an event's strictly-increasing global sequence number.
_SEQUENCE_NUMBER_FIELD = "sequence_number"
#: JSON field naming a settlement event's type token.
_EVENT_TYPE_FIELD = "event_type"
#: Reversal count a market carries before it has ever been reversed.
_INITIAL_REVERSAL_COUNT = 0


class ResolutionOutcome(enum.Enum):
    """The settled ground-truth outcome of a binary event market.

    A binary market resolves to exactly one of two states, encoded here by the
    lowercase JSON tokens the fixtures use so that ``ResolutionOutcome(token)``
    round-trips a raw string straight into the typed value.
    """

    YES = "yes"
    NO = "no"


def _outcome_from_token(token: str) -> ResolutionOutcome:
    """Parse a raw ``outcome`` token into a :class:`ResolutionOutcome`.

    Args:
        token: The raw outcome string from a fixture resolution entry.

    Returns:
        The matching :class:`ResolutionOutcome` member.

    Raises:
        ValueError: If ``token`` is not one of the known ``outcome`` values;
            the message names the ``outcome`` field for locatability.
    """
    try:
        return ResolutionOutcome(token)
    except ValueError as exc:
        raise ValueError(
            f"unknown resolution outcome: {token!r} "
            f"(expected one of {[member.value for member in ResolutionOutcome]})"
        ) from exc


def resolutions_from_fixture(
    fixture: Mapping[str, Any],
) -> Mapping[str, ResolutionOutcome]:
    """Build a ticker-keyed resolution mapping from a fixture payload.

    Reads the fixture's ``resolutions`` list -- each entry a
    ``{"market_ticker": ..., "outcome": ...}`` object -- and returns a mapping
    from each market ticker to its typed :class:`ResolutionOutcome`.

    Args:
        fixture: The decoded fixture payload, carrying a ``resolutions`` list.

    Returns:
        A mapping from ``market_ticker`` to its :class:`ResolutionOutcome`.

    Raises:
        ValueError: If an ``outcome`` token is unknown (message names
            ``outcome``), or if a ``market_ticker`` appears more than once
            (message names ``market_ticker``).
    """
    resolutions: dict[str, ResolutionOutcome] = {}
    for entry in fixture[_RESOLUTIONS_KEY]:
        ticker = entry[_TICKER_FIELD]
        outcome = _outcome_from_token(entry[_OUTCOME_FIELD])
        if ticker in resolutions:
            raise ValueError(f"duplicate market_ticker in resolutions: {ticker!r}")
        resolutions[ticker] = outcome
    return resolutions


class ResolutionStatus(enum.Enum):
    """The lifecycle state of a market under the settlement-event tracker.

    A market progresses UNRESOLVED -> RESOLVED on a settlement, RESOLVED ->
    REVERSED when that settlement is disputed and reversed, and REVERSED ->
    RESOLVED again when it is re-settled with a corrected outcome. Only a
    RESOLVED market carries a current settled :class:`ResolutionOutcome`; the
    other two states carry ``None``.
    """

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    REVERSED = "reversed"


class SettlementEventType(enum.Enum):
    """The kind of settlement-ledger event, encoded by its fixture token.

    ``SETTLEMENT`` carries a settled outcome; ``SETTLEMENT_REVERSED`` clears a
    prior settlement and carries no outcome. The lowercase token values match
    the fixture stream so ``SettlementEventType(token)`` parses a raw string.
    """

    SETTLEMENT = "settlement"
    SETTLEMENT_REVERSED = "settlement_reversed"


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    """One immutable entry in a market's settlement ledger.

    Attributes:
        sequence_number: The event's position in the strictly-increasing global
            settlement stream.
        event_type: Whether this event settles a market or reverses a prior
            settlement.
        market_ticker: The market the event applies to.
        outcome: The settled outcome for a ``SETTLEMENT``; ``None`` for a
            ``SETTLEMENT_REVERSED``.
    """

    sequence_number: int
    event_type: SettlementEventType
    market_ticker: str
    outcome: ResolutionOutcome | None

    def __post_init__(self) -> None:
        """Validate the sequence number and outcome/type coherence.

        Raises:
            TypeError: If ``sequence_number`` is a ``bool`` (an ``int`` subclass
                that must not masquerade as a position) or is not an ``int`` at
                all -- mirroring the ``_IntUnit`` guard; the message names the
                ``sequence_number`` field.
            ValueError: If a ``SETTLEMENT`` carries no ``outcome`` or a
                ``SETTLEMENT_REVERSED`` carries one; the message names the
                ``outcome`` field.
        """
        number = self.sequence_number
        if isinstance(number, bool) or not isinstance(number, int):
            raise TypeError(
                f"sequence_number requires a non-bool int, got {type(number).__name__}"
            )
        self._validate_outcome_coherence()

    def _validate_outcome_coherence(self) -> None:
        """Check that the presence of an ``outcome`` matches the event type.

        Raises:
            ValueError: If a ``SETTLEMENT`` has ``outcome=None`` or a
                ``SETTLEMENT_REVERSED`` has a non-``None`` ``outcome``; the
                message names the ``outcome`` field.
        """
        settlement_needs_outcome = self.event_type is SettlementEventType.SETTLEMENT
        has_outcome = self.outcome is not None
        if settlement_needs_outcome and not has_outcome:
            raise ValueError(
                "SETTLEMENT event requires a non-None outcome; outcome was None"
            )
        if not settlement_needs_outcome and has_outcome:
            raise ValueError(
                "SETTLEMENT_REVERSED event requires outcome to be None; "
                "a reversal clears the outcome rather than carrying a new one"
            )


@dataclass(frozen=True, slots=True)
class MarketResolution:
    """A market's derived resolution state after folding its settlement stream.

    Attributes:
        market_ticker: The market this resolution describes.
        status: The market's current :class:`ResolutionStatus`.
        outcome: The current settled outcome, present iff ``status`` is
            ``RESOLVED`` and ``None`` otherwise.
        reversal_count: How many times the market has been reversed so far.
    """

    market_ticker: str
    status: ResolutionStatus
    outcome: ResolutionOutcome | None
    reversal_count: int

    def __post_init__(self) -> None:
        """Enforce the outcome-present-iff-RESOLVED invariant.

        Raises:
            ValueError: If ``outcome`` is present without ``status`` being
                ``RESOLVED``, or absent while it is; the message names the
                ``outcome`` field.
        """
        is_resolved = self.status is ResolutionStatus.RESOLVED
        has_outcome = self.outcome is not None
        if is_resolved != has_outcome:
            raise ValueError(
                "MarketResolution requires outcome to be present iff status is "
                f"RESOLVED; got status={self.status.value}, outcome={self.outcome!r}"
            )


def _unresolved(market_ticker: str) -> MarketResolution:
    """Build the default UNRESOLVED resolution for a never-settled market.

    Args:
        market_ticker: The market to describe.

    Returns:
        A :class:`MarketResolution` in the ``UNRESOLVED`` ground state.
    """
    return MarketResolution(
        market_ticker=market_ticker,
        status=ResolutionStatus.UNRESOLVED,
        outcome=None,
        reversal_count=_INITIAL_REVERSAL_COUNT,
    )


def _next_sequence(previous: int | None, current: int) -> int:
    """Return ``current`` after checking it strictly exceeds ``previous``.

    Args:
        previous: The prior event's sequence number, or ``None`` before any
            event has been folded.
        current: This event's sequence number.

    Returns:
        ``current``, to become the new ``previous`` for the next event.

    Raises:
        ValueError: If ``current`` does not strictly exceed ``previous``; the
            message names the ``sequence_number`` field.
    """
    if previous is not None and current <= previous:
        raise ValueError(
            "settlement_events sequence_number must be strictly increasing; "
            f"got {current} after {previous}"
        )
    return current


def _apply_settlement(
    current: MarketResolution, event: SettlementEvent
) -> MarketResolution:
    """Fold a ``SETTLEMENT`` event onto a market's current resolution.

    Args:
        current: The market's resolution before this event.
        event: The settlement event to apply.

    Returns:
        The market resolved to the event's outcome, preserving the running
        reversal count.

    Raises:
        ValueError: If the market is already ``RESOLVED`` with no intervening
            reversal; the message names the offending market.
    """
    if current.status is ResolutionStatus.RESOLVED:
        raise ValueError(
            f"cannot settle already-RESOLVED market {current.market_ticker!r} "
            "without an intervening reversal"
        )
    return MarketResolution(
        market_ticker=current.market_ticker,
        status=ResolutionStatus.RESOLVED,
        outcome=event.outcome,
        reversal_count=current.reversal_count,
    )


def _apply_reversal(current: MarketResolution) -> MarketResolution:
    """Fold a ``SETTLEMENT_REVERSED`` event onto a market's current resolution.

    Args:
        current: The market's resolution before this event.

    Returns:
        The market moved to ``REVERSED`` with its outcome cleared and its
        reversal count incremented.

    Raises:
        ValueError: If the market is not currently ``RESOLVED`` (nothing to
            reverse); the message names the offending market.
    """
    if current.status is not ResolutionStatus.RESOLVED:
        raise ValueError(
            f"cannot reverse market {current.market_ticker!r} in state "
            f"{current.status.value}; only a RESOLVED market can be reversed"
        )
    return MarketResolution(
        market_ticker=current.market_ticker,
        status=ResolutionStatus.REVERSED,
        outcome=None,
        reversal_count=current.reversal_count + 1,
    )


def _fold_event(current: MarketResolution, event: SettlementEvent) -> MarketResolution:
    """Dispatch one settlement event to its transition helper.

    Args:
        current: The market's resolution before this event.
        event: The event to apply.

    Returns:
        The market's resolution after applying the event.
    """
    if event.event_type is SettlementEventType.SETTLEMENT:
        return _apply_settlement(current, event)
    return _apply_reversal(current)


@dataclass(frozen=True, slots=True)
class ResolutionTracker:
    """A total view of every market's resolution folded from a settlement stream.

    Attributes:
        resolutions: The derived :class:`MarketResolution` for each market that
            appears in the folded stream, keyed by ``market_ticker``.
    """

    resolutions: Mapping[str, MarketResolution]

    @classmethod
    def from_ledger(cls, events: Iterable[SettlementEvent]) -> ResolutionTracker:
        """Fold an ordered settlement stream into a resolution tracker.

        Args:
            events: The settlement events, in strictly-increasing
                ``sequence_number`` order.

        Returns:
            A tracker holding each seen market's derived resolution.

        Raises:
            ValueError: If two events tie or decrease in ``sequence_number``, or
                if any event is an illegal transition for its market's current
                state.
        """
        resolutions: dict[str, MarketResolution] = {}
        previous_sequence: int | None = None
        for event in events:
            previous_sequence = _next_sequence(previous_sequence, event.sequence_number)
            current = resolutions.get(event.market_ticker)
            if current is None:
                current = _unresolved(event.market_ticker)
            resolutions[event.market_ticker] = _fold_event(current, event)
        return cls(resolutions=resolutions)

    def get(self, market_ticker: str) -> MarketResolution:
        """Return a market's resolution, defaulting an unseen market to UNRESOLVED.

        Args:
            market_ticker: The market to look up.

        Returns:
            The market's folded :class:`MarketResolution`, or the ``UNRESOLVED``
            ground state for a market that never appeared in the stream.
        """
        resolution = self.resolutions.get(market_ticker)
        if resolution is None:
            return _unresolved(market_ticker)
        return resolution

    def resolved_outcomes(self) -> Mapping[str, ResolutionOutcome]:
        """Return the settled outcome of every currently-RESOLVED market.

        REVERSED and UNRESOLVED markets are excluded: they carry no current
        settled outcome. The filter keys on ``outcome is not None``, which the
        :class:`MarketResolution` invariant makes exactly equivalent to
        ``status is RESOLVED``.

        Returns:
            A mapping from ``market_ticker`` to its current settled outcome, in
            the same shape as :func:`resolutions_from_fixture`.
        """
        outcomes: dict[str, ResolutionOutcome] = {}
        for ticker, resolution in self.resolutions.items():
            outcome = resolution.outcome
            if outcome is not None:
                outcomes[ticker] = outcome
        return outcomes


def _settlement_event_type_from_token(token: str) -> SettlementEventType:
    """Parse a raw ``event_type`` token into a :class:`SettlementEventType`.

    Args:
        token: The raw event-type string from a fixture settlement entry.

    Returns:
        The matching :class:`SettlementEventType` member.

    Raises:
        ValueError: If ``token`` is not a known ``event_type`` value; the
            message names the ``event_type`` field for locatability.
    """
    try:
        return SettlementEventType(token)
    except ValueError as exc:
        raise ValueError(
            f"unknown settlement event_type: {token!r} "
            f"(expected one of {[member.value for member in SettlementEventType]})"
        ) from exc


def _outcome_from_optional_token(token: str | None) -> ResolutionOutcome | None:
    """Parse an optional outcome token, passing ``None`` straight through.

    Args:
        token: The raw outcome token, or ``None`` for a reversal event.

    Returns:
        The parsed :class:`ResolutionOutcome`, or ``None`` when ``token`` is
        ``None``.

    Raises:
        ValueError: If a non-``None`` ``token`` is unknown; the message names
            the ``outcome`` field.
    """
    if token is None:
        return None
    return _outcome_from_token(token)


def settlement_events_from_fixture(
    fixture: Mapping[str, Any],
) -> tuple[SettlementEvent, ...]:
    """Build the ordered settlement-event stream from a fixture payload.

    Reads the fixture's ``settlement_events`` list -- each entry a
    ``{sequence_number, event_type, market_ticker, outcome}`` object -- and
    returns one typed :class:`SettlementEvent` per entry, preserving stream
    order.

    Args:
        fixture: The decoded fixture payload, carrying a ``settlement_events``
            list.

    Returns:
        The settlement events in the order they appear in the fixture.

    Raises:
        ValueError: If an ``event_type`` token is unknown (message names
            ``event_type``) or an ``outcome`` token is unknown (message names
            ``outcome``).
        TypeError: If a ``sequence_number`` is a ``bool`` or not an ``int``.
    """
    events: list[SettlementEvent] = []
    for entry in fixture[_SETTLEMENT_EVENTS_KEY]:
        events.append(
            SettlementEvent(
                sequence_number=entry[_SEQUENCE_NUMBER_FIELD],
                event_type=_settlement_event_type_from_token(entry[_EVENT_TYPE_FIELD]),
                market_ticker=entry[_TICKER_FIELD],
                outcome=_outcome_from_optional_token(entry[_OUTCOME_FIELD]),
            )
        )
    return tuple(events)
