"""Event types and canonical serialization for the hash-chained ledger.

This module defines the M0 event vocabulary that every hedgekit process
records into the append-only ledger, plus the two serialization
primitives the store hashes over:

- :func:`canonical_json` -- a deterministic, whitespace-free JSON encoding
  whose output depends only on a value's contents, never on dict insertion
  order, so identical events always hash identically.
- :func:`utc_now_iso` -- a UTC ISO-8601 timestamp with microsecond
  precision, used as each record's ``created_at``.

Each concrete event (:class:`ConfigLoaded`, :class:`ModeHeartbeat`,
:class:`AlertEmitted`) is a frozen dataclass with an ergonomic, typed
constructor that derives its ``event_type`` (the class name), its
``payload_schema_version``, and its ``payload`` dict. The
:attr:`Event.envelope_json` property wraps those into the persisted
envelope ``{"component", "data", "schema_version"}``, and
:data:`EVENT_TYPES` maps each ``event_type`` string back to its class so a
persisted envelope can be reconstructed as
``EVENT_TYPES[event_type](component=..., **data)``.

Example:
    >>> event = ConfigLoaded(component="pipeline", config_hash="abc", diff={})
    >>> event.event_type
    'ConfigLoaded'
    >>> event.payload
    {'config_hash': 'abc', 'diff': {}}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: All-zero, SHA-256-width sentinel used as the ``prev_hash`` of the first
#: record in a chain, since it has no predecessor to link back to.
GENESIS_PREV_HASH = "0" * 64

#: Schema version stamped on every M0 event payload. Bump when a payload's
#: shape changes so old and new records remain distinguishable.
_SCHEMA_VERSION = 1


def canonical_json(obj: dict[str, object]) -> str:
    """Serialize a mapping to deterministic, whitespace-free JSON.

    Keys are emitted in sorted order and separators carry no spaces, so the
    output is a byte-stable function of the value's contents alone --
    independent of dict insertion order. This is the exact form the ledger
    hashes over.

    Args:
        obj: The mapping to serialize.

    Returns:
        The canonical JSON encoding of ``obj``.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 microsecond timestamp.

    Returns:
        A string such as ``"2024-01-01T00:00:00.000000+00:00"``.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Event:
    """A ledger event carrying its type, source, schema version, and payload.

    Attributes:
        event_type: Discriminator naming the concrete event kind.
        component: The process or subsystem that produced the event.
        payload_schema_version: Version of the payload's shape.
        payload: The event's type-specific data.
    """

    event_type: str
    component: str
    payload_schema_version: int
    payload: dict[str, object]

    @property
    def envelope_json(self) -> str:
        """Return the canonical JSON envelope persisted for this event.

        Returns:
            Canonical JSON of ``{"component", "data", "schema_version"}``,
            where ``data`` is this event's payload.
        """
        return canonical_json(
            {
                "component": self.component,
                "data": self.payload,
                "schema_version": self.payload_schema_version,
            }
        )


def _derive_typed_event(event: Event, payload: dict[str, object]) -> None:
    """Populate the derived ``Event`` fields on a frozen typed subclass.

    Sets ``event_type`` to the concrete class name, ``payload_schema_version``
    to the current schema version, and ``payload`` to the assembled dict,
    using ``object.__setattr__`` because the instances are frozen.

    Args:
        event: The freshly constructed typed event to populate.
        payload: The type-specific payload assembled by the subclass.
    """
    object.__setattr__(event, "event_type", type(event).__name__)
    object.__setattr__(event, "payload_schema_version", _SCHEMA_VERSION)
    object.__setattr__(event, "payload", payload)


@dataclass(frozen=True)
class ConfigLoaded(Event):
    """Records that a component loaded a configuration revision.

    Attributes:
        config_hash: Content hash identifying the loaded configuration.
        diff: The change from the previously active configuration.
    """

    config_hash: str
    diff: dict[str, object]
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "config_hash": self.config_hash,
            "diff": self.diff,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class ModeHeartbeat(Event):
    """Records a periodic liveness beat for a component's operating mode.

    Attributes:
        mode: The operating mode reported by the beat.
        beat: The monotonically increasing heartbeat counter.
    """

    mode: str
    beat: int
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {"mode": self.mode, "beat": self.beat}
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class AlertEmitted(Event):
    """Records that a component emitted an operational alert.

    Attributes:
        severity: The alert's severity label.
        message: Human-readable description of the alert.
    """

    severity: str
    message: str
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "severity": self.severity,
            "message": self.message,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class PromotionEvaluated(Event):
    """Records the outcome of one Risk Kernel promotion-gate evaluation.

    Attributes:
        source_mode: The mode promotion was requested from (``Mode.name``).
        target_mode: The mode promotion was toward (``Mode.name``).
        approved: Whether every gate criterion passed.
        override_bypassed: Whether an active significance override promoted
            despite the mandatory significance criterion failing -- ``True``
            only when the override rescued a promotion the raw evaluation
            (``approved is False``) would otherwise have blocked.
        evidence: The evaluated ``GateEvidence`` snapshot as a payload.
        results: One per-criterion result payload, in gate order.
    """

    source_mode: str
    target_mode: str
    approved: bool
    override_bypassed: bool
    evidence: dict[str, object]
    results: list[dict[str, object]]
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "source_mode": self.source_mode,
            "target_mode": self.target_mode,
            "approved": self.approved,
            "override_bypassed": self.override_bypassed,
            "evidence": self.evidence,
            "results": self.results,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class SignificanceOverrideApplied(Event):
    """Records the one-way significance-gate override (SPEC S5.1).

    Attributes:
        operator_ack: The exact acknowledgement phrase the operator typed.
        ceiling: The override's mode ceiling (always ``"LIVE_MICRO"``).
    """

    operator_ack: str
    ceiling: str
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "operator_ack": self.operator_ack,
            "ceiling": self.ceiling,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class DemotionTriggerFired(Event):
    """Records one Risk Kernel demotion-trigger firing.

    Attributes:
        trigger: The firing trigger (``DemotionTrigger.name``).
        action: The trigger's mapped action (``DemotionAction.name``).
        from_mode: The mode at firing time (``Mode.name``).
        to_mode: The mode after resolution (``Mode.name``; equals ``from_mode``
            on a no-op firing).
        transitioned: Whether the firing actually moved the mode.
    """

    trigger: str
    action: str
    from_mode: str
    to_mode: str
    transitioned: bool
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "trigger": self.trigger,
            "action": self.action,
            "from_mode": self.from_mode,
            "to_mode": self.to_mode,
            "transitioned": self.transitioned,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class KillEngaged(Event):
    """Records that the Risk Kernel kill switch engaged (issue #35).

    The single announcement event every kill emits, whichever of the four
    triggers fired it. It never carries a sell/close/submit/dump action: a kill
    only halts and cancels, never trades (SPEC position-hold invariant).

    Attributes:
        trigger: The firing trigger's name (``KillTrigger.name``).
        kill_sequence: The monotonic, strictly-increasing kill counter, so a
            re-arm and a subsequent kill are always distinguishable.
        epoch: The wall-clock instant of the kill, in whole epoch seconds (an
            ``int``, never a float -- SPEC S6.1).
    """

    trigger: str
    kill_sequence: int
    epoch: int
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "trigger": self.trigger,
            "kill_sequence": self.kill_sequence,
            "epoch": self.epoch,
        }
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class CancelAllDirective(Event):
    """Records the kill switch's one cancel-all-open-orders directive (issue #35).

    The kill switch cancels resting orders; it never closes or sells the
    positions those orders would have touched (SPEC position-hold invariant),
    so the scope names only open *orders*.

    Attributes:
        scope: The cancellation scope (always ``"all_open_orders"``).
    """

    scope: str
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {"scope": self.scope}
        _derive_typed_event(self, payload)


@dataclass(frozen=True)
class KillReArmed(Event):
    """Records a successful typed-confirmation re-arm out of KILLED (issue #35).

    Attributes:
        kill_sequence: The kill counter of the kill this re-arm cleared, tying
            the re-arm back to its originating kill in the audit trail.
    """

    kill_sequence: int
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {"kill_sequence": self.kill_sequence}
        _derive_typed_event(self, payload)


#: Maps each event_type string to its class, so a persisted envelope can be
#: reconstructed as ``EVENT_TYPES[event_type](component=..., **data)``.
EVENT_TYPES: dict[str, type[Event]] = {
    "ConfigLoaded": ConfigLoaded,
    "ModeHeartbeat": ModeHeartbeat,
    "AlertEmitted": AlertEmitted,
    "PromotionEvaluated": PromotionEvaluated,
    "SignificanceOverrideApplied": SignificanceOverrideApplied,
    "DemotionTriggerFired": DemotionTriggerFired,
    "KillEngaged": KillEngaged,
    "CancelAllDirective": CancelAllDirective,
    "KillReArmed": KillReArmed,
}
