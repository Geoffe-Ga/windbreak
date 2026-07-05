"""Fail-closed schema-drift detection for raw exchange payloads (issue #20).

Implements SPEC S3 principle 3 ("fail closed on drift") for every raw payload a
connector fetches. A :class:`SchemaValidator` walks a payload against a
:class:`ResponseSchema` resolved for its endpoint path and classifies each
field:

* Only recognized fields (top to bottom) -> passes silently.
* An extra field the schema explicitly names ``cosmetic`` -> warns, never halts.
* An unrecognized extra field -- at the top level, nested inside a
  mapping-valued field, or inside one item of a mapping-valued list -> ledgers
  one :data:`SCHEMA_ANOMALY_EVENT` (carrying the schema key, version, offending
  field name(s), and a payload hash) and then raises :class:`SchemaAnomalyHaltError`.
* A path with no registered schema at all -> raises :class:`SchemaAnomalyHaltError`
  *without* a ledger event: a new endpoint must ship with a schema rather than
  silently pass through unchecked.

A broken ledger writer is isolated (logged and swallowed); the
:class:`SchemaAnomalyHaltError` still raises regardless. The ledgered event's payload
hash uses the same canonical-JSON SHA-256 scheme as
:func:`hedgekit.connector.kalshi.normalize.payload_hash` (kept in lock-step here
rather than imported, to keep this generic layer free of any exchange-specific
module), taken over the *full* top-level payload; its ``ts`` renders the
injected wall clock through :func:`hedgekit.connector.snapshot.utc_now_iso`.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from hedgekit.connector.snapshot import ConnectorEvent, utc_now_iso
from hedgekit.ledger import canonical_json

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from hedgekit.connector.snapshot import EventLedgerWriter

#: Ledger event type recorded when an unrecognized field is detected on a
#: recognized endpoint's payload (SPEC S3 principle 3, fail closed on drift).
SCHEMA_ANOMALY_EVENT: Final = "SCHEMA_ANOMALY"

#: The wildcard path segment matching any single positional segment value.
_WILDCARD: Final = "*"

#: Version stamped on the halt raised for an entirely unregistered path, where
#: no schema (and thus no real version) exists to attribute the drift to.
_UNREGISTERED_VERSION: Final = 0

_LOGGER = logging.getLogger("hedgekit.connector.validation")


def _payload_fingerprint(payload: Mapping[str, object]) -> str:
    """Return the canonical-JSON SHA-256 fingerprint of a full payload.

    Mirrors :func:`hedgekit.connector.kalshi.normalize.payload_hash`: the digest
    is taken over the key-sorted, whitespace-free JSON encoding, so it depends
    only on the payload's contents, giving a reproducible provenance handle.

    Args:
        payload: The full top-level payload to fingerprint.

    Returns:
        The 64-character lowercase-hex SHA-256 digest.
    """
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


class SchemaAnomalyHaltError(RuntimeError):
    """Raised to halt processing when a payload carries unrecognized fields.

    Attributes:
        schema_key: The key of the schema the drift was detected against (or a
            path rendering when no schema was registered at all).
        version: The offended schema's version.
        fields: The offending, unrecognized field name(s).
    """

    def __init__(self, schema_key: str, version: int, fields: Sequence[str]) -> None:
        """Initialize with the offended schema and the offending fields.

        Args:
            schema_key: The key of the schema the drift was detected against.
            version: The offended schema's version.
            fields: The offending, unrecognized field name(s).
        """
        self.schema_key = schema_key
        self.version = version
        self.fields: tuple[str, ...] = tuple(fields)
        super().__init__(
            f"schema {schema_key!r} v{version} halted on unrecognized "
            f"field(s): {', '.join(self.fields) or '(unregistered path)'}"
        )


@dataclass(frozen=True, slots=True)
class ResponseSchema:
    """A recognized shape for one exchange payload (or sub-payload).

    Attributes:
        key: A stable identifier for this schema, ledgered on drift.
        version: The schema version, ledgered on drift.
        recognized: Maps each recognized field name to either None (an
            any-shaped leaf) or a child :class:`ResponseSchema` to recurse into
            -- applied to a mapping-valued field directly and to every
            mapping item of a list-valued field.
        cosmetic: Field names that may appear extra without halting; their
            presence only warns.
    """

    key: str
    version: int
    recognized: Mapping[str, ResponseSchema | None]
    cosmetic: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Anomaly:
    """One detected drift: the offended schema and its offending fields.

    Attributes:
        schema_key: The offended schema's key.
        version: The offended schema's version.
        fields: The offending, unrecognized field names.
    """

    schema_key: str
    version: int
    fields: tuple[str, ...]


class SchemaRegistry:
    """Resolves an endpoint's segment path to its :class:`ResponseSchema`."""

    def __init__(self, schemas: Mapping[tuple[str, ...], ResponseSchema]) -> None:
        """Initialize with the registered ``(segments -> schema)`` patterns.

        Args:
            schemas: Maps each segment pattern (with ``"*"`` matching any single
                positional segment) to the schema serving that endpoint.
        """
        self._schemas = dict(schemas)

    def schema_for(self, segments: tuple[str, ...]) -> ResponseSchema | None:
        """Return the schema whose pattern matches ``segments``, or None.

        Args:
            segments: The endpoint path segments to resolve.

        Returns:
            The matching schema, or None when no registered pattern matches by
            length and per-position literal (``"*"`` matching any value).
        """
        for pattern, schema in self._schemas.items():
            if _pattern_matches(pattern, segments):
                return schema
        return None


def _pattern_matches(pattern: tuple[str, ...], segments: tuple[str, ...]) -> bool:
    """Return whether ``segments`` matches ``pattern`` positionally.

    Args:
        pattern: The registered segment pattern; ``"*"`` matches any value.
        segments: The concrete endpoint path segments.

    Returns:
        True when the two have equal length and every pattern segment is either
        ``"*"`` or literally equal to its positional counterpart.
    """
    if len(pattern) != len(segments):
        return False
    return all(
        expected in (_WILDCARD, actual)
        for expected, actual in zip(pattern, segments, strict=True)
    )


class SchemaValidator:
    """Validates raw payloads against a registry, failing closed on drift."""

    def __init__(
        self,
        registry: SchemaRegistry,
        ledger_writer: EventLedgerWriter,
        *,
        wall_clock: Callable[[], datetime],
    ) -> None:
        """Initialize the validator.

        Args:
            registry: Resolves an endpoint's segments to its schema.
            ledger_writer: The seam that records :data:`SCHEMA_ANOMALY_EVENT`s.
            wall_clock: Returns "now" as a datetime, stamped on ledgered events;
                injected so event timestamps are deterministic in tests.
        """
        self._registry = registry
        self._ledger_writer = ledger_writer
        self._wall_clock = wall_clock

    def validate(
        self, segments: tuple[str, ...], payload: Mapping[str, object]
    ) -> None:
        """Validate ``payload`` for ``segments``, failing closed on drift.

        Args:
            segments: The endpoint path the payload was fetched from.
            payload: The full top-level raw payload to validate.

        Raises:
            SchemaAnomalyHaltError: If the path is unregistered, or the payload
                carries an unrecognized field at any recognized depth. In the
                latter case a :data:`SCHEMA_ANOMALY_EVENT` is ledgered first.
        """
        schema = self._registry.schema_for(segments)
        if schema is None:
            raise SchemaAnomalyHaltError("/".join(segments), _UNREGISTERED_VERSION, ())
        anomaly = self._find_anomaly(schema, payload)
        if anomaly is None:
            return
        self._ledger_anomaly(anomaly, payload)
        raise SchemaAnomalyHaltError(
            anomaly.schema_key, anomaly.version, anomaly.fields
        )

    def _find_anomaly(
        self, schema: ResponseSchema, mapping: Mapping[str, object]
    ) -> _Anomaly | None:
        """Return the first drift under ``schema`` in ``mapping``, or None.

        Emits a warning for each present cosmetic-allowlisted field as a side
        effect, then reports this level's own unrecognized fields before
        recursing into recognized children.

        Args:
            schema: The schema this mapping is validated against.
            mapping: The (sub-)payload mapping to inspect.

        Returns:
            The first detected :class:`_Anomaly`, or None when clean.
        """
        self._warn_cosmetic_fields(schema, mapping)
        offending = tuple(
            name
            for name in mapping
            if name not in schema.recognized and name not in schema.cosmetic
        )
        if offending:
            return _Anomaly(schema.key, schema.version, offending)
        return self._first_child_anomaly(schema, mapping)

    def _first_child_anomaly(
        self, schema: ResponseSchema, mapping: Mapping[str, object]
    ) -> _Anomaly | None:
        """Recurse into each recognized child field, returning the first drift.

        Args:
            schema: The schema whose recognized children are recursed into.
            mapping: The mapping whose child values are inspected.

        Returns:
            The first child :class:`_Anomaly`, or None when every child is clean.
        """
        for name, child in schema.recognized.items():
            if child is None or name not in mapping:
                continue
            anomaly = self._recurse_into_value(child, mapping[name])
            if anomaly is not None:
                return anomaly
        return None

    def _recurse_into_value(
        self, child: ResponseSchema, value: object
    ) -> _Anomaly | None:
        """Validate a recognized field's value against its child schema.

        A mapping value recurses directly; a list value recurses into each of
        its mapping items. Both are handled uniformly by treating a lone mapping
        as a single-item list; non-mapping values (a scalar, or a non-mapping
        list item) carry no schema and yield no anomaly.

        Args:
            child: The child schema the value is validated against.
            value: The recognized field's value.

        Returns:
            The first :class:`_Anomaly` found within ``value``, or None.
        """
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, Mapping):
                anomaly = self._find_anomaly(child, item)
                if anomaly is not None:
                    return anomaly
        return None

    def _warn_cosmetic_fields(
        self, schema: ResponseSchema, mapping: Mapping[str, object]
    ) -> None:
        """Warn for each present cosmetic-allowlisted field on ``mapping``.

        Args:
            schema: The schema whose cosmetic allowlist is consulted.
            mapping: The mapping whose keys are checked against the allowlist.
        """
        for name in mapping:
            if name in schema.cosmetic:
                _LOGGER.warning(
                    "cosmetic schema drift on %s v%s: unexpected field %r "
                    "(allowlisted, ignored)",
                    schema.key,
                    schema.version,
                    name,
                    extra={"component": "connector.validation"},
                )

    def _ledger_anomaly(self, anomaly: _Anomaly, payload: Mapping[str, object]) -> None:
        """Ledger one :data:`SCHEMA_ANOMALY_EVENT`, isolating a raising writer.

        Args:
            anomaly: The detected drift to record.
            payload: The full top-level payload, hashed for provenance.
        """
        event = ConnectorEvent(
            event_type=SCHEMA_ANOMALY_EVENT,
            payload={
                "schema_key": anomaly.schema_key,
                "version": anomaly.version,
                "fields": anomaly.fields,
                "raw_exchange_payload_hash": _payload_fingerprint(payload),
            },
            ts=utc_now_iso(self._wall_clock()),
        )
        try:
            self._ledger_writer.record(event)
        except Exception as exc:
            _LOGGER.warning(
                "event ledger writer failed to record %s event: %s",
                event.event_type,
                exc,
                extra={"component": "connector.validation"},
            )


def kalshi_default_schema_registry() -> SchemaRegistry:
    """Build the default schema registry covering Kalshi's five read endpoints.

    Every field set below matches the recorded fixtures byte-for-byte and the
    fields :mod:`hedgekit.connector.kalshi.normalize` consumes. The order book's
    cosmetic allowlist admits ``display_label`` but never ``fee``: a money/risk
    field appearing on a book must halt, while a purely presentational label
    only warns.

    Returns:
        A registry serving ``/markets``, ``/events``,
        ``/markets/{ticker}/orderbook``, ``/exchange/status``, and
        ``/series/{ticker}``.
    """
    return SchemaRegistry(
        {
            ("markets",): _MARKETS_SCHEMA,
            ("events",): _EVENTS_SCHEMA,
            ("markets", _WILDCARD, "orderbook"): _ORDERBOOK_SCHEMA,
            ("exchange", "status"): _EXCHANGE_STATUS_SCHEMA,
            ("series", _WILDCARD): _SERIES_SCHEMA,
        }
    )


#: One raw ``/markets`` list entry (a normalized-binary source record).
_MARKET_ITEM_SCHEMA: Final = ResponseSchema(
    key="kalshi.market",
    version=1,
    recognized={
        "ticker": None,
        "event_ticker": None,
        "market_type": None,
        "title": None,
        "rules_primary": None,
        "category": None,
        "close_time": None,
        "expected_expiration_time": None,
        "tick_size": None,
    },
    cosmetic=frozenset(),
)

#: The ``/markets`` list endpoint (a page of market entries plus a cursor).
_MARKETS_SCHEMA: Final = ResponseSchema(
    key="kalshi.markets",
    version=1,
    recognized={"markets": _MARKET_ITEM_SCHEMA, "cursor": None},
    cosmetic=frozenset(),
)

#: One raw ``/events`` list entry (drives mutually-exclusive grouping).
_EVENT_ITEM_SCHEMA: Final = ResponseSchema(
    key="kalshi.event",
    version=1,
    recognized={
        "event_ticker": None,
        "title": None,
        "mutually_exclusive": None,
    },
    cosmetic=frozenset(),
)

#: The ``/events`` list endpoint (a page of event entries plus a cursor).
_EVENTS_SCHEMA: Final = ResponseSchema(
    key="kalshi.events",
    version=1,
    recognized={"events": _EVENT_ITEM_SCHEMA, "cursor": None},
    cosmetic=frozenset(),
)

#: The inner order book: two price/size ladders. ``fee`` is deliberately absent
#: from both recognized and cosmetic, so a money field on a book halts.
_ORDERBOOK_BOOK_SCHEMA: Final = ResponseSchema(
    key="kalshi.orderbook.book",
    version=1,
    recognized={"yes": None, "no": None},
    cosmetic=frozenset(),
)

#: The ``/markets/{ticker}/orderbook`` endpoint. A presentational
#: ``display_label`` at the top level only warns; anything else halts.
_ORDERBOOK_SCHEMA: Final = ResponseSchema(
    key="kalshi.orderbook",
    version=1,
    recognized={"orderbook": _ORDERBOOK_BOOK_SCHEMA},
    cosmetic=frozenset({"display_label"}),
)

#: The ``/exchange/status`` endpoint (the two active flags).
_EXCHANGE_STATUS_SCHEMA: Final = ResponseSchema(
    key="kalshi.exchange_status",
    version=1,
    recognized={"exchange_active": None, "trading_active": None},
    cosmetic=frozenset(),
)

#: The inner ``/series/{ticker}`` fee schedule block.
_SERIES_BLOCK_SCHEMA: Final = ResponseSchema(
    key="kalshi.series.block",
    version=1,
    recognized={
        "ticker": None,
        "fee_schedule_id": None,
        "fee_type": None,
        "maker_fee_bps": None,
        "taker_fee_bps": None,
        "settlement_fee_bps": None,
    },
    cosmetic=frozenset(),
)

#: The ``/series/{ticker}`` endpoint (a single ``series`` fee-schedule block).
_SERIES_SCHEMA: Final = ResponseSchema(
    key="kalshi.series",
    version=1,
    recognized={"series": _SERIES_BLOCK_SCHEMA},
    cosmetic=frozenset(),
)
