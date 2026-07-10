"""Operational drills: rehearse the safety mechanisms on demand (issue #59).

A drill *exercises* an already-shipped safety mechanism -- restore-from-backup,
kill/re-arm, reconciliation-mismatch, key-rotation, ratchet-sweep -- end to end
against an injected :class:`~windbreak.drills.context.DrillContext`, grades the
outcome, and appends exactly one
:class:`~windbreak.ledger.events.DrillCompleted` to the operational ledger. The
``windbreak drill <name>`` CLI verb makes running each one routine, so CI can run
the whole suite on every change.

Drills are **not** a privileged side door. Every drill routes through the real
Risk Kernel / Order Gateway / floor-governance / ledger seams: it never bypasses
floor checks, approval-token verification, or the hash-chained ledger, and it
adds no new kill/restore/ratchet/reconcile logic of its own -- only orchestrates
the shipped mechanisms.

The ``--production`` flag is **manual-only** and rebinds **only the exchange
adapter** (:func:`~windbreak.drills.context.bind_production_context`), and even
then only when real exchange credentials are present in the environment; it fails
closed otherwise and never falls back to the paper exchange. Every other seam --
the ledger, the floor governance, the kill switch, the token verification -- is
identical to the deterministic paper run.

Only the light framework/context/exchange primitives are re-exported here; the
:data:`~windbreak.drills.registry.DRILLS` registry and the concrete drill
modules pull in the heavy Risk Kernel / Order Gateway / preflight seams, so they
are imported directly from :mod:`windbreak.drills.registry` on demand (e.g. by
the ``windbreak drill`` CLI verb) rather than at package import time -- keeping
the always-on RESEARCH heartbeat path, which imports ``windbreak.main`` (and thus
this package via the context binding), free of them.
"""

from __future__ import annotations

from windbreak.drills.context import (
    DrillContext,
    ProductionCredentialsMissingError,
    bind_paper_context,
    bind_production_context,
)
from windbreak.drills.exchanges import HeldPositionsExchange
from windbreak.drills.framework import (
    Drill,
    DrillEvidenceError,
    DrillFailedError,
    DrillLedgerWriter,
    DrillPreconditionError,
    DrillResult,
    run_drill,
)

__all__ = [
    "Drill",
    "DrillContext",
    "DrillEvidenceError",
    "DrillFailedError",
    "DrillLedgerWriter",
    "DrillPreconditionError",
    "DrillResult",
    "HeldPositionsExchange",
    "ProductionCredentialsMissingError",
    "bind_paper_context",
    "bind_production_context",
    "run_drill",
]
