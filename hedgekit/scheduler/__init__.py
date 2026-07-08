"""The always-on PAPER-mode composition root (Process orchestration, issue #48).

:mod:`hedgekit.scheduler.loop` is the single place the real, unmodified Market
Connector, Forecast Engine, Trade Selector, Risk Kernel, Order Gateway, and
Reconciler are wired together into one PAPER tick following the SPEC S5.3 SINGLE
order path (snapshot -> forecast -> select -> approve -> route -> fill ->
reconcile), appending an audit event to the hash-chained ledger at every stage.

This package is the *only* legitimate importer of
:mod:`hedgekit.connector.paper` outside the Order Gateway: it constructs a
`PaperExchange` in its `build_paper_deps` PAPER factory. The RESEARCH loop never
imports this package (``hedgekit.main`` wires the PAPER tick via a local import
only when PAPER is actually activated), so the paper fake stays off the
RESEARCH/LIVE trading path.
"""

from __future__ import annotations

from hedgekit.riskkernel.reservations import ApprovalOutcome
from hedgekit.scheduler.loop import (
    ApprovalSeam,
    KernelApproval,
    PaperTickDeps,
    TickOutcome,
    build_evaluation_context,
    build_paper_deps,
    compute_equity_micros,
    is_quote_fresh,
    market_snapshot_event_to_record,
    run_single_tick,
)

__all__ = [
    "ApprovalOutcome",
    "ApprovalSeam",
    "KernelApproval",
    "PaperTickDeps",
    "TickOutcome",
    "build_evaluation_context",
    "build_paper_deps",
    "compute_equity_micros",
    "is_quote_fresh",
    "market_snapshot_event_to_record",
    "run_single_tick",
]
