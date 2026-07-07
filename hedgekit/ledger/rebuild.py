"""Fold the ledger into derived read models (SPEC S5.1, issue #13).

``rebuild`` is a pure projection over a verified ledger: it verifies the
hash chain, then folds the records in sequence order into three byte-stable
read-model files -- ``config_versions.json`` (the ``ConfigLoaded`` rows),
``mode_history.json`` (the ``ModeHeartbeat`` rows), and ``gateway_events.json``
(the chronological Order Gateway / crash-recovery events, issue #40).
``AlertEmitted`` and any unrecognized event types are skipped. Because
verification runs first, a corrupt ledger raises :class:`ChainIntegrityError`
instead of producing a plausible-but-wrong projection.

``rebuild_command`` adapts ``rebuild`` to the ``hedgekit rebuild`` CLI,
returning 0 on success and 1 (with the offending ``sequence_number`` on
stderr) when the chain fails verification.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from hedgekit.ledger.store import ChainIntegrityError, SqliteLedgerStore

if TYPE_CHECKING:
    from argparse import Namespace
    from pathlib import Path

    from hedgekit.ledger.store import LedgerRecord

#: Read-model filename holding the ``ConfigLoaded`` projection.
_CONFIG_VERSIONS_FILENAME = "config_versions.json"

#: Read-model filename holding the ``ModeHeartbeat`` projection.
_MODE_HISTORY_FILENAME = "mode_history.json"

#: Read-model filename holding the chronological Order Gateway / crash-recovery
#: event projection (issue #40).
_GATEWAY_EVENTS_FILENAME = "gateway_events.json"

_CONFIG_LOADED = "ConfigLoaded"
_MODE_HEARTBEAT = "ModeHeartbeat"

#: The Order Gateway / crash-recovery event types projected, verbatim, into
#: ``gateway_events.json`` (issue #38-#40), in chronological ledger order.
_GATEWAY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "OrderTransitionLedgered",
        "SubmissionRefused",
        "ReduceOnlyRefused",
        "ReduceOnlyViolation",
        "ReconciliationHalted",
        "ReconciliationHealed",
        "RecoveryCompleted",
    }
)


def _config_projection(record: LedgerRecord) -> dict[str, object]:
    """Project a ``ConfigLoaded`` record into its read-model entry.

    Args:
        record: A ``ConfigLoaded`` ledger record.

    Returns:
        The ``{seq, created_at, config_hash, diff}`` read-model entry.
    """
    data = json.loads(record.payload_json)["data"]
    return {
        "seq": record.sequence_number,
        "created_at": record.created_at,
        "config_hash": data["config_hash"],
        "diff": data["diff"],
    }


def _mode_projection(record: LedgerRecord) -> dict[str, object]:
    """Project a ``ModeHeartbeat`` record into its read-model entry.

    Args:
        record: A ``ModeHeartbeat`` ledger record.

    Returns:
        The ``{seq, created_at, mode, beat}`` read-model entry.
    """
    data = json.loads(record.payload_json)["data"]
    return {
        "seq": record.sequence_number,
        "created_at": record.created_at,
        "mode": data["mode"],
        "beat": data["beat"],
    }


def _gateway_projection(record: LedgerRecord) -> dict[str, object]:
    """Project a gateway/recovery record into its read-model entry.

    Args:
        record: A ledger record whose ``event_type`` is a gateway/recovery type.

    Returns:
        The ``{seq, created_at, event_type, data}`` read-model entry, carrying
        the event's full persisted payload verbatim under ``data``.
    """
    data = json.loads(record.payload_json)["data"]
    return {
        "seq": record.sequence_number,
        "created_at": record.created_at,
        "event_type": record.event_type,
        "data": data,
    }


def _write_read_model(path: Path, rows: list[dict[str, object]]) -> None:
    """Write a read model as canonical JSON bytes with one trailing newline.

    Args:
        path: Destination file path.
        rows: The read-model entries to serialize.
    """
    body = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    path.write_bytes(body.encode("utf-8") + b"\n")


def rebuild(ledger_path: Path, output_dir: Path) -> None:
    """Verify the ledger and fold it into the two read-model files.

    Args:
        ledger_path: Path to the SQLite ledger database.
        output_dir: Directory to write the read models into; created if
            absent.

    Raises:
        ChainIntegrityError: If the ledger's hash chain fails verification.
    """
    store = SqliteLedgerStore(ledger_path)
    try:
        store.verify_chain()
        records = store.read_all()
    finally:
        store.close()

    config_versions = [
        _config_projection(record)
        for record in records
        if record.event_type == _CONFIG_LOADED
    ]
    mode_history = [
        _mode_projection(record)
        for record in records
        if record.event_type == _MODE_HEARTBEAT
    ]
    gateway_events = [
        _gateway_projection(record)
        for record in records
        if record.event_type in _GATEWAY_EVENT_TYPES
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    # ``Path.joinpath`` (not the ``/`` operator) keeps this module clear of the
    # no-float lint's blanket true-division ban on money-path packages (SPEC
    # S6.1); path joining is byte-identical either way.
    _write_read_model(output_dir.joinpath(_CONFIG_VERSIONS_FILENAME), config_versions)
    _write_read_model(output_dir.joinpath(_MODE_HISTORY_FILENAME), mode_history)
    _write_read_model(output_dir.joinpath(_GATEWAY_EVENTS_FILENAME), gateway_events)


def rebuild_command(args: Namespace) -> int:
    """Run ``rebuild`` for the CLI, mapping failures to an exit code.

    Args:
        args: Parsed CLI arguments exposing ``ledger_path`` and
            ``output_dir`` paths.

    Returns:
        0 on a clean rebuild; 1 if the chain fails verification (with the
        offending ``sequence_number`` printed to stderr).
    """
    try:
        rebuild(args.ledger_path, args.output_dir)
    except ChainIntegrityError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0
