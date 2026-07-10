"""Failing-first tests for windbreak.drills.context (issue #59, RED).

`windbreak.drills.context` does not exist yet, so the import below fails
collection with `ModuleNotFoundError: No module named
'windbreak.drills.context'` -- the expected Gate 1 RED state for issue #59.

Pins: `bind_paper_context` builds a deterministic `DrillContext` from an
injected clock/env (never `time.time`/`os.environ` read internally);
`bind_production_context` rebinds *only* the exchange relative to the paper
binding it is built from, and fails closed (raises
`ProductionCredentialsMissingError`) when exchange credentials are absent
from the injected `env` mapping -- it must never fall back to silently
reusing the paper exchange or reading the real process environment.

Design assumption (flagged for the implementer): `bind_production_context`
takes the already-built paper `DrillContext` plus the injected `env` mapping,
and raises unless `env` carries `WINDBREAK_TRADE_KEY` -- the same variable
`windbreak.preflight.checks.EnvTradeKeyLeakProber` inspects for a leak,
reused here as evidence real exchange credentials are configured. If the
real seam differs, only this file's call sites need updating; the
fails-closed-without-credentials and changes-only-the-exchange assertions
below should still hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.drills.conftest import (
    FAKE_CRED_ENV,
    FIXED_EPOCH_S,
    InMemoryDrillLedgerWriter,
)
from windbreak.drills.context import (
    DrillContext,
    ProductionCredentialsMissingError,
    bind_paper_context,
    bind_production_context,
)

if TYPE_CHECKING:
    from pathlib import Path


def _build_paper_context(tmp_path: Path) -> DrillContext:
    """Build a deterministic paper `DrillContext` rooted at `tmp_path`."""
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir(exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return bind_paper_context(
        fixture_dir=fixture_dir,
        state_dir=state_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        clock=lambda: FIXED_EPOCH_S,
        env={},
    )


# --- bind_paper_context: deterministic, never touches the real environment ----


def test_bind_paper_context_uses_the_injected_clock_verbatim(tmp_path: Path) -> None:
    """The paper binding's `.clock()` returns exactly the injected clock's
    value -- never real wall-clock time.
    """
    ctx = _build_paper_context(tmp_path)

    assert ctx.clock() == FIXED_EPOCH_S


def test_bind_paper_context_is_deterministic_across_two_independent_builds(
    tmp_path: Path,
) -> None:
    """Two independently built paper contexts over the same inputs agree on
    every deterministic field (clock, env, state_dir, fixture_dir).
    """
    first = _build_paper_context(tmp_path)
    second = _build_paper_context(tmp_path)

    assert first.clock() == second.clock()
    assert first.env == second.env
    assert first.state_dir == second.state_dir
    assert first.fixture_dir == second.fixture_dir


def test_bind_paper_context_never_reads_the_real_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with a real, populated `os.environ`, the paper binding's `.env`
    reflects only the explicitly injected mapping.
    """
    monkeypatch.setenv("WINDBREAK_TRADE_KEY", "should-never-leak-in")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir(exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)

    ctx = bind_paper_context(
        fixture_dir=fixture_dir,
        state_dir=state_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        clock=lambda: FIXED_EPOCH_S,
        env={},
    )

    assert "WINDBREAK_TRADE_KEY" not in ctx.env


def test_bind_paper_context_carries_over_fixture_dir_and_state_dir_verbatim(
    tmp_path: Path,
) -> None:
    """The paper binding's `.fixture_dir`/`.state_dir` are exactly the paths
    passed in, unmodified.
    """
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir(exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)

    ctx = bind_paper_context(
        fixture_dir=fixture_dir,
        state_dir=state_dir,
        ledger_writer=InMemoryDrillLedgerWriter(),
        clock=lambda: FIXED_EPOCH_S,
        env={},
    )

    assert ctx.fixture_dir == fixture_dir
    assert ctx.state_dir == state_dir


# --- bind_production_context: fails closed without credentials ----------------


def test_bind_production_context_without_credentials_raises(tmp_path: Path) -> None:
    """`bind_production_context` raises `ProductionCredentialsMissingError`
    when the injected `env` carries no exchange credentials -- fail closed,
    never a silent fallback to the paper exchange.
    """
    paper_ctx = _build_paper_context(tmp_path)

    with pytest.raises(ProductionCredentialsMissingError):
        bind_production_context(paper_ctx, env={})


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_bind_production_context_rejects_blank_credentials(
    tmp_path: Path, blank: str
) -> None:
    """An exported-but-blank credential is treated as absent (fail closed).

    A ``WINDBREAK_TRADE_KEY`` present in the environment but empty or
    whitespace-only must not satisfy the gate -- ambiguity fails closed rather
    than proceeding to a live venue with no real credential.
    """
    paper_ctx = _build_paper_context(tmp_path)

    with pytest.raises(ProductionCredentialsMissingError):
        bind_production_context(paper_ctx, env={"WINDBREAK_TRADE_KEY": blank})


def test_production_binding_changes_only_the_exchange_relative_to_paper(
    tmp_path: Path,
) -> None:
    """With credentials present, `--production`'s rebinding changes only
    `.exchange`: `.state_dir`, `.fixture_dir`, and `.ledger_writer` are
    carried over from the paper binding unchanged.
    """
    paper_ctx = _build_paper_context(tmp_path)

    production_ctx = bind_production_context(paper_ctx, env=FAKE_CRED_ENV)

    assert production_ctx.exchange is not paper_ctx.exchange
    assert production_ctx.state_dir == paper_ctx.state_dir
    assert production_ctx.fixture_dir == paper_ctx.fixture_dir
    assert production_ctx.ledger_writer is paper_ctx.ledger_writer


# --- tmp_dir_factory: fresh, existing, unique directories per call -------------


def test_paper_context_tmp_dir_factory_mints_fresh_existing_unique_dirs(
    tmp_path: Path,
) -> None:
    """`ctx.tmp_dir_factory()` returns a freshly created, previously-unused
    directory on every call: never the same path twice, and always already
    existing on disk by the time the caller receives it.
    """
    ctx = _build_paper_context(tmp_path)

    first = ctx.tmp_dir_factory()
    second = ctx.tmp_dir_factory()

    assert first != second
    assert first.is_dir()
    assert second.is_dir()
