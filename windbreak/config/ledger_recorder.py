"""Back the config-load recorder seam with the real ledger (SPEC §16, S5.1).

The config loader (issue #11) notifies a
:class:`~windbreak.config.recorder.ConfigEventRecorder` on every load, and the
hash-chained ledger (issue #13) is the durable, tamper-evident store those
events belong in. This module (issue #74) joins the two: :func:`diff_payload`
renders a :class:`~windbreak.config.versioning.ConfigDiff` into the JSON-safe
shape the :class:`~windbreak.ledger.events.ConfigLoaded` event persists, and
:class:`LedgerConfigEventRecorder` is the store-injected
:class:`~windbreak.config.recorder.ConfigEventRecorder` that appends exactly
one ``ConfigLoaded`` event per load, mirroring the store-injected adapter
pattern of
:class:`~windbreak.order_gateway.ledger_writer.SqliteGatewayLedgerWriter`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from windbreak.ledger.events import ConfigLoaded

if TYPE_CHECKING:
    from windbreak.config.versioning import ConfigDiff
    from windbreak.ledger.store import LedgerStore


def diff_payload(diff: ConfigDiff) -> dict[str, object]:
    """Render a :class:`ConfigDiff` into the JSON-safe ``ConfigLoaded`` diff.

    The ``added`` and ``removed`` maps pass through verbatim; each ``changed``
    entry's ``(old, new)`` tuple is emitted as a two-element ``[old, new]``
    list so the persisted payload round-trips losslessly through
    ``EVENT_TYPES["ConfigLoaded"](component=..., **data)`` (JSON has no tuple,
    and a tuple would otherwise reconstruct as a list and break dataclass
    equality). An empty diff renders to three empty dicts, never omitted keys.
    The output is deterministic under
    :func:`~windbreak.ledger.events.canonical_json`.

    Args:
        diff: The path-level diff of the loaded config against defaults.

    Returns:
        A mapping ``{"added", "removed", "changed"}`` whose ``changed`` values
        are ``[old, new]`` lists.
    """
    return {
        "added": dict(diff.added),
        "removed": dict(diff.removed),
        "changed": {path: [old, new] for path, (old, new) in diff.changed.items()},
    }


class LedgerConfigEventRecorder:
    """A :class:`ConfigEventRecorder` persisting loads to a hash-chained ledger.

    Appends each config-load event to a
    :class:`~windbreak.ledger.store.LedgerStore` as a
    :class:`~windbreak.ledger.events.ConfigLoaded` event, so every loaded
    configuration version becomes a durable, tamper-evident ledger record the
    ``rebuild`` projection (issue #13) can fold into ``config_versions.json``.
    Mirrors the store-injected
    :class:`~windbreak.order_gateway.ledger_writer.SqliteGatewayLedgerWriter`.
    """

    def __init__(self, store: LedgerStore, *, component: str) -> None:
        """Bind the recorder to a ledger store and stamping component.

        Args:
            store: The append-only ledger store every load is persisted to.
            component: The process label stamped on each ``ConfigLoaded`` event.
        """
        self._store = store
        self._component = component

    def record_config_loaded(
        self, *, config_hash: str, diff: ConfigDiff, source: str
    ) -> None:
        """Append one ``ConfigLoaded`` event for the just-loaded configuration.

        The ``source`` argument is intentionally not persisted: the
        :class:`~windbreak.ledger.events.ConfigLoaded` schema has no source
        field, so the human-readable origin is dropped at the ledger boundary.

        Args:
            config_hash: The SHA-256 hex digest of the loaded configuration.
            diff: The path-level diff of the loaded config against defaults.
            source: The human-readable origin; accepted for protocol
                conformance but not persisted.
        """
        del source
        self._store.append(
            ConfigLoaded(
                component=self._component,
                config_hash=config_hash,
                diff=diff_payload(diff),
            )
        )
