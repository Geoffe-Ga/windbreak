"""The dashboard read-model bundle and its ledger-backed source factory (issue #48).

:class:`DashboardReadModels` is the immutable "current view" the three PAPER-loop
routes render: the latest positions, the equity curve, and the interleaved
selector/intent decisions, each a list of the read-model rows
:mod:`windbreak.ledger.rebuild` produces. An empty bundle is the documented
"no data yet" input. :func:`build_ledger_read_models_source` folds a verified
ledger database into a zero-arg source callable, reusing the very projection
functions ``windbreak rebuild`` writes so the dashboard never re-derives its own
view of the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: One read-model row: the ``{seq, created_at, event_type, data}`` shape every
#: :mod:`windbreak.ledger.rebuild` projection emits.
ReadModelRow = dict[str, object]


@dataclass(frozen=True)
class DashboardReadModels:
    """The immutable read-model bundle the three PAPER-loop views render.

    Attributes:
        positions: The latest positions-snapshot rows (at most one).
        equity_curve: Every equity-sample row, in ledger order.
        decisions: The interleaved selector/intent decision rows, in ledger
            order.
    """

    positions: list[ReadModelRow]
    equity_curve: list[ReadModelRow]
    decisions: list[ReadModelRow]


def build_ledger_read_models_source(
    ledger_path: Path,
) -> Callable[[], DashboardReadModels]:
    """Build a zero-arg source folding a verified ledger into read models.

    The returned callable opens the ledger fresh on every invocation, verifies
    its hash chain, and folds it into a :class:`DashboardReadModels` via the same
    projection functions ``windbreak rebuild`` writes -- so every request reflects
    live ledger truth and a corrupt ledger fails closed
    (:class:`~windbreak.ledger.store.ChainIntegrityError`) rather than rendering a
    plausible-but-wrong view.

    Args:
        ledger_path: Path to the SQLite ledger database to project.

    Returns:
        A callable suitable for
        :func:`windbreak.dashboard.app.create_server`'s ``read_models_source``.
    """
    from windbreak.ledger.rebuild import (
        equity_curve_read_model,
        positions_read_model,
        selector_decisions_read_model,
    )
    from windbreak.ledger.store import SqliteLedgerStore

    def _source() -> DashboardReadModels:
        """Fold the ledger into a fresh read-model bundle."""
        store = SqliteLedgerStore(ledger_path)
        try:
            store.verify_chain()
            records = store.read_all()
        finally:
            store.close()
        return DashboardReadModels(
            positions=positions_read_model(records),
            equity_curve=equity_curve_read_model(records),
            decisions=selector_decisions_read_model(records),
        )

    return _source
