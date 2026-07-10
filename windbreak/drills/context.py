"""The drill context and its paper/production bindings (issue #59).

:class:`DrillContext` is the single injected value every drill runs against: a
clock, an environment mapping, an exchange adapter, the state/fixture
directories, the operational ledger writer, and a scratch-directory factory.
Nothing inside a drill ever reads :func:`time.time` or :data:`os.environ` or
dials the network -- the CLI layer reads the wall clock and the process
environment and injects them here, so every drill is deterministic and
replayable.

:func:`bind_paper_context` builds the deterministic CI default. The manual-only
:func:`bind_production_context` rebinds **only** the exchange adapter relative to
a paper binding, and fails closed (:class:`ProductionCredentialsMissingError`)
when the injected environment carries no exchange credentials -- it never falls
back to silently reusing the paper exchange or reading the real process
environment.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from windbreak.drills.exchanges import HeldPositionsExchange

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from windbreak.drills.framework import DrillLedgerWriter

#: The environment variable whose presence signals real exchange credentials are
#: configured -- the same variable
#: :class:`windbreak.preflight.checks.EnvTradeKeyLeakProber` inspects, reused
#: here as the fail-closed gate on the manual ``--production`` binding.
_PRODUCTION_CREDENTIAL_VAR = "WINDBREAK_TRADE_KEY"

#: Subdirectory of ``state_dir`` a paper binding roots its scratch factory under.
_SCRATCH_DIRNAME = "drill-scratch"


class ProductionCredentialsMissingError(Exception):
    """Raised when ``--production`` is requested without exchange credentials.

    Fail closed: a production binding never silently falls back to the paper
    exchange when the injected environment carries no real exchange credentials.
    """


@dataclass(frozen=True)
class DrillContext:
    """The injected value a drill runs against (issue #59).

    Attributes:
        clock: A zero-argument callable returning the current epoch second.
        env: The injected environment mapping (never the real process env).
        exchange: The exchange adapter drills exercise, or ``None`` for drills
            that do not touch an exchange.
        state_dir: The directory drills read/write on-disk protocol files in.
        fixture_dir: The directory drills read fixtures (e.g. a backup ledger)
            from.
        ledger_writer: The operational ledger writer.
        tmp_dir_factory: A factory returning a fresh scratch directory per call.
    """

    clock: Callable[[], int]
    env: Mapping[str, str]
    exchange: HeldPositionsExchange | None
    state_dir: Path
    fixture_dir: Path
    ledger_writer: DrillLedgerWriter
    tmp_dir_factory: Callable[[], Path]


def _scratch_factory(base: Path) -> Callable[[], Path]:
    """Build a factory minting a fresh, previously-unused directory per call.

    Args:
        base: The parent directory each scratch directory is created under;
            created if absent.

    Returns:
        A zero-argument callable returning a fresh, existing directory each call.
    """
    base.mkdir(parents=True, exist_ok=True)
    counter = itertools.count()

    def _factory() -> Path:
        """Return a freshly created, previously-unused scratch directory."""
        path = base / f"tmp-{next(counter)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    return _factory


def bind_paper_context(
    *,
    fixture_dir: Path,
    state_dir: Path,
    ledger_writer: DrillLedgerWriter,
    clock: Callable[[], int],
    env: Mapping[str, str],
) -> DrillContext:
    """Build the deterministic paper :class:`DrillContext` (the CI default).

    Every field derives from the injected inputs -- the clock and environment
    are used verbatim, never read from :func:`time.time` or :data:`os.environ`
    -- so two builds over the same inputs agree on every deterministic field.
    The exchange is a fresh, empty :class:`HeldPositionsExchange`, and the
    scratch factory is rooted deterministically under ``state_dir``.

    Args:
        fixture_dir: The directory drills read fixtures from.
        state_dir: The directory drills use for on-disk protocol/scratch files.
        ledger_writer: The operational ledger writer.
        clock: The injected epoch-second clock.
        env: The injected environment mapping.

    Returns:
        The deterministic paper :class:`DrillContext`.
    """
    return DrillContext(
        clock=clock,
        env=dict(env),
        exchange=HeldPositionsExchange(open_orders=(), positions=()),
        state_dir=state_dir,
        fixture_dir=fixture_dir,
        ledger_writer=ledger_writer,
        tmp_dir_factory=_scratch_factory(state_dir / _SCRATCH_DIRNAME),
    )


def bind_production_context(
    paper: DrillContext, *, env: Mapping[str, str]
) -> DrillContext:
    """Rebind *only* the exchange adapter for a manual ``--production`` run.

    Fails closed: unless ``env`` carries a *non-empty* exchange credential
    variable, this raises rather than falling back to the paper exchange -- an
    exported-but-blank ``WINDBREAK_TRADE_KEY=""`` is treated as absent
    (fail-closed on ambiguity). On success it changes only ``.exchange`` --
    every other field (state/fixture dirs, ledger writer, clock, env) carries
    over from ``paper`` unchanged.

    No live exchange adapter exists in the repo yet, so the rebound exchange is
    a fresh, empty :class:`HeldPositionsExchange` stub; the credential gate is
    the fail-closed guard that will front the real adapter once it lands. The
    ``--production`` verb is therefore manual-only and never exercised in CI.

    Args:
        paper: The paper binding to rebind the exchange relative to.
        env: The injected environment mapping inspected for credentials.

    Returns:
        A :class:`DrillContext` identical to ``paper`` but for a fresh
        production exchange adapter (a stub until a live adapter lands).

    Raises:
        ProductionCredentialsMissingError: If ``env`` carries no non-empty
            exchange credential.
    """
    if not env.get(_PRODUCTION_CREDENTIAL_VAR, "").strip():
        raise ProductionCredentialsMissingError(
            "production drill requires a non-empty exchange credential in the "
            f"environment ({_PRODUCTION_CREDENTIAL_VAR}); refusing to fall back "
            "to paper"
        )
    return replace(paper, exchange=HeldPositionsExchange(open_orders=(), positions=()))
