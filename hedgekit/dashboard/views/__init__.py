"""Pure read-model renderers for the PAPER-loop dashboard views (issue #48).

Each renderer is a pure function over the read-model row shape
:mod:`hedgekit.ledger.rebuild` already produces for every projection
(``{seq, created_at, event_type, data}``), so the dashboard reuses the same
projections ``hedgekit rebuild`` writes rather than re-deriving its own view of
the ledger. Every ledger-derived string is ``html.escape``d before it reaches
HTML output -- selector/veto reasons are forecast/LLM-adjacent and therefore an
XSS surface -- mirroring :mod:`hedgekit.dashboard.app`'s own ``html.escape``
treatment of ``mode``/``last_heartbeat``.

:class:`DashboardReadModels` bundles the three projections a live view needs.
:func:`build_ledger_read_models_source` adapts a ledger database path into the
zero-arg source callable :func:`hedgekit.dashboard.app.create_server` accepts, so
the dashboard renders live ledger truth without the renderers ever touching the
store themselves.
"""

from __future__ import annotations

from hedgekit.dashboard.views.decisions import render_decisions
from hedgekit.dashboard.views.equity import render_equity_vs_floor
from hedgekit.dashboard.views.models import (
    DashboardReadModels,
    build_ledger_read_models_source,
)
from hedgekit.dashboard.views.positions import render_positions

__all__ = [
    "DashboardReadModels",
    "build_ledger_read_models_source",
    "render_decisions",
    "render_equity_vs_floor",
    "render_positions",
]
