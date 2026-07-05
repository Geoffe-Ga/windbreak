"""Tests for `SqliteLedgerStore.verify_chain` (issue #13).

`verify_chain` is the tamper-evidence gate: it must pass on an empty,
single-event, or N-event untampered chain, and it must independently
detect corruption of every one of the ledger's eight persisted columns,
raising `ChainIntegrityError` naming the exact (expected) sequence
position where the first violation is detected.

Tampering is injected via a raw `sqlite3` connection directly against
the on-disk table -- the only way to simulate corruption, since the
public `LedgerStore` API has no mutation method (see
`test_ledger_append_only_sql.py`).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from hedgekit.ledger.events import GENESIS_PREV_HASH, ModeHeartbeat, canonical_json
from hedgekit.ledger.store import (
    ChainIntegrityError,
    SqliteLedgerStore,
    compute_event_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Every column of the 8-field §12 row, paired with a tampered value.
#: One test case per column, so a regression in any single column's
#: verification logic is caught in isolation.
_TAMPER_CASES = [
    ("sequence_number", 99),
    ("event_type", "TamperedType"),
    ("created_at", "2099-01-01T00:00:00.000000+00:00"),
    ("component", "tampered-component"),
    (
        "payload_json",
        '{"component":"tampered","data":{},"schema_version":1}',
    ),
    ("payload_schema_version", 999),
    ("prev_hash", "1" * 64),
    ("event_hash", "2" * 64),
]


def test_verify_chain_passes_on_empty_ledger(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """An empty ledger has nothing to verify and raises nothing."""
    store = ledger_store_factory()

    store.verify_chain()


def test_verify_chain_passes_on_single_untampered_event(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """A single, untampered genesis event verifies cleanly."""
    store = ledger_store_factory()
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))

    store.verify_chain()


def test_verify_chain_passes_on_n_untampered_events(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """A longer, untampered chain of events verifies cleanly."""
    store = ledger_store_factory()
    for beat in range(1, 6):
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=beat))

    store.verify_chain()


def _tamper(
    db_path: Path, table: str, sequence_number: int, column: str, value: object
) -> None:
    """Directly UPDATE one column of one row via raw sqlite3 (test-only).

    Args:
        db_path: Path to the SQLite database file.
        table: Name of the ledger table.
        sequence_number: The row's `sequence_number` to target.
        column: The column to corrupt.
        value: The tampered value to write.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE sequence_number = ?",
            (value, sequence_number),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(("column", "value"), _TAMPER_CASES)
def test_verify_chain_detects_tampering_in_every_column(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
    tmp_path: Path,
    ledger_table_name: str,
    column: str,
    value: object,
) -> None:
    """Tampering any single column of a mid-chain row breaks verify_chain.

    Builds a 3-row chain, corrupts row 2 (the middle row) in exactly one
    column, reopens the store, and asserts `ChainIntegrityError` is
    raised reporting the tampered row's expected sequence position (2)
    both in its message and via its `.sequence_number` attribute.
    """
    db_name = "tamper.db"
    store = ledger_store_factory(db_name)
    for beat in range(1, 4):
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=beat))
    store.close()

    _tamper(
        tmp_path / db_name,
        ledger_table_name,
        sequence_number=2,
        column=column,
        value=value,
    )

    reopened = SqliteLedgerStore(tmp_path / db_name)
    try:
        with pytest.raises(ChainIntegrityError) as exc_info:
            reopened.verify_chain()
    finally:
        reopened.close()

    assert exc_info.value.sequence_number == 2
    assert "sequence_number=2" in str(exc_info.value)


def test_verify_chain_reports_the_first_violation_not_a_later_one(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
    tmp_path: Path,
    ledger_table_name: str,
) -> None:
    """When rows 2 and 4 are both tampered, verify_chain reports row 2 first."""
    db_name = "double_tamper.db"
    store = ledger_store_factory(db_name)
    for beat in range(1, 6):
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=beat))
    store.close()

    db_path = tmp_path / db_name
    _tamper(
        db_path,
        ledger_table_name,
        sequence_number=4,
        column="event_hash",
        value="a" * 64,
    )
    _tamper(
        db_path,
        ledger_table_name,
        sequence_number=2,
        column="event_hash",
        value="b" * 64,
    )

    reopened = SqliteLedgerStore(db_path)
    try:
        with pytest.raises(ChainIntegrityError) as exc_info:
            reopened.verify_chain()
    finally:
        reopened.close()

    assert exc_info.value.sequence_number == 2


def test_verify_chain_catches_internally_consistent_forgery_via_prev_hash_linkage(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
    tmp_path: Path,
    ledger_table_name: str,
) -> None:
    """A forged row that recomputes its OWN hash correctly is still caught.

    Single-column tampering (see `_TAMPER_CASES` above) always trips the
    hash-recompute check first, so `_verify_row`'s `prev_hash` linkage
    branch (`if record.prev_hash != expected_prev_hash: raise
    ChainIntegrityError(...)`) is never exercised by those cases. This
    constructs the one attack only that branch can catch: row 2's
    `payload_json` is rewritten to a forged-but-plausible envelope and its
    `event_hash` is *recomputed* to match, so row 2 is internally
    consistent -- its own hash-recompute and envelope-projection checks
    both pass. Row 3 is left untouched, so row 3's `prev_hash` no longer
    links to row 2's new `event_hash`, and only the linkage check fires,
    at row 3.
    """
    db_name = "forgery.db"
    store = ledger_store_factory(db_name)
    for beat in range(1, 4):
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=beat))
    store.close()

    db_path = tmp_path / db_name
    conn = sqlite3.connect(db_path)
    try:
        event_type, created_at, prev_hash = conn.execute(
            f"SELECT event_type, created_at, prev_hash FROM {ledger_table_name} "
            "WHERE sequence_number = 2"
        ).fetchone()
        forged_payload_json = canonical_json(
            {
                "component": "pipeline",
                "data": {"mode": "RESEARCH", "beat": 999},
                "schema_version": 1,
            }
        )
        forged_event_hash = compute_event_hash(
            2, event_type, created_at, forged_payload_json, prev_hash
        )
        conn.execute(
            f"UPDATE {ledger_table_name} "
            "SET payload_json = ?, event_hash = ? WHERE sequence_number = 2",
            (forged_payload_json, forged_event_hash),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = SqliteLedgerStore(db_path)
    try:
        with pytest.raises(ChainIntegrityError) as exc_info:
            reopened.verify_chain()
    finally:
        reopened.close()

    assert exc_info.value.sequence_number == 3
    assert "sequence_number=3" in str(exc_info.value)


def test_verify_chain_detects_non_contiguous_starting_sequence(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
    tmp_path: Path,
    ledger_table_name: str,
) -> None:
    """A row that is internally self-consistent for seq=2 still fails at seq=1.

    Isolates the `if record.sequence_number != expected_seq:` contiguity
    check in `_verify_row`, which no other test in this suite uniquely
    exercises: every `_TAMPER_CASES` row-99 mutation and every forgery
    above builds a chain that legitimately starts at sequence_number=1, so
    a mutant that deletes the contiguity branch entirely would still be
    caught by later checks (or not exercised at all) without ever being
    forced through *this* specific branch as the sole failure reason.

    Here, the single persisted row recomputes its own hash correctly,
    its `prev_hash` correctly links back to `GENESIS_PREV_HASH` (its only
    possible predecessor), and its `component`/`schema_version` columns
    match its envelope -- every other `_verify_row` check passes. Only
    the requirement that a chain start contiguously at sequence_number=1
    is violated, so only the contiguity branch can raise here.
    """
    db_name = "noncontiguous.db"
    store = ledger_store_factory(db_name)
    store.close()

    created_at = "2024-01-01T00:00:00.000000+00:00"
    payload_json = canonical_json(
        {
            "component": "pipeline",
            "data": {"mode": "RESEARCH", "beat": 1},
            "schema_version": 1,
        }
    )
    event_hash = compute_event_hash(
        2, "ModeHeartbeat", created_at, payload_json, GENESIS_PREV_HASH
    )

    db_path = tmp_path / db_name
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {ledger_table_name} ("
            "sequence_number, event_type, created_at, component, "
            "payload_json, payload_schema_version, prev_hash, event_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                "ModeHeartbeat",
                created_at,
                "pipeline",
                payload_json,
                1,
                GENESIS_PREV_HASH,
                event_hash,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = SqliteLedgerStore(db_path)
    try:
        with pytest.raises(ChainIntegrityError) as exc_info:
            reopened.verify_chain()
    finally:
        reopened.close()

    assert exc_info.value.sequence_number == 1
    assert "sequence_number=1" in str(exc_info.value)


#: Two ways `payload_json` can be corrupt without `_verify_row`'s prior
#: checks (contiguity, hash-recompute, prev_hash linkage) noticing, because
#: the row's `event_hash` is recomputed over the corrupt bytes to match.
_MALFORMED_ENVELOPE_CASES = [
    pytest.param("this is not json{", id="invalid-json"),
    pytest.param(
        canonical_json({"data": {"mode": "RESEARCH", "beat": 1}, "schema_version": 1}),
        id="missing-component-key",
    ),
]


@pytest.mark.parametrize("corrupt_payload_json", _MALFORMED_ENVELOPE_CASES)
def test_verify_chain_wraps_malformed_envelope_as_chain_integrity_error(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
    tmp_path: Path,
    ledger_table_name: str,
    corrupt_payload_json: str,
) -> None:
    """A corrupt `payload_json` must surface as `ChainIntegrityError`.

    Pins the failure *contract* of `_verify_envelope`: whatever is wrong
    with a stored envelope, callers of `verify_chain` should only ever see
    `ChainIntegrityError`, never a raw `json.JSONDecodeError` (malformed
    JSON) or `KeyError` (valid JSON missing a required key) leaking out of
    the store's internals.

    Builds a genuine single-row chain via the public API, then rewrites
    row 1's `payload_json` to a corrupt value and *recomputes* row 1's
    `event_hash` over that corrupt value (using the row's real, persisted
    `created_at`), so the row still passes contiguity, hash-recompute, and
    prev_hash-linkage -- reaching `_verify_envelope` is the only way this
    corruption can be detected.
    """
    db_name = "malformed_envelope.db"
    store = ledger_store_factory(db_name)
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.close()

    db_path = tmp_path / db_name
    conn = sqlite3.connect(db_path)
    try:
        created_at = conn.execute(
            f"SELECT created_at FROM {ledger_table_name} WHERE sequence_number = 1"
        ).fetchone()[0]
        corrupt_event_hash = compute_event_hash(
            1, "ModeHeartbeat", created_at, corrupt_payload_json, GENESIS_PREV_HASH
        )
        conn.execute(
            f"UPDATE {ledger_table_name} "
            "SET payload_json = ?, event_hash = ? WHERE sequence_number = 1",
            (corrupt_payload_json, corrupt_event_hash),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = SqliteLedgerStore(db_path)
    try:
        with pytest.raises(ChainIntegrityError) as exc_info:
            reopened.verify_chain()
    finally:
        reopened.close()

    assert exc_info.value.sequence_number == 1
