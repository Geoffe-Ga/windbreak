"""Recording of configuration-load events for later ledgering (SPEC §16).

Loading a configuration is an auditable event: each load carries the
resulting config hash, the diff against defaults, and its source. This
module defines the :class:`ConfigEventRecorder` boundary the loader calls,
plus an in-memory implementation for tests and early wiring. This boundary is
backed by the real hash-chained ledger in
:mod:`windbreak.config.ledger_recorder` (issue #74).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from windbreak.config.versioning import ConfigDiff


class ConfigEventRecorder(Protocol):
    """The boundary the loader notifies on every configuration load."""

    def record_config_loaded(
        self, *, config_hash: str, diff: ConfigDiff, source: str
    ) -> None:
        """Record that a configuration version was loaded."""


@dataclass(frozen=True, slots=True)
class ConfigLoadEvent:
    """A single recorded configuration-load event.

    Attributes:
        config_hash: The SHA-256 hex digest of the loaded configuration.
        diff: The path-level diff of the loaded config against defaults.
        source: A human-readable origin (a file path or ``<defaults>``).
    """

    config_hash: str
    diff: ConfigDiff
    source: str


@dataclass
class InMemoryConfigEventRecorder:
    """A :class:`ConfigEventRecorder` that accumulates events in memory.

    Attributes:
        events: The recorded load events, in call order.
    """

    events: list[ConfigLoadEvent] = field(default_factory=list)

    def record_config_loaded(
        self, *, config_hash: str, diff: ConfigDiff, source: str
    ) -> None:
        """Append a :class:`ConfigLoadEvent` for the just-loaded configuration."""
        self.events.append(
            ConfigLoadEvent(config_hash=config_hash, diff=diff, source=source)
        )
