"""The operational-drill registry (issue #59).

:data:`DRILLS` maps each of the five documented drill names the operational
runbook names to a zero-argument factory building that drill. It is the single
source of truth the ``windbreak drill <name>`` CLI verb and the
"CI runs every drill" guarantee both read, so a new drill is registered here
exactly once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from windbreak.drills.catalog import DRILL_NAMES
from windbreak.drills.key_rotation import KeyRotationDrill
from windbreak.drills.kill_rearm import KillRearmDrill
from windbreak.drills.ratchet_sweep import RatchetSweepDrill
from windbreak.drills.reconciliation_mismatch import ReconciliationMismatchDrill
from windbreak.drills.restore_from_backup import RestoreFromBackupDrill

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from windbreak.drills.framework import Drill

#: Drill factories, positionally aligned with :data:`DRILL_NAMES`.
_DRILL_FACTORIES: tuple[Callable[[], Drill], ...] = (
    RestoreFromBackupDrill,
    KillRearmDrill,
    ReconciliationMismatchDrill,
    KeyRotationDrill,
    RatchetSweepDrill,
)

#: The five documented operational drills, keyed by their runbook names. Derived
#: from the single-source :data:`DRILL_NAMES` so the CLI ``choices`` and this
#: registry can never drift; ``strict=True`` guards the name/factory alignment.
DRILLS: Mapping[str, Callable[[], Drill]] = dict(
    zip(DRILL_NAMES, _DRILL_FACTORIES, strict=True)
)
