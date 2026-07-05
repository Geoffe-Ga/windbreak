"""Shared component: the append-only, hash-chained ledger (SPEC S5.1).

Provides the tamper-evident event log every process writes to and that
Evaluation and the Dashboard read from. Events (:class:`ConfigLoaded`,
:class:`ModeHeartbeat`, :class:`AlertEmitted`) are appended to a
:class:`SqliteLedgerStore`, which chains each record's SHA-256 hash to its
predecessor so :meth:`SqliteLedgerStore.verify_chain` can detect any
single-column corruption. :func:`rebuild` folds a verified ledger into
derived read models.

Example:
    >>> from pathlib import Path
    >>> store = SqliteLedgerStore(Path("ledger.db"))
    >>> event = ConfigLoaded(component="pipeline", config_hash="abc", diff={})
    >>> seq = store.append(event)
    >>> store.verify_chain()
    >>> store.close()
    >>> rebuild(Path("ledger.db"), Path("read_models"))
"""

from __future__ import annotations

from hedgekit.ledger.events import (
    EVENT_TYPES,
    GENESIS_PREV_HASH,
    AlertEmitted,
    ConfigLoaded,
    Event,
    ModeHeartbeat,
    canonical_json,
)
from hedgekit.ledger.rebuild import rebuild, rebuild_command
from hedgekit.ledger.store import (
    ChainIntegrityError,
    LedgerRecord,
    LedgerStore,
    SqliteLedgerStore,
    compute_event_hash,
)

__all__ = [
    "EVENT_TYPES",
    "GENESIS_PREV_HASH",
    "AlertEmitted",
    "ChainIntegrityError",
    "ConfigLoaded",
    "Event",
    "LedgerRecord",
    "LedgerStore",
    "ModeHeartbeat",
    "SqliteLedgerStore",
    "canonical_json",
    "compute_event_hash",
    "rebuild",
    "rebuild_command",
]
