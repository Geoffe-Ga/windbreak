"""Failing-first tests for the resolution tracker (issue #50, RED).

`ResolutionStatus`, `SettlementEventType`, `SettlementEvent`,
`MarketResolution`, `ResolutionTracker`, and `settlement_events_from_fixture`
do not exist yet in `windbreak.evaluation.resolution`, so every import below
fails collection with `ImportError: cannot import name '...' from
'windbreak.evaluation.resolution'` -- the expected Gate 1 RED state for issue
#50. `ResolutionOutcome` and `resolutions_from_fixture` (issue #49) already
exist and are exercised here only as a regression check.

Pins the settlement-event resolution tracker (SPEC-EPIC_07, #50):

- A market's resolution is a pure fold over a stream of `SettlementEvent`s
  consumed in strictly-increasing global `sequence_number` order:
  UNRESOLVED --SETTLEMENT--> RESOLVED(outcome);
  RESOLVED --SETTLEMENT_REVERSED--> REVERSED(outcome=None, reversal_count+1);
  REVERSED --SETTLEMENT--> RESOLVED(corrected outcome).
- `ResolutionTracker.get(ticker)` is total: an unseen ticker returns
  `MarketResolution(status=UNRESOLVED, outcome=None, reversal_count=0)`.
- `ResolutionTracker.resolved_outcomes()` includes only RESOLVED markets --
  REVERSED markets (mid-dispute, no current settled outcome) are excluded,
  same shape as `resolutions_from_fixture`'s output.
- Every illegal transition and malformed event raises with the offending
  field or ticker named in the message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

#: The epic-wide known-answer fixture shared by issues #49-#55; see its own
#: "description" and "settlement_events" / "expected.reversal" keys for the
#: hand-computed settlement stream this suite pins against.
SYNTHETIC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_known_answer.json"
)

#: The three disjoint tickers the fixture's settlement_events stream covers.
_TICKER_T1 = "KXEXAMPLE-26-T1"
_TICKER_T2 = "KXEXAMPLE-26-T2"
_TICKER_T3 = "KXEXAMPLE-26-T3"


def _load_fixture() -> dict[str, Any]:
    """Load and JSON-decode the shared synthetic known-answer fixture.

    Returns:
        The decoded fixture payload.
    """
    return json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Verbatim issue scenario: settle, reverse, resettle differently.
# ---------------------------------------------------------------------------


def test_reversed_and_resettled_market_ends_resolved_with_corrected_outcome() -> None:
    """T1 settles YES, is reversed, then resettles NO: the tracker ends at
    RESOLVED/NO with reversal_count 1 -- the corrected outcome, not the
    original one.
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        ResolutionStatus,
        ResolutionTracker,
        settlement_events_from_fixture,
    )

    fixture = _load_fixture()
    events = settlement_events_from_fixture(fixture)

    tracker = ResolutionTracker.from_ledger(events)
    resolution = tracker.get(_TICKER_T1)

    assert resolution.market_ticker == _TICKER_T1
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.outcome is ResolutionOutcome.NO
    assert resolution.reversal_count == 1


# ---------------------------------------------------------------------------
# 2. Downstream-recompute proof: the same ticker resolves differently
#    depending on how much of the stream has been folded.
# ---------------------------------------------------------------------------


def test_resolved_outcomes_recomputes_from_a_partial_vs_full_event_stream() -> None:
    """Folding only the first settlement event yields T1=YES in
    `resolved_outcomes()`; folding the full stream yields T1=NO (the
    corrected, post-reversal outcome) and T3 has dropped out entirely
    (REVERSED markets are not "resolved").
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        ResolutionTracker,
        settlement_events_from_fixture,
    )

    fixture = _load_fixture()
    events = settlement_events_from_fixture(fixture)

    partial_tracker = ResolutionTracker.from_ledger(events[:1])
    assert partial_tracker.resolved_outcomes() == {_TICKER_T1: ResolutionOutcome.YES}

    full_tracker = ResolutionTracker.from_ledger(events)
    full_outcomes = full_tracker.resolved_outcomes()

    assert full_outcomes[_TICKER_T1] is ResolutionOutcome.NO
    assert _TICKER_T3 not in full_outcomes
    assert full_outcomes == {
        _TICKER_T1: ResolutionOutcome.NO,
        _TICKER_T2: ResolutionOutcome.YES,
    }


def test_plain_settlement_with_no_reversal_resolves_once() -> None:
    """T2 settles YES exactly once: RESOLVED/YES/reversal_count 0."""
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        ResolutionStatus,
        ResolutionTracker,
        settlement_events_from_fixture,
    )

    fixture = _load_fixture()
    events = settlement_events_from_fixture(fixture)

    tracker = ResolutionTracker.from_ledger(events)
    resolution = tracker.get(_TICKER_T2)

    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.outcome is ResolutionOutcome.YES
    assert resolution.reversal_count == 0


def test_reversed_market_with_no_resettlement_has_none_outcome() -> None:
    """T3 settles NO then is reversed with no resettlement yet: REVERSED,
    `outcome` is `None` (mid-dispute -- there is no current settled answer),
    reversal_count 1.
    """
    from windbreak.evaluation.resolution import (
        ResolutionStatus,
        ResolutionTracker,
        settlement_events_from_fixture,
    )

    fixture = _load_fixture()
    events = settlement_events_from_fixture(fixture)

    tracker = ResolutionTracker.from_ledger(events)
    resolution = tracker.get(_TICKER_T3)

    assert resolution.status is ResolutionStatus.REVERSED
    assert resolution.outcome is None
    assert resolution.reversal_count == 1


def test_get_is_total_and_defaults_unseen_ticker_to_unresolved() -> None:
    """A ticker with no settlement events at all returns
    `MarketResolution(status=UNRESOLVED, outcome=None, reversal_count=0)` --
    `get` never raises `KeyError`.
    """
    from windbreak.evaluation.resolution import (
        ResolutionStatus,
        ResolutionTracker,
        settlement_events_from_fixture,
    )

    fixture = _load_fixture()
    events = settlement_events_from_fixture(fixture)
    tracker = ResolutionTracker.from_ledger(events)

    resolution = tracker.get("KXEXAMPLE-26-NEVER-SEEN")

    assert resolution.market_ticker == "KXEXAMPLE-26-NEVER-SEEN"
    assert resolution.status is ResolutionStatus.UNRESOLVED
    assert resolution.outcome is None
    assert resolution.reversal_count == 0


# ---------------------------------------------------------------------------
# 3. settlement_events_from_fixture: direct loader unit tests.
# ---------------------------------------------------------------------------


def test_settlement_events_from_fixture_parses_every_field_in_stream_order() -> None:
    """The loader returns one `SettlementEvent` per entry, in stream order,
    with the outcome token parsed through the existing outcome parser.
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        SettlementEvent,
        SettlementEventType,
        settlement_events_from_fixture,
    )

    fixture = _load_fixture()

    events = settlement_events_from_fixture(fixture)

    assert len(events) == 6
    assert all(isinstance(event, SettlementEvent) for event in events)
    assert [event.sequence_number for event in events] == [1, 2, 3, 4, 5, 6]
    assert events[0].event_type is SettlementEventType.SETTLEMENT
    assert events[0].market_ticker == _TICKER_T1
    assert events[0].outcome is ResolutionOutcome.YES
    assert events[1].event_type is SettlementEventType.SETTLEMENT_REVERSED
    assert events[1].outcome is None


def test_settlement_events_from_fixture_rejects_unknown_event_type() -> None:
    """An `event_type` token other than `settlement`/`settlement_reversed`
    raises `ValueError` naming the `event_type` field.
    """
    from windbreak.evaluation.resolution import settlement_events_from_fixture

    fixture = {
        "settlement_events": [
            {
                "sequence_number": 1,
                "event_type": "settlement_pending",
                "market_ticker": "MKT-X",
                "outcome": None,
            }
        ]
    }

    with pytest.raises(ValueError, match="event_type"):
        settlement_events_from_fixture(fixture)


# ---------------------------------------------------------------------------
# 4. SettlementEvent construction invariants.
# ---------------------------------------------------------------------------


def test_settlement_event_requires_outcome_for_settlement() -> None:
    """A `SETTLEMENT` event with `outcome=None` raises `ValueError` naming
    the `outcome` field -- a settlement without a settled answer is
    incoherent.
    """
    from windbreak.evaluation.resolution import SettlementEvent, SettlementEventType

    with pytest.raises(ValueError, match="outcome"):
        SettlementEvent(
            sequence_number=1,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-X",
            outcome=None,
        )


def test_settlement_event_rejects_outcome_on_reversal() -> None:
    """A `SETTLEMENT_REVERSED` event carrying a non-`None` `outcome` raises
    `ValueError` naming the `outcome` field -- a reversal clears the
    outcome, it does not carry a new one (that requires a follow-up
    `SETTLEMENT`).
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        SettlementEvent,
        SettlementEventType,
    )

    with pytest.raises(ValueError, match="outcome"):
        SettlementEvent(
            sequence_number=1,
            event_type=SettlementEventType.SETTLEMENT_REVERSED,
            market_ticker="MKT-X",
            outcome=ResolutionOutcome.YES,
        )


def test_settlement_event_rejects_bool_as_sequence_number() -> None:
    """A `bool` `sequence_number` (an `int` subclass) raises `TypeError`
    naming the `sequence_number` field, per the repo-wide "no bool-as-int"
    rule (see `windbreak.numeric.types._IntUnit`).
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        SettlementEvent,
        SettlementEventType,
    )

    with pytest.raises(TypeError, match="sequence_number"):
        SettlementEvent(
            sequence_number=True,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-X",
            outcome=ResolutionOutcome.YES,
        )


# ---------------------------------------------------------------------------
# 5. MarketResolution construction invariants, both directions.
# ---------------------------------------------------------------------------


def test_market_resolution_rejects_resolved_status_with_no_outcome() -> None:
    """`status=RESOLVED` with `outcome=None` raises `ValueError` naming
    `outcome` -- a resolved market must carry its settled answer.
    """
    from windbreak.evaluation.resolution import MarketResolution, ResolutionStatus

    with pytest.raises(ValueError, match="outcome"):
        MarketResolution(
            market_ticker="MKT-X",
            status=ResolutionStatus.RESOLVED,
            outcome=None,
            reversal_count=0,
        )


def test_market_resolution_rejects_non_resolved_status_with_an_outcome() -> None:
    """`status=UNRESOLVED` (or `REVERSED`) with a non-`None` `outcome`
    raises `ValueError` naming `outcome` -- only a `RESOLVED` market may
    carry a settled answer.
    """
    from windbreak.evaluation.resolution import (
        MarketResolution,
        ResolutionOutcome,
        ResolutionStatus,
    )

    with pytest.raises(ValueError, match="outcome"):
        MarketResolution(
            market_ticker="MKT-X",
            status=ResolutionStatus.UNRESOLVED,
            outcome=ResolutionOutcome.YES,
            reversal_count=0,
        )

    with pytest.raises(ValueError, match="outcome"):
        MarketResolution(
            market_ticker="MKT-X",
            status=ResolutionStatus.REVERSED,
            outcome=ResolutionOutcome.NO,
            reversal_count=1,
        )


# ---------------------------------------------------------------------------
# 6. ResolutionTracker.from_ledger: illegal transitions and stream ordering.
# ---------------------------------------------------------------------------


def test_from_ledger_rejects_duplicate_sequence_number() -> None:
    """Two events sharing a `sequence_number` raise `ValueError` naming
    `sequence_number` -- the global stream order must be a strict total
    order with no ties.
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        ResolutionTracker,
        SettlementEvent,
        SettlementEventType,
    )

    events = (
        SettlementEvent(
            sequence_number=1,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-A",
            outcome=ResolutionOutcome.YES,
        ),
        SettlementEvent(
            sequence_number=1,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-B",
            outcome=ResolutionOutcome.NO,
        ),
    )

    with pytest.raises(ValueError, match="sequence_number"):
        ResolutionTracker.from_ledger(events)


def test_from_ledger_rejects_decreasing_sequence_number() -> None:
    """A `sequence_number` lower than the previous event's raises
    `ValueError` naming `sequence_number`.
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        ResolutionTracker,
        SettlementEvent,
        SettlementEventType,
    )

    events = (
        SettlementEvent(
            sequence_number=2,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-A",
            outcome=ResolutionOutcome.YES,
        ),
        SettlementEvent(
            sequence_number=1,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-B",
            outcome=ResolutionOutcome.NO,
        ),
    )

    with pytest.raises(ValueError, match="sequence_number"):
        ResolutionTracker.from_ledger(events)


def test_from_ledger_rejects_settlement_on_an_already_resolved_market() -> None:
    """A second `SETTLEMENT` on a market that is already `RESOLVED` (with no
    intervening reversal) raises `ValueError` naming the offending
    `market_ticker`.
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        ResolutionTracker,
        SettlementEvent,
        SettlementEventType,
    )

    events = (
        SettlementEvent(
            sequence_number=1,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-A",
            outcome=ResolutionOutcome.YES,
        ),
        SettlementEvent(
            sequence_number=2,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-A",
            outcome=ResolutionOutcome.NO,
        ),
    )

    with pytest.raises(ValueError, match="MKT-A"):
        ResolutionTracker.from_ledger(events)


def test_from_ledger_rejects_reversal_of_an_unresolved_market() -> None:
    """A `SETTLEMENT_REVERSED` as the first event ever seen for a market
    (still `UNRESOLVED`) raises `ValueError` naming the offending
    `market_ticker` -- there is nothing to reverse.
    """
    from windbreak.evaluation.resolution import (
        ResolutionTracker,
        SettlementEvent,
        SettlementEventType,
    )

    events = (
        SettlementEvent(
            sequence_number=1,
            event_type=SettlementEventType.SETTLEMENT_REVERSED,
            market_ticker="MKT-A",
            outcome=None,
        ),
    )

    with pytest.raises(ValueError, match="MKT-A"):
        ResolutionTracker.from_ledger(events)


def test_from_ledger_rejects_double_reversal_of_the_same_market() -> None:
    """A second `SETTLEMENT_REVERSED` on a market that is already
    `REVERSED` (with no intervening resettlement) raises `ValueError`
    naming the offending `market_ticker`.
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        ResolutionTracker,
        SettlementEvent,
        SettlementEventType,
    )

    events = (
        SettlementEvent(
            sequence_number=1,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-A",
            outcome=ResolutionOutcome.YES,
        ),
        SettlementEvent(
            sequence_number=2,
            event_type=SettlementEventType.SETTLEMENT_REVERSED,
            market_ticker="MKT-A",
            outcome=None,
        ),
        SettlementEvent(
            sequence_number=3,
            event_type=SettlementEventType.SETTLEMENT_REVERSED,
            market_ticker="MKT-A",
            outcome=None,
        ),
    )

    with pytest.raises(ValueError, match="MKT-A"):
        ResolutionTracker.from_ledger(events)


# ---------------------------------------------------------------------------
# 7. Regression: issue #49's resolutions_from_fixture is untouched by the
#    new settlement_events / quote_snapshots / base_rates blocks.
# ---------------------------------------------------------------------------


def test_resolutions_from_fixture_still_works_alongside_the_new_blocks() -> None:
    """`resolutions_from_fixture` (issue #49) still parses the fixture's
    static `resolutions` block correctly now that `settlement_events`,
    `quote_snapshots`, and `base_rates` sit alongside it -- the new,
    additive blocks do not interfere with the existing loader.
    """
    from windbreak.evaluation.resolution import (
        ResolutionOutcome,
        resolutions_from_fixture,
    )

    fixture = _load_fixture()

    resolutions = resolutions_from_fixture(fixture)

    assert resolutions["MKT-01"] is ResolutionOutcome.YES
    assert resolutions["MKT-10"] is ResolutionOutcome.NO
    assert len(resolutions) == 10
    # The new disjoint settlement-event tickers must never leak into the
    # static resolutions mapping.
    assert _TICKER_T1 not in resolutions
    assert _TICKER_T2 not in resolutions
    assert _TICKER_T3 not in resolutions
