"""Tests for windbreak.connector.validation (issue #20): fail-closed schema drift.

`SchemaValidator.validate(segments, payload)` implements SPEC §3 principle 3
("fail closed on drift") for every raw payload a connector fetches:

* An exact-match payload passes silently.
* An extra field the schema names `cosmetic` only warns.
* An extra field the schema does *not* recognize -- at the top level, nested
  inside a mapping-valued field, or inside a mapping list-item -- ledgers one
  `SCHEMA_ANOMALY` event (carrying the schema key, version, offending
  field name(s), and a payload hash) and *then* raises `SchemaAnomalyHaltError`.
* A path with no registered schema at all (`SchemaRegistry.schema_for`
  returns None) raises `SchemaAnomalyHaltError` too: a new endpoint must ship with
  a schema, not silently pass through unchecked.
* A raising ledger writer is isolated (logged and swallowed); the
  `SchemaAnomalyHaltError` still raises regardless.

`windbreak.connector.validation` does not exist yet, so importing it fails
collection with `ModuleNotFoundError` -- the expected Gate 1 RED state for
issue #20.

Pinned contract details not fully specified by the architect's design (see
this test module's docstrings at point of use for the reasoning):

* The `SCHEMA_ANOMALY` event payload uses the keys `"schema_key"`,
  `"version"`, `"fields"`, and `"raw_exchange_payload_hash"` -- the last
  name matches the existing `PRODUCT_REFUSED` / `MARKET_MALFORMED` event
  convention in `windbreak.connector.kalshi.normalize` for consistency.
* The payload hash is over the *full* top-level payload passed to
  `.validate()` (via the same canonical-JSON + SHA-256 scheme as
  `windbreak.connector.kalshi.normalize.payload_hash`), not just the
  sub-mapping where the anomaly was found.
* An unregistered path does *not* ledger a `SCHEMA_ANOMALY` event (the
  design text says "ledger ... THEN raise" only for the recognized-schema
  case; the unregistered-path bullet says only "raise").
* The ledgered event's `ts` uses the same ISO-8601-UTC-with-trailing-`Z`
  rendering (`%Y-%m-%dT%H:%M:%S.%f` + `"Z"`) already used throughout
  `windbreak.connector` (`snapshot.utc_now_iso` / `adapter._iso_timestamp`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from windbreak.connector.snapshot import ConnectorEvent, InMemoryEventLedgerWriter
from windbreak.connector.validation import (
    SCHEMA_ANOMALY_EVENT,
    ResponseSchema,
    SchemaAnomalyHaltError,
    SchemaRegistry,
    SchemaValidator,
    kalshi_default_schema_registry,
)
from windbreak.ledger import canonical_json

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The fixed clock every `SchemaValidator` in this module is built against.
_FIXED_WALL_CLOCK_ISO = "2026-07-04T12:00:00.000000Z"


def _wall_clock() -> datetime:
    """Return the fixed wall-clock datetime this module's tests pin against."""
    return datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


def _expected_payload_hash(payload: Mapping[str, object]) -> str:
    """Compute the same canonical-JSON SHA-256 fingerprint `validate()` must use.

    Args:
        payload: The full top-level payload passed to `.validate()`.

    Returns:
        The 64-character lowercase-hex SHA-256 digest of its canonical JSON.
    """
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


class _RaisingLedgerWriter:
    """An `EventLedgerWriter` that always raises, simulating a broken ledger."""

    def record(self, event: ConnectorEvent) -> None:
        """Raise unconditionally.

        Args:
            event: The event that would have been recorded.
        """
        raise RuntimeError("ledger unavailable")


def _widget_registry() -> SchemaRegistry:
    """Build a small, fictional-endpoint registry for generic semantics tests.

    A "widget" endpoint, independent of any real Kalshi shape, so
    `SchemaValidator`'s exact/cosmetic/nested/list-item/unregistered
    semantics can be pinned without coupling to the Kalshi fixture shapes
    (those are covered separately by `kalshi_default_schema_registry`).

    Returns:
        A registry with one `("widget",)` schema and no other pattern
        registered (so `("nonexistent",)` is deliberately unregistered).
    """
    book_schema = ResponseSchema(
        key="widget.book",
        version=1,
        recognized={"yes": None, "no": None},
        cosmetic=frozenset(),
    )
    item_schema = ResponseSchema(
        key="widget.item",
        version=1,
        recognized={"id": None},
        cosmetic=frozenset({"note"}),
    )
    widget_schema = ResponseSchema(
        key="widget",
        version=1,
        recognized={"book": book_schema, "items": item_schema, "cursor": None},
        cosmetic=frozenset({"display_label"}),
    )
    return SchemaRegistry({("widget",): widget_schema})


def _clean_widget_payload() -> dict[str, Any]:
    """Return a widget payload with no cosmetic or unrecognized fields at all."""
    return {
        "cursor": "",
        "book": {"yes": [1], "no": []},
        "items": [{"id": 1}],
    }


# --- exact match / cosmetic ------------------------------------------------


def test_exact_match_payload_passes_with_no_event_and_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A payload with only recognized fields passes silently, top to bottom."""
    caplog.set_level(logging.WARNING)
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(_widget_registry(), ledger, wall_clock=_wall_clock)

    validator.validate(("widget",), _clean_widget_payload())

    assert ledger.events_by_type(SCHEMA_ANOMALY_EVENT) == ()
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_cosmetic_extra_top_level_field_only_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cosmetic-allowlisted extra field warns but never raises or ledgers."""
    caplog.set_level(logging.WARNING)
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(_widget_registry(), ledger, wall_clock=_wall_clock)
    payload = {**_clean_widget_payload(), "display_label": "a widget"}

    validator.validate(("widget",), payload)

    assert ledger.events_by_type(SCHEMA_ANOMALY_EVENT) == ()
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "display_label" in messages


def test_cosmetic_extra_field_inside_a_list_item_only_warns() -> None:
    """A list-item's own cosmetic allowlist (`"note"`) is honored, not just the top."""
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(_widget_registry(), ledger, wall_clock=_wall_clock)
    payload = {**_clean_widget_payload(), "items": [{"id": 1, "note": "extra info"}]}

    validator.validate(("widget",), payload)

    assert ledger.events_by_type(SCHEMA_ANOMALY_EVENT) == ()


# --- unrecognized field: top level / nested mapping / list item -----------


def test_unrecognized_top_level_field_halts_and_ledgers_before_raising() -> None:
    """An unrecognized top-level field ledgers one event, then raises."""
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(_widget_registry(), ledger, wall_clock=_wall_clock)
    payload = {**_clean_widget_payload(), "fee": 5}

    with pytest.raises(SchemaAnomalyHaltError):
        validator.validate(("widget",), payload)

    (event,) = ledger.events_by_type(SCHEMA_ANOMALY_EVENT)
    assert event.payload["schema_key"] == "widget"
    assert event.payload["version"] == 1
    assert tuple(event.payload["fields"]) == ("fee",)
    assert event.payload["raw_exchange_payload_hash"] == _expected_payload_hash(payload)
    assert event.ts == _FIXED_WALL_CLOCK_ISO


def test_unrecognized_field_halt_carries_every_offending_field_name() -> None:
    """Two unrecognized top-level fields both appear in the halted event."""
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(_widget_registry(), ledger, wall_clock=_wall_clock)
    payload = {**_clean_widget_payload(), "fee": 5, "risk_flag": True}

    with pytest.raises(SchemaAnomalyHaltError) as exc_info:
        validator.validate(("widget",), payload)

    (event,) = ledger.events_by_type(SCHEMA_ANOMALY_EVENT)
    assert set(event.payload["fields"]) == {"fee", "risk_flag"}
    assert set(exc_info.value.fields) == {"fee", "risk_flag"}
    assert exc_info.value.schema_key == "widget"
    assert exc_info.value.version == 1


def test_unrecognized_field_nested_inside_a_mapping_field_halts() -> None:
    """An unexpected key nested inside a mapping-valued field is caught too."""
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(_widget_registry(), ledger, wall_clock=_wall_clock)
    payload = {
        **_clean_widget_payload(),
        "book": {"yes": [1], "no": [], "fee": 5},
    }

    with pytest.raises(SchemaAnomalyHaltError):
        validator.validate(("widget",), payload)

    (event,) = ledger.events_by_type(SCHEMA_ANOMALY_EVENT)
    assert event.payload["schema_key"] == "widget.book"
    assert event.payload["version"] == 1
    assert tuple(event.payload["fields"]) == ("fee",)


def test_unrecognized_field_inside_a_mapping_list_item_halts() -> None:
    """An unexpected key inside one item of a mapping-valued list is caught."""
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(_widget_registry(), ledger, wall_clock=_wall_clock)
    payload = {
        **_clean_widget_payload(),
        "items": [{"id": 1}, {"id": 2, "extra_field": "unexpected"}],
    }

    with pytest.raises(SchemaAnomalyHaltError):
        validator.validate(("widget",), payload)

    (event,) = ledger.events_by_type(SCHEMA_ANOMALY_EVENT)
    assert event.payload["schema_key"] == "widget.item"
    assert event.payload["version"] == 1
    assert tuple(event.payload["fields"]) == ("extra_field",)


# --- unregistered path ------------------------------------------------------


def test_unregistered_path_raises_without_ledgering_an_event() -> None:
    """A path with no registered schema fails closed without a ledger event.

    A brand-new endpoint must ship with a schema; `schema_for` returning None
    is refused rather than silently passed through.
    """
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(_widget_registry(), ledger, wall_clock=_wall_clock)

    with pytest.raises(SchemaAnomalyHaltError):
        validator.validate(("nonexistent",), {"anything": "at all"})

    assert ledger.events_by_type(SCHEMA_ANOMALY_EVENT) == ()


# --- isolated (raising) ledger writer ---------------------------------------


def test_raising_ledger_writer_is_isolated_but_the_halt_still_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken ledger writer never swallows the `SchemaAnomalyHaltError` itself."""
    caplog.set_level(logging.DEBUG)
    validator = SchemaValidator(
        _widget_registry(), _RaisingLedgerWriter(), wall_clock=_wall_clock
    )
    payload = {**_clean_widget_payload(), "fee": 5}

    with pytest.raises(SchemaAnomalyHaltError):
        validator.validate(("widget",), payload)

    assert any("ledger" in record.getMessage().lower() for record in caplog.records)


# --- ResponseSchema / SchemaRegistry ----------------------------------------


def test_response_schema_is_frozen() -> None:
    """`ResponseSchema` is a frozen dataclass; mutation raises."""
    schema = ResponseSchema(key="x", version=1, recognized={}, cosmetic=frozenset())

    with pytest.raises(FrozenInstanceError):
        schema.version = 2  # type: ignore[misc]


def test_schema_registry_matches_wildcard_and_exact_segment_patterns() -> None:
    """`schema_for` matches `"*"` wildcards positionally, by segment count."""
    orderbook = ResponseSchema(key="ob", version=1, recognized={}, cosmetic=frozenset())
    exchange_status = ResponseSchema(
        key="es", version=1, recognized={}, cosmetic=frozenset()
    )
    markets = ResponseSchema(key="mk", version=1, recognized={}, cosmetic=frozenset())
    events = ResponseSchema(key="ev", version=1, recognized={}, cosmetic=frozenset())
    series = ResponseSchema(key="sr", version=1, recognized={}, cosmetic=frozenset())
    registry = SchemaRegistry(
        {
            ("markets", "*", "orderbook"): orderbook,
            ("exchange", "status"): exchange_status,
            ("markets",): markets,
            ("events",): events,
            ("series", "*"): series,
        }
    )

    assert registry.schema_for(("markets", "KXFED-24DEC", "orderbook")) is orderbook
    assert registry.schema_for(("exchange", "status")) is exchange_status
    assert registry.schema_for(("markets",)) is markets
    assert registry.schema_for(("events",)) is events
    assert registry.schema_for(("series", "KXFED")) is series


def test_schema_registry_returns_none_for_an_unregistered_segment_shape() -> None:
    """A segment tuple matching no pattern (by length or literal) yields None."""
    markets = ResponseSchema(key="mk", version=1, recognized={}, cosmetic=frozenset())
    registry = SchemaRegistry({("markets",): markets})

    assert registry.schema_for(("markets", "KXFED-24DEC")) is None
    assert registry.schema_for(("unknown", "path")) is None


# --- kalshi_default_schema_registry: all five current endpoints ------------

#: `tests/connector/test_validation.py` -> `tests/` -> `tests/fixtures/...`.
_KALSHI_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "exchange" / "kalshi"
)


def _read_kalshi_fixture(name: str) -> Any:
    """Parse one recorded Kalshi JSON fixture by filename.

    Args:
        name: The fixture file's name, e.g. `"markets.json"`.

    Returns:
        The parsed JSON.
    """
    return json.loads((_KALSHI_FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("segments", "fixture_name"),
    [
        (("markets",), "markets.json"),
        (("events",), "events.json"),
        (("markets", "KXFED-24DEC", "orderbook"), "orderbook_KXFED-24DEC.json"),
        (("exchange", "status"), "exchange_status.json"),
        (("series", "KXFED"), "series_KXFED.json"),
    ],
)
def test_kalshi_default_registry_validates_every_recorded_fixture_clean(
    segments: tuple[str, ...], fixture_name: str
) -> None:
    """Every one of the five current endpoints' recorded fixtures validates clean."""
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(
        kalshi_default_schema_registry(), ledger, wall_clock=_wall_clock
    )
    payload = _read_kalshi_fixture(fixture_name)

    validator.validate(segments, payload)

    assert ledger.events_by_type(SCHEMA_ANOMALY_EVENT) == ()


@pytest.mark.parametrize(
    "segments",
    [
        ("markets",),
        ("events",),
        ("markets", "KXFED-24DEC", "orderbook"),
        ("exchange", "status"),
        ("series", "KXFED"),
    ],
)
def test_kalshi_default_registry_registers_all_five_endpoints(
    segments: tuple[str, ...],
) -> None:
    """`kalshi_default_schema_registry` covers all five current endpoints."""
    registry = kalshi_default_schema_registry()

    assert registry.schema_for(segments) is not None


def test_kalshi_default_registry_orderbook_money_drift_fixture_halts() -> None:
    """`orderbook_drift_money_fee.json`'s unexpected `fee` field halts."""
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(
        kalshi_default_schema_registry(), ledger, wall_clock=_wall_clock
    )
    payload = _read_kalshi_fixture("faults/orderbook_drift_money_fee.json")

    with pytest.raises(SchemaAnomalyHaltError):
        validator.validate(("markets", "KXFED-24DEC", "orderbook"), payload)

    (event,) = ledger.events_by_type(SCHEMA_ANOMALY_EVENT)
    assert "fee" in event.payload["fields"]


def test_kalshi_default_registry_orderbook_cosmetic_drift_fixture_only_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`orderbook_drift_cosmetic.json`'s extra field only warns."""
    caplog.set_level(logging.WARNING)
    ledger = InMemoryEventLedgerWriter()
    validator = SchemaValidator(
        kalshi_default_schema_registry(), ledger, wall_clock=_wall_clock
    )
    payload = _read_kalshi_fixture("faults/orderbook_drift_cosmetic.json")

    validator.validate(("markets", "KXFED-24DEC", "orderbook"), payload)

    assert ledger.events_by_type(SCHEMA_ANOMALY_EVENT) == ()
    assert any(record.levelno >= logging.WARNING for record in caplog.records)
