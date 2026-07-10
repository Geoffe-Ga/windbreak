"""Failing-first tests for the "CI runs every drill" guarantee (issue #59, RED).

`windbreak.drills.registry` does not exist yet, so the import below fails
collection with `ModuleNotFoundError` -- the expected Gate 1 RED state for
issue #59.

Parametrized over every registered drill: each one, run against a shared
paper `DrillContext`, passes and ledgers exactly one
`DrillCompleted(passed=True)` -- the CI-runs-every-drill guarantee the
`windbreak drill <name>` verb exists to make routine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.drills.conftest import (
    FIXED_EPOCH_S,
    InMemoryDrillLedgerWriter,
    make_tmp_dir_factory,
)
from windbreak.drills.context import DrillContext
from windbreak.drills.exchanges import HeldPositionsExchange
from windbreak.drills.framework import run_drill
from windbreak.drills.registry import DRILLS
from windbreak.ledger.events import ConfigLoaded
from windbreak.ledger.store import SqliteLedgerStore

if TYPE_CHECKING:
    from pathlib import Path

#: The five drill names the operational runbook documents (issue #59).
_EXPECTED_DRILL_NAMES = frozenset(
    {
        "restore-from-backup",
        "kill-rearm",
        "reconciliation-mismatch",
        "key-rotation",
        "ratchet-sweep",
    }
)


def _build_ctx(tmp_path: Path) -> DrillContext:
    """Build a permissive paper `DrillContext` shared by every registered
    drill: a seeded `ledger.db` (for `restore-from-backup`), obviously-fake
    credentials (for `key-rotation`), and a `HeldPositionsExchange` (for
    `kill-rearm`/`ratchet-sweep`) -- each drill reads only the fields it
    needs and ignores the rest.
    """
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    store = SqliteLedgerStore(fixture_dir / "ledger.db")
    try:
        store.append(ConfigLoaded(component="pipeline", config_hash="hash-1", diff={}))
    finally:
        store.close()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return DrillContext(
        clock=lambda: FIXED_EPOCH_S,
        env={
            "WINDBREAK_APPROVAL_TOKEN_KEY": "0" * 64,
            "WINDBREAK_TRADE_KEY": "fake-key-not-real",
        },
        exchange=HeldPositionsExchange(open_orders=(), positions=()),
        state_dir=state_dir,
        fixture_dir=fixture_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        tmp_dir_factory=make_tmp_dir_factory(tmp_path / "scratch"),
    )


def test_the_registry_has_exactly_the_five_documented_drill_names() -> None:
    """`DRILLS` has exactly the five keys the operational runbook names."""
    assert set(DRILLS) == _EXPECTED_DRILL_NAMES


@pytest.mark.parametrize("name", sorted(_EXPECTED_DRILL_NAMES))
def test_every_registered_drill_passes_against_a_paper_context(
    name: str, tmp_path: Path
) -> None:
    """Every registered drill, run against a paper `DrillContext`, passes and
    reports its own registry name as `DrillResult.drill`.
    """
    ctx = _build_ctx(tmp_path)
    drill = DRILLS[name]()

    result = drill.run(ctx)

    assert result.passed is True
    assert result.drill == name


@pytest.mark.parametrize("name", sorted(_EXPECTED_DRILL_NAMES))
def test_every_registered_drill_ledgers_exactly_one_drill_completed_via_run_drill(
    name: str, tmp_path: Path
) -> None:
    """`run_drill` against every registered drill ledgers exactly one
    `DrillCompleted(passed=True)` into the operational ledger writer.
    """
    ctx = _build_ctx(tmp_path)
    operational_writer = InMemoryDrillLedgerWriter()
    drill = DRILLS[name]()

    result = run_drill(drill, ctx, operational_writer)

    assert result.passed is True
    completed = [
        e for e in operational_writer.events if e.event_type == "DrillCompleted"
    ]
    assert len(completed) == 1
    assert completed[0].payload["drill"] == name
    assert completed[0].payload["passed"] is True
