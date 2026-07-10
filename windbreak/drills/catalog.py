"""The canonical operational-drill name catalog, kept import-light (issue #59).

This module holds *only* the tuple of drill names -- no drill classes -- so the
``windbreak drill`` CLI verb can offer them as argparse ``choices`` without
importing the heavy Risk Kernel / Order Gateway / preflight seams the concrete
drill modules pull in. :mod:`windbreak.drills.registry` pairs these same names
with their factories.
"""

from __future__ import annotations

#: The five documented operational-drill names, in runbook order. The single
#: source of truth both the registry and the CLI ``choices`` derive from.
DRILL_NAMES: tuple[str, ...] = (
    "restore-from-backup",
    "kill-rearm",
    "reconciliation-mismatch",
    "key-rotation",
    "ratchet-sweep",
)
