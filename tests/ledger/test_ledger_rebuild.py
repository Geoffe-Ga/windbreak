"""Tests for folding the ledger into read models (issue #13, `rebuild`).

`rebuild` is a pure fold over `read_all()` after `verify_chain()` passes.
These tests pin: exact byte-for-byte determinism of the two read-model
files across repeated rebuilds of the same ledger, that only those two
files are ever written, that `AlertEmitted` and unrecognized event types
are silently skipped rather than erroring, that an empty ledger still
produces valid (empty) read models, and that a tampered ledger's
corruption propagates as `ChainIntegrityError` instead of silently
producing a plausible-looking but wrong read model.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from hedgekit.ledger.events import (
    AlertEmitted,
    ConfigLoaded,
    ModeHeartbeat,
    canonical_json,
)
from hedgekit.ledger.rebuild import rebuild
from hedgekit.ledger.store import (
    ChainIntegrityError,
    SqliteLedgerStore,
    compute_event_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path


def _populate_interleaved_ledger(store: SqliteLedgerStore) -> None:
    """Append a fixed, interleaved sequence of all three M0 event types.

    Sequence numbers: 1=ConfigLoaded, 2=ModeHeartbeat, 3=AlertEmitted,
    4=ModeHeartbeat, 5=ConfigLoaded.
    """
    store.append(
        ConfigLoaded(component="pipeline", config_hash="hash-1", diff={"a": 1})
    )
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.append(AlertEmitted(component="alerts", severity="low", message="noop"))
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=2))
    store.append(
        ConfigLoaded(component="pipeline", config_hash="hash-2", diff={"a": 2})
    )


def test_rebuild_writes_only_the_two_documented_read_model_files(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """Only config_versions.json and mode_history.json ever appear in output_dir."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    produced = sorted(path.name for path in output_dir.iterdir())
    assert produced == ["config_versions.json", "mode_history.json"]


def test_rebuild_is_byte_for_byte_deterministic_across_repeated_runs(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """Rebuilding the same ledger into two different output dirs is byte-identical."""
    db_path = tmp_path / "ledger.db"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    first_dir = tmp_path / "out1"
    second_dir = tmp_path / "out2"
    rebuild(db_path, first_dir)
    rebuild(db_path, second_dir)

    for name in ("config_versions.json", "mode_history.json"):
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()


def test_rebuild_config_versions_contains_only_config_loaded_projection_in_order(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """config_versions.json holds exactly the ConfigLoaded rows, in seq order."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    config_versions = json.loads((output_dir / "config_versions.json").read_text())
    assert [entry["seq"] for entry in config_versions] == [1, 5]
    assert [entry["config_hash"] for entry in config_versions] == ["hash-1", "hash-2"]
    assert [entry["diff"] for entry in config_versions] == [{"a": 1}, {"a": 2}]
    assert all("created_at" in entry for entry in config_versions)


def test_rebuild_mode_history_contains_only_mode_heartbeat_projection_in_order(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """mode_history.json holds exactly the ModeHeartbeat rows, in seq order."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    mode_history = json.loads((output_dir / "mode_history.json").read_text())
    assert [entry["seq"] for entry in mode_history] == [2, 4]
    assert [entry["mode"] for entry in mode_history] == ["RESEARCH", "RESEARCH"]
    assert [entry["beat"] for entry in mode_history] == [1, 2]
    assert all("created_at" in entry for entry in mode_history)


def test_rebuild_read_models_are_canonical_json_with_one_trailing_newline(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """Each read model is canonical JSON bytes ending in exactly one newline."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    for name in ("config_versions.json", "mode_history.json"):
        raw = (output_dir / name).read_bytes()
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")
        body = raw[:-1].decode("utf-8")
        assert body == canonical_json(json.loads(body))


def test_rebuild_on_empty_ledger_produces_valid_empty_read_models(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """An empty ledger still produces two well-formed, empty read models."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.close()

    rebuild(db_path, output_dir)

    assert json.loads((output_dir / "config_versions.json").read_text()) == []
    assert json.loads((output_dir / "mode_history.json").read_text()) == []


def test_rebuild_skips_unknown_event_types_without_error(
    tmp_path: Path, deterministic_clock: Callable[[], datetime], ledger_table_name: str
) -> None:
    """An event_type absent from EVENT_TYPES is silently skipped, not an error."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.close()

    created_at = "2099-01-01T00:00:00.000000+00:00"
    payload_json = canonical_json(
        {"component": "future", "data": {"whatever": True}, "schema_version": 1}
    )
    conn = sqlite3.connect(db_path)
    try:
        prev_hash = conn.execute(
            f"SELECT event_hash FROM {ledger_table_name} WHERE sequence_number = 1"
        ).fetchone()[0]
        event_hash = compute_event_hash(
            2, "FutureEvent", created_at, payload_json, prev_hash
        )
        conn.execute(
            f"INSERT INTO {ledger_table_name} ("
            "sequence_number, event_type, created_at, component, "
            "payload_json, payload_schema_version, prev_hash, event_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                "FutureEvent",
                created_at,
                "future",
                payload_json,
                1,
                prev_hash,
                event_hash,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    rebuild(db_path, output_dir)

    assert json.loads((output_dir / "config_versions.json").read_text()) == []
    mode_history = json.loads((output_dir / "mode_history.json").read_text())
    assert len(mode_history) == 1
    assert mode_history[0]["beat"] == 1


def test_rebuild_on_tampered_ledger_raises_chain_integrity_error(
    tmp_path: Path, deterministic_clock: Callable[[], datetime], ledger_table_name: str
) -> None:
    """rebuild verifies the chain first, so a corrupt ledger never projects."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE {ledger_table_name} SET event_hash = ? WHERE sequence_number = 3",
            ("0" * 64,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ChainIntegrityError):
        rebuild(db_path, output_dir)
