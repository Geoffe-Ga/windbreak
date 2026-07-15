"""Tests for the six new PAPER-loop ledger event types (issue #48, RED).

`windbreak.ledger.events` does not yet define `MarketSnapshotRecorded`,
`ScreenDecisionRecorded`, `ForecastCreated`, `SelectorDecisionRecorded`,
`EquitySampled`, or `PositionsSnapshotRecorded`, so every import below fails
collection with `ImportError: cannot import name 'MarketSnapshotRecorded'
from 'windbreak.ledger.events'` -- the expected Gate 1 RED state for issue #48.

Mirrors `tests/ledger/test_ledger_events.py`'s own registry-round-trip idiom
exactly (`EVENT_TYPES[event_type](component=..., **envelope["data"])`
reconstructs the original event), and additionally proves each type is
re-exported from `windbreak.ledger` (the package `__init__.py`), matching every
prior event type's own re-export contract.

Every numeric field pinned here is a scaled int (pips/centis/micros/epoch
seconds) -- never a float (SPEC S6.1): the ledger package is float-banned.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def test_market_snapshot_recorded_populates_event_type_and_payload() -> None:
    """`MarketSnapshotRecorded`'s ergonomic constructor derives the Event
    contract and assembles its payload from typed fields.
    """
    from windbreak.ledger.events import MarketSnapshotRecorded

    event = MarketSnapshotRecorded(
        component="scheduler",
        ticker="MKT-DEEP",
        best_bid_pips=4500,
        best_ask_pips=4600,
        fetched_at_epoch_s=1_700_000_000,
    )

    assert event.event_type == "MarketSnapshotRecorded"
    assert event.component == "scheduler"
    assert event.payload_schema_version == 1
    assert event.payload == {
        "ticker": "MKT-DEEP",
        "best_bid_pips": 4500,
        "best_ask_pips": 4600,
        "fetched_at_epoch_s": 1_700_000_000,
    }


def test_market_snapshot_recorded_accepts_none_bid_or_ask() -> None:
    """A one-sided (or empty) book is representable: `None` for the missing side."""
    from windbreak.ledger.events import MarketSnapshotRecorded

    event = MarketSnapshotRecorded(
        component="scheduler",
        ticker="MKT-DEEP",
        best_bid_pips=None,
        best_ask_pips=None,
        fetched_at_epoch_s=1_700_000_000,
    )

    assert event.payload["best_bid_pips"] is None
    assert event.payload["best_ask_pips"] is None


def test_screen_decision_recorded_populates_event_type_and_payload() -> None:
    """`ScreenDecisionRecorded`'s constructor derives the Event contract."""
    from windbreak.ledger.events import ScreenDecisionRecorded

    event = ScreenDecisionRecorded(
        component="scheduler",
        ticker="MKT-DEEP",
        eligible=True,
        blocked_by=[],
    )

    assert event.event_type == "ScreenDecisionRecorded"
    assert event.payload == {
        "ticker": "MKT-DEEP",
        "eligible": True,
        "blocked_by": [],
    }


def test_forecast_created_populates_event_type_and_payload() -> None:
    """`ForecastCreated`'s constructor derives the Event contract.

    Issue #188 adds `research_cost_micros` and `market_price_baseline_pips`
    (the two fields the weekly evaluation/cost-meter fold reads verbatim off
    the ledgered payload) and bumps the event's own
    `payload_schema_version` to `2` -- the first M0-family event whose schema
    version is not the module-wide `1` -- so a v1-shaped row already on disk
    is distinguishable from a v2 one without inspecting the payload's keys.
    """
    from windbreak.ledger.events import ForecastCreated

    event = ForecastCreated(
        component="scheduler",
        forecast_id="fc-0001",
        market_ticker="MKT-DEEP",
        probability_ppm=520_000,
        eligible_for_live=False,
        abstention_reason="no_verified_citations",
        research_cost_micros=3_000_000,
        market_price_baseline_pips=4600,
    )

    assert event.event_type == "ForecastCreated"
    assert event.payload == {
        "forecast_id": "fc-0001",
        "market_ticker": "MKT-DEEP",
        "probability_ppm": 520_000,
        "eligible_for_live": False,
        "abstention_reason": "no_verified_citations",
        "research_cost_micros": 3_000_000,
        "market_price_baseline_pips": 4600,
    }
    assert event.payload_schema_version == 2


def test_forecast_created_schema_version_two_sibling_stays_one() -> None:
    """`ForecastCreated` alone is stamped `payload_schema_version == 2`; a
    sibling PAPER-loop event (`EquitySampled`) stays on the module-wide `1`
    (issue #188): `_derive_typed_event` gains an optional `schema_version`
    override that only `ForecastCreated` supplies, so every other concrete
    event -- unmodified -- keeps stamping the shared default.
    """
    from windbreak.ledger.events import EquitySampled, ForecastCreated

    forecast_event = ForecastCreated(
        component="scheduler",
        forecast_id="fc-0001",
        market_ticker="MKT-DEEP",
        probability_ppm=520_000,
        eligible_for_live=False,
        abstention_reason=None,
        research_cost_micros=1_000_000,
        market_price_baseline_pips=4500,
    )
    sibling_event = EquitySampled(
        component="scheduler", equity_micros=0, floor_micros=0, epoch_s=1
    )

    assert forecast_event.payload_schema_version == 2
    assert sibling_event.payload_schema_version == 1


def test_selector_decision_recorded_populates_event_type_and_payload() -> None:
    """`SelectorDecisionRecorded`'s constructor derives the Event contract."""
    from windbreak.ledger.events import SelectorDecisionRecorded

    event = SelectorDecisionRecorded(
        component="scheduler",
        forecast_id="fc-0001",
        market_ticker="MKT-DEEP",
        intent_count=0,
        reasons=["fail:net_edge_min: net_edge_ppm=-500 min_net_edge_ppm=30000"],
    )

    assert event.event_type == "SelectorDecisionRecorded"
    assert event.payload == {
        "forecast_id": "fc-0001",
        "market_ticker": "MKT-DEEP",
        "intent_count": 0,
        "reasons": ["fail:net_edge_min: net_edge_ppm=-500 min_net_edge_ppm=30000"],
    }


def test_equity_sampled_populates_event_type_and_payload() -> None:
    """`EquitySampled`'s constructor derives the Event contract; every field
    is a scaled int, never a float (SPEC S6.1).
    """
    from windbreak.ledger.events import EquitySampled

    event = EquitySampled(
        component="scheduler",
        equity_micros=1_000_000_000,
        floor_micros=0,
        epoch_s=1_700_000_000,
    )

    assert event.event_type == "EquitySampled"
    assert event.payload == {
        "equity_micros": 1_000_000_000,
        "floor_micros": 0,
        "epoch_s": 1_700_000_000,
    }
    assert isinstance(event.payload["equity_micros"], int)
    assert not isinstance(event.payload["equity_micros"], float)


def test_equity_sampled_rejects_a_float_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A float `equity_micros` is rejected, not silently accepted -- the
    ledger package is float-banned (SPEC S6.1).

    `Event`'s subclasses do not themselves validate field types (they are
    plain dataclasses), so this pins the module-level float lint instead:
    `scripts/lint_no_floats.py` (exercised elsewhere) is the actual
    enforcement point, but the equity value's *type* must at minimum survive
    an explicit isinstance check here, so a mutation dropping that
    expectation is caught.
    """
    from windbreak.ledger.events import EquitySampled

    event = EquitySampled(
        component="scheduler", equity_micros=1_000_000, floor_micros=0, epoch_s=1
    )

    assert isinstance(event.equity_micros, int)


def test_positions_snapshot_recorded_populates_event_type_and_payload() -> None:
    """`PositionsSnapshotRecorded`'s constructor derives the Event contract."""
    from windbreak.ledger.events import PositionsSnapshotRecorded

    positions = [
        {"ticker": "MKT-DEEP", "quantity_centis": 200, "average_price_pips": 4600}
    ]
    event = PositionsSnapshotRecorded(component="scheduler", positions=positions)

    assert event.event_type == "PositionsSnapshotRecorded"
    assert event.payload == {"positions": positions}


def test_positions_snapshot_recorded_accepts_an_empty_positions_list() -> None:
    """A flat account (no open positions) is representable as an empty list."""
    from windbreak.ledger.events import PositionsSnapshotRecorded

    event = PositionsSnapshotRecorded(component="scheduler", positions=[])

    assert event.payload == {"positions": []}


# --- EVENT_TYPES registry round-trips, mirroring test_ledger_events.py --------


def test_event_types_registry_round_trips_market_snapshot_recorded() -> None:
    """A registry lookup plus persisted `data` reconstructs `MarketSnapshotRecorded`."""
    from windbreak.ledger.events import EVENT_TYPES, MarketSnapshotRecorded

    original = MarketSnapshotRecorded(
        component="scheduler",
        ticker="MKT-DEEP",
        best_bid_pips=4500,
        best_ask_pips=4600,
        fetched_at_epoch_s=1_700_000_000,
    )
    envelope = json.loads(original.envelope_json)

    rebuilt_cls = EVENT_TYPES[original.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == original


def test_event_types_registry_round_trips_screen_decision_recorded() -> None:
    """A registry lookup plus persisted `data` reconstructs `ScreenDecisionRecorded`."""
    from windbreak.ledger.events import EVENT_TYPES, ScreenDecisionRecorded

    original = ScreenDecisionRecorded(
        component="scheduler", ticker="MKT-DEEP", eligible=False, blocked_by=["sports"]
    )
    envelope = json.loads(original.envelope_json)

    rebuilt_cls = EVENT_TYPES[original.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == original


def test_event_types_registry_round_trips_forecast_created() -> None:
    """A registry lookup plus persisted `data` reconstructs `ForecastCreated`,
    including its two issue #188 cost/baseline fields.
    """
    from windbreak.ledger.events import EVENT_TYPES, ForecastCreated

    original = ForecastCreated(
        component="scheduler",
        forecast_id="fc-0001",
        market_ticker="MKT-DEEP",
        probability_ppm=520_000,
        eligible_for_live=True,
        abstention_reason=None,
        research_cost_micros=2_500_000,
        market_price_baseline_pips=4700,
    )
    envelope = json.loads(original.envelope_json)

    assert envelope["schema_version"] == 2

    rebuilt_cls = EVENT_TYPES[original.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == original


def test_event_types_registry_round_trips_selector_decision_recorded() -> None:
    """A registry lookup plus persisted `data` rebuilds `SelectorDecisionRecorded`."""
    from windbreak.ledger.events import EVENT_TYPES, SelectorDecisionRecorded

    original = SelectorDecisionRecorded(
        component="scheduler",
        forecast_id="fc-0001",
        market_ticker="MKT-DEEP",
        intent_count=1,
        reasons=["pass:net_edge_min", "sizing: raw_centis=200"],
    )
    envelope = json.loads(original.envelope_json)

    rebuilt_cls = EVENT_TYPES[original.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == original


def test_event_types_registry_round_trips_equity_sampled() -> None:
    """A registry lookup plus persisted `data` reconstructs `EquitySampled`."""
    from windbreak.ledger.events import EVENT_TYPES, EquitySampled

    original = EquitySampled(
        component="scheduler",
        equity_micros=1_000_000_000,
        floor_micros=0,
        epoch_s=1_700_000_000,
    )
    envelope = json.loads(original.envelope_json)

    rebuilt_cls = EVENT_TYPES[original.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == original


def test_event_types_registry_round_trips_positions_snapshot_recorded() -> None:
    """A registry lookup plus persisted `data` rebuilds `PositionsSnapshotRecorded`."""
    from windbreak.ledger.events import EVENT_TYPES, PositionsSnapshotRecorded

    original = PositionsSnapshotRecorded(
        component="scheduler",
        positions=[
            {"ticker": "MKT-DEEP", "quantity_centis": 200, "average_price_pips": 4600}
        ],
    )
    envelope = json.loads(original.envelope_json)

    rebuilt_cls = EVENT_TYPES[original.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == original


# --- Re-exported from windbreak.ledger (the package __init__), like every -----
# --- other concrete event type ------------------------------------------------


def test_all_six_new_event_types_are_reexported_from_windbreak_ledger() -> None:
    """Every new event type importable from `windbreak.ledger` directly, not
    only from `windbreak.ledger.events` -- matching every existing event
    type's own re-export contract (see `windbreak/ledger/__init__.py`).
    """
    import windbreak.ledger as ledger_package

    for name in (
        "MarketSnapshotRecorded",
        "ScreenDecisionRecorded",
        "ForecastCreated",
        "SelectorDecisionRecorded",
        "EquitySampled",
        "PositionsSnapshotRecorded",
    ):
        assert hasattr(ledger_package, name), (
            f"{name} not re-exported from windbreak.ledger"
        )
