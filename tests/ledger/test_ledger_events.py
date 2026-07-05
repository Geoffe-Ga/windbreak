"""Tests for `hedgekit.ledger.events` (issue #13).

Pins the event/envelope contract that `hedgekit.ledger.store` hashes and
persists:

- `canonical_json` is a deterministic, whitespace-free serialization of a
  dict, independent of key insertion order.
- `utc_now_iso` returns a UTC ISO-8601 timestamp with microsecond
  precision.
- `Event` is a frozen base dataclass with `event_type`, `component`,
  `payload_schema_version`, and `payload` fields, plus an `envelope_json`
  property wrapping them as `{"component", "data", "schema_version"}`.
- The three M0 event subtypes (`ConfigLoaded`, `ModeHeartbeat`,
  `AlertEmitted`) are frozen dataclasses whose ergonomic constructors
  (e.g. `ConfigLoaded(component=..., config_hash=..., diff=...)`)
  auto-populate `event_type` (equal to the class name) and
  `payload_schema_version`, and whose `payload` property assembles the
  typed fields into the persisted payload dict.
- `EVENT_TYPES` maps each `event_type` string to its class, so a
  persisted envelope can be reconstructed as
  `EVENT_TYPES[event_type](component=..., **data)`.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import re

import pytest

from hedgekit.ledger.events import (
    EVENT_TYPES,
    GENESIS_PREV_HASH,
    AlertEmitted,
    ConfigLoaded,
    Event,
    ModeHeartbeat,
    canonical_json,
    utc_now_iso,
)

_ISO_UTC_MICROSECOND_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$"
)

#: The three key/value pairs canonical_json tests permute the order of.
_SAMPLE_ITEMS = [("b", 1), ("a", 2), ("c", 3)]


def test_genesis_prev_hash_is_sixty_four_zero_characters() -> None:
    """GENESIS_PREV_HASH is the documented all-zero SHA-256-width sentinel."""
    assert GENESIS_PREV_HASH == "0" * 64
    assert len(GENESIS_PREV_HASH) == 64


@pytest.mark.parametrize("ordered_items", list(itertools.permutations(_SAMPLE_ITEMS)))
def test_canonical_json_is_independent_of_dict_insertion_order(
    ordered_items: tuple[tuple[str, int], ...],
) -> None:
    """Every insertion order of the same key/value pairs serializes identically."""
    obj = dict(ordered_items)

    assert canonical_json(obj) == '{"a":2,"b":1,"c":3}'


def test_canonical_json_contains_no_whitespace() -> None:
    """Nested structures still serialize with zero whitespace characters."""
    result = canonical_json({"nested": {"z": 1, "a": 2}, "list": [3, 1, 2]})

    assert " " not in result
    assert "\n" not in result
    assert "\t" not in result


def test_canonical_json_matches_sorted_compact_json_dumps() -> None:
    """canonical_json agrees with the equivalent explicit json.dumps call."""
    obj = {"z": 1, "a": {"y": 2, "x": 1}}
    expected = json.dumps(obj, sort_keys=True, separators=(",", ":"))

    assert canonical_json(obj) == expected


def test_utc_now_iso_returns_utc_iso8601_with_microseconds() -> None:
    """utc_now_iso() returns a UTC-offset ISO-8601 string with microseconds."""
    timestamp = utc_now_iso()

    assert _ISO_UTC_MICROSECOND_RE.match(timestamp) is not None, timestamp
    assert timestamp.endswith("+00:00")


def test_event_base_class_exposes_all_four_fields() -> None:
    """The base Event dataclass carries event_type/component/schema/payload."""
    event = Event(
        event_type="ConfigLoaded",
        component="pipeline",
        payload_schema_version=1,
        payload={"config_hash": "abc", "diff": {}},
    )

    assert event.event_type == "ConfigLoaded"
    assert event.component == "pipeline"
    assert event.payload_schema_version == 1
    assert event.payload == {"config_hash": "abc", "diff": {}}


def test_event_envelope_json_has_component_data_schema_version_sorted_keys() -> None:
    """envelope_json wraps the four fields into the pinned envelope shape."""
    event = Event(
        event_type="ConfigLoaded",
        component="pipeline",
        payload_schema_version=1,
        payload={"config_hash": "abc", "diff": {}},
    )

    envelope = json.loads(event.envelope_json)

    assert envelope == {
        "component": "pipeline",
        "data": {"config_hash": "abc", "diff": {}},
        "schema_version": 1,
    }
    assert event.envelope_json == canonical_json(
        {
            "component": "pipeline",
            "data": {"config_hash": "abc", "diff": {}},
            "schema_version": 1,
        }
    )


def test_event_is_frozen() -> None:
    """Event instances cannot be mutated after construction."""
    event = Event(
        event_type="ConfigLoaded",
        component="pipeline",
        payload_schema_version=1,
        payload={},
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.component = "changed"  # type: ignore[misc]


def test_config_loaded_populates_event_type_schema_version_and_payload() -> None:
    """ConfigLoaded's ergonomic constructor derives the full Event contract."""
    event = ConfigLoaded(component="pipeline", config_hash="deadbeef", diff={"x": 1})

    assert event.event_type == "ConfigLoaded"
    assert event.component == "pipeline"
    assert event.payload_schema_version == 1
    assert event.payload == {"config_hash": "deadbeef", "diff": {"x": 1}}


def test_mode_heartbeat_populates_event_type_schema_version_and_payload() -> None:
    """ModeHeartbeat's ergonomic constructor derives the full Event contract."""
    event = ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=7)

    assert event.event_type == "ModeHeartbeat"
    assert event.component == "pipeline"
    assert event.payload_schema_version == 1
    assert event.payload == {"mode": "RESEARCH", "beat": 7}


def test_alert_emitted_populates_event_type_schema_version_and_payload() -> None:
    """AlertEmitted's ergonomic constructor derives the full Event contract."""
    event = AlertEmitted(component="alerts", severity="high", message="disk full")

    assert event.event_type == "AlertEmitted"
    assert event.component == "alerts"
    assert event.payload_schema_version == 1
    assert event.payload == {"severity": "high", "message": "disk full"}


def test_config_loaded_envelope_json_matches_canonical_envelope() -> None:
    """envelope_json is the canonical envelope.

    The persisted object is exactly the {"component", "data",
    "schema_version"} shape.
    """
    event = ConfigLoaded(component="pipeline", config_hash="deadbeef", diff={"x": 1})

    envelope = json.loads(event.envelope_json)

    assert envelope == {
        "component": "pipeline",
        "data": {"config_hash": "deadbeef", "diff": {"x": 1}},
        "schema_version": 1,
    }


def test_config_loaded_is_frozen() -> None:
    """ConfigLoaded, like the base Event, is immutable after construction."""
    event = ConfigLoaded(component="pipeline", config_hash="abc", diff={})

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.config_hash = "changed"  # type: ignore[misc]


def test_event_types_registry_maps_type_name_to_class() -> None:
    """EVENT_TYPES lets a persisted event_type string recover its class."""
    assert {
        "ConfigLoaded": ConfigLoaded,
        "ModeHeartbeat": ModeHeartbeat,
        "AlertEmitted": AlertEmitted,
    } == EVENT_TYPES


def test_event_types_registry_round_trips_from_payload_data() -> None:
    """A registry lookup plus the persisted `data` dict reconstructs the event."""
    original = ConfigLoaded(component="pipeline", config_hash="deadbeef", diff={"x": 1})
    envelope = json.loads(original.envelope_json)

    rebuilt_cls = EVENT_TYPES[original.event_type]
    rebuilt = rebuilt_cls(component=envelope["component"], **envelope["data"])

    assert rebuilt == original
