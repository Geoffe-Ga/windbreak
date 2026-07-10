"""Failing-first tests for the `restore-from-backup` drill (issue #59, RED).

`windbreak.drills.restore_from_backup` does not exist yet, so the import
below fails collection with `ModuleNotFoundError` -- the expected Gate 1 RED
state for issue #59.

Design assumption (flagged for the implementer, since the issue text leaves
the drill's exact seam open): `RestoreFromBackupDrill.execute` reads its
"original" ledger from `ctx.fixture_dir / "ledger.db"`, copies it into a
fresh scratch directory from `ctx.tmp_dir_factory()` (simulating a restore
from backup), verifies the copy's hash chain, rebuilds *both* the original
and the copy into two further scratch directories, and asserts every
read-model JSON file is byte-identical between them -- proving a restored
backup reproduces the exact same derived operational state. A corrupt
"original" (simulating a corrupted backup) surfaces as `ChainIntegrityError`
from `verify_chain`, which `execute` turns into a `DrillFailedError` carrying the
offending `sequence_number` in its evidence, rather than letting the raw
exception propagate. If the real seam differs, only this file's setup
helpers need updating; the pass/fail assertions below should still hold.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from tests.drills.conftest import (
    FIXED_EPOCH_S,
    InMemoryDrillLedgerWriter,
    make_tmp_dir_factory,
)
from windbreak.drills.context import DrillContext
from windbreak.drills.framework import DrillFailedError, DrillPreconditionError
from windbreak.drills.restore_from_backup import RestoreFromBackupDrill
from windbreak.ledger.events import ConfigLoaded, ModeHeartbeat
from windbreak.ledger.store import SqliteLedgerStore

if TYPE_CHECKING:
    from pathlib import Path


def _seed_ledger(db_path: Path) -> None:
    """Append a small, deterministic event sequence to a fresh ledger."""
    store = SqliteLedgerStore(db_path)
    try:
        store.append(
            ConfigLoaded(component="pipeline", config_hash="hash-1", diff={"a": 1})
        )
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
        store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=2))
    finally:
        store.close()


def _tamper_sequence_three(db_path: Path) -> None:
    """Mutate sequence_number 3's stored hash, breaking the chain at that row."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE ledger SET event_hash = ? WHERE sequence_number = 3",
            ("0" * 64,),
        )
        conn.commit()
    finally:
        conn.close()


def _build_ctx(tmp_path: Path, fixture_dir: Path) -> DrillContext:
    """Build a `DrillContext` rooted at `tmp_path` reading from `fixture_dir`."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={},
        exchange=None,
        state_dir=state_dir,
        fixture_dir=fixture_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=make_tmp_dir_factory(tmp_path / "scratch"),
    )


# --- Equivalence pass: a clean backup restores byte-identically ----------------


def test_restore_from_a_clean_backup_passes_with_byte_identical_read_models(
    tmp_path: Path,
) -> None:
    """A clean, untampered "backup" ledger restores to byte-identical read
    models: the drill passes.
    """
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    _seed_ledger(fixture_dir / "ledger.db")
    ctx = _build_ctx(tmp_path, fixture_dir)
    drill = RestoreFromBackupDrill()

    result = drill.run(ctx)

    assert result.passed is True
    assert result.drill == "restore-from-backup"
    json.dumps(result.evidence)  # evidence is JSON-serializable by construction
    # A seeded ledger must actually compare at least one read model, so a
    # rebuild regression that silently emits nothing cannot pass vacuously.
    compared_files = result.evidence["compared_files"]
    assert isinstance(compared_files, int)
    assert compared_files >= 1
    assert result.evidence["read_models_identical"] is True


def test_restore_from_an_empty_ledger_is_a_trivial_passing_edge_case(
    tmp_path: Path,
) -> None:
    """An empty ledger (no events appended) still restores cleanly: an empty
    chain is trivially valid and every read model is trivially empty on both
    sides.
    """
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    SqliteLedgerStore(fixture_dir / "ledger.db").close()
    ctx = _build_ctx(tmp_path, fixture_dir)
    drill = RestoreFromBackupDrill()

    result = drill.run(ctx)

    assert result.passed is True


# --- Negative: a tampered "backup" fails closed with the offending seq ---------


def test_restore_from_a_tampered_backup_fails_with_the_offending_sequence_number(
    tmp_path: Path,
) -> None:
    """A tampered byte in the "backup" ledger surfaces as a chain-integrity
    failure: the drill fails (never silently passing a corrupted restore),
    and the offending `sequence_number` (3) is present in its evidence.
    """
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    _seed_ledger(fixture_dir / "ledger.db")
    _tamper_sequence_three(fixture_dir / "ledger.db")
    ctx = _build_ctx(tmp_path, fixture_dir)
    drill = RestoreFromBackupDrill()

    result = drill.run(ctx)

    assert result.passed is False
    assert result.evidence["sequence_number"] == 3


# --- Determinism: rebuilding the same ledger twice agrees on the verdict -------


def test_running_the_drill_twice_against_the_same_backup_agrees_on_pass_fail(
    tmp_path: Path,
) -> None:
    """Two independent runs of the drill against the identical backup ledger
    agree on the pass/fail verdict -- the comparison is a pure function of the
    ledger's content, not of run-to-run incidental state.
    """
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    _seed_ledger(fixture_dir / "ledger.db")
    ctx_one = _build_ctx(tmp_path / "run-one", fixture_dir)
    ctx_two = _build_ctx(tmp_path / "run-two", fixture_dir)
    drill = RestoreFromBackupDrill()

    first = drill.run(ctx_one)
    second = drill.run(ctx_two)

    assert first.passed is True
    assert second.passed is True


# --- Negative / fault-injection: FAILURE branches (issue #59 Gate 1 coverage) --


def test_restore_from_backup_precondition_raises_when_no_backup_ledger_exists(
    tmp_path: Path,
) -> None:
    """`check_preconditions` raises `DrillPreconditionError` when the fixture
    directory holds no `ledger.db` at all -- there is nothing to restore
    from.
    """
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    ctx = _build_ctx(tmp_path, fixture_dir)
    drill = RestoreFromBackupDrill()

    with pytest.raises(DrillPreconditionError):
        drill.check_preconditions(ctx)


def test_diff_read_models_fails_when_a_read_model_file_diverges(
    tmp_path: Path,
) -> None:
    """`_diff_read_models` raises `DrillFailedError` naming exactly the diverging
    file when the two rebuilt directories disagree on its bytes.

    This is a *different* branch from the tampered-backup case above: there,
    corruption breaks `verify_chain` before rebuild ever runs (every stored
    column is covered by the hash chain or its envelope cross-check, so any
    byte-level tamper is caught there first). A read-model divergence with an
    otherwise-intact chain on both sides can only be exercised by driving
    `_diff_read_models` directly against two rebuilt directories that
    disagree -- e.g. two independent rebuild runs, or an out-of-band file
    drift -- without needing to break the chain at all.
    """
    original = tmp_path / "original"
    restored = tmp_path / "restored"
    original.mkdir()
    restored.mkdir()
    (original / "config_versions.json").write_bytes(b"[]\n")
    (restored / "config_versions.json").write_bytes(b'[{"seq":1}]\n')
    drill = RestoreFromBackupDrill()

    with pytest.raises(DrillFailedError) as excinfo:
        drill._diff_read_models(original, restored)

    assert excinfo.value.evidence == {
        "read_models_identical": False,
        "diverging_files": ["config_versions.json"],
    }
