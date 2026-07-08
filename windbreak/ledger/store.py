"""SQLite-backed, append-only, hash-chained ledger store (SPEC S5.1, §12).

Persists a tamper-evident log of :class:`~windbreak.ledger.events.Event`
records. Each row carries a monotonically increasing ``sequence_number``
starting at 1 and a SHA-256 ``event_hash`` that chains to its
predecessor's hash (the first row chains to
:data:`~windbreak.ledger.events.GENESIS_PREV_HASH`). Because any change to a
persisted row breaks the chain, :meth:`SqliteLedgerStore.verify_chain` can
detect corruption of any single column.

The store only ever inserts and reads rows -- it exposes no mutation path
by design, which is what makes the log trustworthy as an audit trail. The
package's SQL is statically checked to remain insert-and-select only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from windbreak.ledger.events import GENESIS_PREV_HASH

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from windbreak.ledger.events import Event

#: DDL creating the eight-column §12 ledger row if it does not yet exist.
_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS ledger ("
    "sequence_number INTEGER PRIMARY KEY, "
    "event_type TEXT NOT NULL, "
    "created_at TEXT NOT NULL, "
    "component TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, "
    "payload_schema_version INTEGER NOT NULL, "
    "prev_hash TEXT NOT NULL, "
    "event_hash TEXT NOT NULL"
    ")"
)

_INSERT_SQL = (
    "INSERT INTO ledger ("
    "sequence_number, event_type, created_at, component, "
    "payload_json, payload_schema_version, prev_hash, event_hash"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

_SELECT_ALL_SQL = (
    "SELECT sequence_number, event_type, created_at, component, "
    "payload_json, payload_schema_version, prev_hash, event_hash "
    "FROM ledger ORDER BY sequence_number"
)

_SELECT_LAST_SQL = (
    "SELECT sequence_number, event_hash FROM ledger "
    "ORDER BY sequence_number DESC LIMIT 1"
)


def _default_clock() -> datetime:
    """Return the current UTC time as a timezone-aware datetime.

    Returns:
        ``datetime.now`` in the UTC timezone.
    """
    return datetime.now(UTC)


def compute_event_hash(
    sequence_number: int,
    event_type: str,
    created_at: str,
    payload_json: str,
    prev_hash: str,
) -> str:
    """Compute a record's chained SHA-256 hash from its §12 fields.

    Hashes the exact concatenation
    ``str(sequence_number) + event_type + created_at + payload_json +
    prev_hash``, so the digest binds the record's position, type,
    timestamp, payload, and its link to the predecessor's hash.

    Args:
        sequence_number: The record's 1-based position in the chain.
        event_type: The record's event type discriminator.
        created_at: The record's ISO-8601 creation timestamp.
        payload_json: The canonical envelope JSON persisted for the record.
        prev_hash: The predecessor's ``event_hash`` (genesis for the first).

    Returns:
        The 64-character hex SHA-256 digest.
    """
    digest_input = (
        str(sequence_number) + event_type + created_at + payload_json + prev_hash
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerRecord:
    """One persisted ledger row: the eight §12 columns, read back verbatim.

    Attributes:
        sequence_number: The record's 1-based position in the chain.
        event_type: The record's event type discriminator.
        created_at: The record's ISO-8601 creation timestamp.
        component: The producing component projected from the envelope.
        payload_json: The canonical envelope JSON stored for the record.
        payload_schema_version: Payload schema version projected from the
            envelope.
        prev_hash: The predecessor's ``event_hash``.
        event_hash: This record's chained hash.
    """

    sequence_number: int
    event_type: str
    created_at: str
    component: str
    payload_json: str
    payload_schema_version: int
    prev_hash: str
    event_hash: str


class ChainIntegrityError(Exception):
    """Raised when the ledger's hash chain fails verification.

    Attributes:
        sequence_number: The expected sequence position at which the first
            violation was detected.
    """

    def __init__(self, sequence_number: int) -> None:
        """Initialize the error with the offending sequence position.

        Args:
            sequence_number: The expected sequence position of the first
                detected violation.
        """
        self.sequence_number = sequence_number
        super().__init__(
            f"ledger chain integrity violation at sequence_number={sequence_number}"
        )


class LedgerStore(Protocol):
    """Structural interface for an append-only, hash-chained ledger."""

    def append(self, event: Event) -> int:
        """Append an event and return its assigned sequence number."""

    def read_all(self) -> list[LedgerRecord]:
        """Return every persisted record in ascending sequence order."""

    def verify_chain(self) -> None:
        """Verify the hash chain, raising ``ChainIntegrityError`` on tamper."""

    def close(self) -> None:
        """Release the underlying storage resources."""


class SqliteLedgerStore:
    """A :class:`LedgerStore` persisted to a WAL-journaled SQLite database.

    The connection runs in autocommit mode so each append can wrap its
    insert in an explicit ``BEGIN IMMEDIATE`` transaction, and the ledger
    table is created on first use if absent.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        now: Callable[[], datetime] = _default_clock,
    ) -> None:
        """Open (or create) the ledger database at ``db_path``.

        Args:
            db_path: Filesystem path to the SQLite database file.
            now: Clock returning the timezone-aware datetime stamped as each
                record's ``created_at``. Injectable for deterministic tests.
        """
        self._now = now
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE_SQL)

    def append(self, event: Event) -> int:
        """Append an event as the next record in the chain.

        Args:
            event: The event to persist.

        Returns:
            The sequence number assigned to the new record.
        """
        created_at = self._now().isoformat(timespec="microseconds")
        payload_json = event.envelope_json
        self._conn.execute("BEGIN IMMEDIATE")
        last = self._conn.execute(_SELECT_LAST_SQL).fetchone()
        if last is None:
            sequence_number = 1
            prev_hash = GENESIS_PREV_HASH
        else:
            sequence_number = int(last[0]) + 1
            prev_hash = str(last[1])
        event_hash = compute_event_hash(
            sequence_number, event.event_type, created_at, payload_json, prev_hash
        )
        self._conn.execute(
            _INSERT_SQL,
            (
                sequence_number,
                event.event_type,
                created_at,
                event.component,
                payload_json,
                event.payload_schema_version,
                prev_hash,
                event_hash,
            ),
        )
        self._conn.execute("COMMIT")
        return sequence_number

    def read_all(self) -> list[LedgerRecord]:
        """Return every record in ascending sequence order.

        Returns:
            The persisted records as :class:`LedgerRecord` instances.
        """
        rows = self._conn.execute(_SELECT_ALL_SQL).fetchall()
        return [LedgerRecord(*row) for row in rows]

    def verify_chain(self) -> None:
        """Verify sequence contiguity and hash linkage across the chain.

        Raises:
            ChainIntegrityError: On the first row whose sequence number,
                recomputed hash, predecessor link, or envelope projection
                does not match, reporting that row's expected position.
        """
        expected_prev_hash = GENESIS_PREV_HASH
        expected_seq = 1
        for record in self.read_all():
            self._verify_row(record, expected_seq, expected_prev_hash)
            expected_prev_hash = record.event_hash
            expected_seq += 1

    def _verify_row(
        self, record: LedgerRecord, expected_seq: int, expected_prev_hash: str
    ) -> None:
        """Verify one record against its expected position and predecessor.

        Args:
            record: The record to verify.
            expected_seq: The sequence number this position must hold.
            expected_prev_hash: The predecessor's ``event_hash``.

        Raises:
            ChainIntegrityError: If sequence, hash, or link checks fail.
        """
        if record.sequence_number != expected_seq:
            raise ChainIntegrityError(expected_seq)
        recomputed = compute_event_hash(
            record.sequence_number,
            record.event_type,
            record.created_at,
            record.payload_json,
            record.prev_hash,
        )
        if recomputed != record.event_hash:
            raise ChainIntegrityError(expected_seq)
        if record.prev_hash != expected_prev_hash:
            raise ChainIntegrityError(expected_seq)
        self._verify_envelope(record, expected_seq)

    def _verify_envelope(self, record: LedgerRecord, expected_seq: int) -> None:
        """Verify a record's column projections match its stored envelope.

        Args:
            record: The record whose ``component`` and schema version are
                checked against its ``payload_json`` envelope.
            expected_seq: The sequence position reported on mismatch.

        Raises:
            ChainIntegrityError: If a projected column disagrees with the
                envelope, or the envelope is malformed or missing a required
                key.
        """
        try:
            envelope: dict[str, object] = json.loads(record.payload_json)
            projections_match = (
                record.component == envelope["component"]
                and record.payload_schema_version == envelope["schema_version"]
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ChainIntegrityError(expected_seq) from exc
        if not projections_match:
            raise ChainIntegrityError(expected_seq)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
