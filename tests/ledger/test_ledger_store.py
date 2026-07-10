"""Tests for `windbreak.ledger.store.SqliteLedgerStore` (issue #13, #75).

Pins the on-disk contract from SPEC §12: monotonically increasing
sequence numbers starting at 1, hash chaining from a fixed genesis
`prev_hash`, WAL journaling, exact round-trip of every persisted column,
and durability across a store close/reopen cycle -- since the ledger
must survive process restarts.

The `compute_event_hash` known-answer test is the mutation-killer for
the hashing formula itself: it independently recomputes the SHA-256
digest in the test body using the exact §12 concatenation and asserts
byte-for-byte equality with the production function's output.

The `head()` tests (issue #75) pin the read used by anchor-head
computation: the current chain head as a `ChainHead(sequence_number,
event_hash)`, or `None` for an empty ledger.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from windbreak.ledger.events import GENESIS_PREV_HASH, ConfigLoaded, ModeHeartbeat
from windbreak.ledger.store import (
    ChainHead,
    LedgerRecord,
    SqliteLedgerStore,
    compute_event_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_append_returns_monotonically_increasing_sequence_numbers(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """The 1st, 2nd, and 3rd appended events receive sequence numbers 1, 2, 3."""
    store = ledger_store_factory()
    event = ConfigLoaded(component="pipeline", config_hash="abc", diff={})

    first = store.append(event)
    second = store.append(event)
    third = store.append(event)

    assert (first, second, third) == (1, 2, 3)


def test_first_record_prev_hash_is_genesis(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """The very first appended row links back to the genesis sentinel."""
    store = ledger_store_factory()
    store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))

    records = store.read_all()

    assert records[0].prev_hash == GENESIS_PREV_HASH


def test_each_record_prev_hash_links_to_predecessor_event_hash(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """Every non-genesis row's prev_hash equals its predecessor's event_hash."""
    store = ledger_store_factory()
    store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=2))

    records = store.read_all()

    assert records[1].prev_hash == records[0].event_hash
    assert records[2].prev_hash == records[1].event_hash


def test_compute_event_hash_matches_independent_sha256_known_answer() -> None:
    """KNOWN-ANSWER: pins the exact §12 concatenation formula byte-for-byte.

    The expected digest is computed here, independently of production
    code, from the literal concatenation
    ``str(sequence_number) + event_type + created_at + payload_json + prev_hash``.
    """
    sequence_number = 7
    event_type = "ConfigLoaded"
    created_at = "2024-01-01T00:00:00.000000+00:00"
    payload_json = (
        '{"component":"pipeline","data":{"config_hash":"abc"},"schema_version":1}'
    )
    prev_hash = "f" * 64

    expected = hashlib.sha256(
        (
            str(sequence_number) + event_type + created_at + payload_json + prev_hash
        ).encode("utf-8")
    ).hexdigest()

    actual = compute_event_hash(
        sequence_number, event_type, created_at, payload_json, prev_hash
    )

    assert actual == expected
    assert len(actual) == 64


def test_compute_event_hash_changes_when_sequence_number_changes() -> None:
    """A different sequence_number produces a different digest.

    Kills constant-fold mutants that ignore sequence_number entirely.
    """
    common_args = ("ConfigLoaded", "2024-01-01T00:00:00.000000+00:00", "{}", "f" * 64)

    assert compute_event_hash(1, *common_args) != compute_event_hash(2, *common_args)


def test_store_uses_wal_journal_mode(
    ledger_store_factory: Callable[..., SqliteLedgerStore], tmp_path: Path
) -> None:
    """The store opens its SQLite connection in WAL journal mode."""
    ledger_store_factory("wal_check.db")

    conn = sqlite3.connect(tmp_path / "wal_check.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    assert mode.lower() == "wal"


def test_read_all_round_trips_all_eight_persisted_fields(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """read_all() reconstructs every LedgerRecord field exactly as persisted."""
    store = ledger_store_factory("roundtrip.db")
    event = ConfigLoaded(component="pipeline", config_hash="deadbeef", diff={"x": 1})

    store.append(event)
    records = store.read_all()

    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LedgerRecord)
    assert record.sequence_number == 1
    assert record.event_type == "ConfigLoaded"
    assert record.component == "pipeline"
    assert record.payload_schema_version == 1
    assert record.prev_hash == GENESIS_PREV_HASH
    assert record.event_hash == compute_event_hash(
        record.sequence_number,
        record.event_type,
        record.created_at,
        record.payload_json,
        record.prev_hash,
    )
    assert '"config_hash":"deadbeef"' in record.payload_json


def test_read_all_returns_records_in_ascending_sequence_order(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """read_all() returns rows ordered 1..N, not insertion or arbitrary order."""
    store = ledger_store_factory("ordering.db")
    for beat in range(1, 4):
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=beat))

    records = store.read_all()

    assert [record.sequence_number for record in records] == [1, 2, 3]


def test_data_persists_across_store_close_and_reopen(
    tmp_path: Path, deterministic_clock: Callable[[], object]
) -> None:
    """A fresh SqliteLedgerStore on the same path sees prior appends."""
    db_path = tmp_path / "reopen.db"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))
    store.close()

    reopened = SqliteLedgerStore(db_path, now=deterministic_clock)
    try:
        records = reopened.read_all()
    finally:
        reopened.close()

    assert len(records) == 1
    assert records[0].sequence_number == 1
    assert records[0].event_type == "ConfigLoaded"


def test_append_after_reopen_continues_the_same_sequence_and_hash_chain(
    tmp_path: Path, deterministic_clock: Callable[[], object]
) -> None:
    """Appending after a reopen continues sequence numbers and prev_hash linkage."""
    db_path = tmp_path / "continue.db"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))
    store.close()

    reopened = SqliteLedgerStore(db_path, now=deterministic_clock)
    try:
        second_seq = reopened.append(
            ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1)
        )
        records = reopened.read_all()
    finally:
        reopened.close()

    assert second_seq == 2
    assert records[1].prev_hash == records[0].event_hash


def test_append_without_injected_clock_uses_default_clock_for_created_at(
    tmp_path: Path,
) -> None:
    """With no `now=` override, append() stamps `created_at` via the real UTC clock.

    Every other test in this suite injects `DeterministicClock`; this is the
    one place `_default_clock` itself runs, so it must independently verify
    the timestamp it produces is a parseable, UTC, microsecond-precision
    ISO-8601 string.
    """
    db_path = tmp_path / "default_clock.db"
    store = SqliteLedgerStore(db_path)
    try:
        store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))
        records = store.read_all()
    finally:
        store.close()

    created_at = records[0].created_at
    parsed = datetime.fromisoformat(created_at)

    assert parsed.utcoffset() == timedelta(0)
    assert created_at.endswith("+00:00")
    fractional_and_offset = created_at.split(".", 1)[1]
    microseconds_digits = fractional_and_offset.split("+", 1)[0]
    assert len(microseconds_digits) == 6
    assert microseconds_digits.isdigit()


def test_head_returns_none_for_empty_ledger(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """head() on a ledger with zero appended rows returns None."""
    store = ledger_store_factory()

    assert store.head() is None


def test_head_returns_chain_head_matching_the_last_appended_row(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """head() returns a ChainHead pinned to the Nth appended row's seq/hash.

    Builds a 5-row chain and asserts head() equals `ChainHead(5,
    <the 5th row's event_hash>)` exactly -- a mutant that returns the wrong
    row (e.g. the first, or an off-by-one) is caught by dataclass equality.
    """
    store = ledger_store_factory()
    for beat in range(1, 6):
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=beat))

    head = store.head()
    last_record = store.read_all()[-1]

    assert head == ChainHead(sequence_number=5, event_hash=last_record.event_hash)


def test_head_matches_the_last_read_all_row_exactly(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """head()'s fields equal the last read_all() row's sequence_number/event_hash."""
    store = ledger_store_factory()
    store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))

    records = store.read_all()
    head = store.head()

    assert head is not None
    assert head.sequence_number == records[-1].sequence_number
    assert head.event_hash == records[-1].event_hash


def test_head_reflects_the_new_head_after_a_further_append(
    ledger_store_factory: Callable[..., SqliteLedgerStore],
) -> None:
    """head() called again after a further append reports the new head, not the old."""
    store = ledger_store_factory()
    store.append(ConfigLoaded(component="pipeline", config_hash="abc", diff={}))
    first_head = store.head()

    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    second_head = store.head()

    assert first_head is not None
    assert first_head == ChainHead(sequence_number=1, event_hash=first_head.event_hash)
    assert second_head is not None
    assert second_head.sequence_number == 2
    assert second_head.event_hash != first_head.event_hash


def test_chain_head_is_frozen() -> None:
    """ChainHead is an immutable dataclass: assigning to a field raises."""
    head = ChainHead(sequence_number=1, event_hash="a" * 64)

    with pytest.raises(dataclasses.FrozenInstanceError):
        head.sequence_number = 2  # type: ignore[misc]
