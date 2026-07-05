"""Process B: the Risk Kernel veto authority (SPEC S5.1-S5.3).

The Risk Kernel is the sole holder of the approval-token **signing** key and
runs with read-only exchange credentials (SPEC S5.2). It validates normalized
order intents, reserves capital, and signs single-use approval tokens. Per the
SPEC S5.3 import boundary, only this package may import the approval-token
signing key handle; a future import-linter check will enforce that rule in CI.

This package root re-exports the mode machine (:mod:`~hedgekit.riskkernel.modes`),
the pre-trade checks (:mod:`~hedgekit.riskkernel.checks`), and the process
skeleton (:mod:`~hedgekit.riskkernel.process`). It deliberately does **not**
re-export :mod:`hedgekit.riskkernel.signing`: the signing key handle is reachable
only via its fully qualified module path, preserving the SPEC S5.3 boundary.
"""

from hedgekit.riskkernel.checks import (
    DEFAULT_CHECKS,
    Check,
    CheckResult,
    Decision,
    OrderIntent,
    evaluate_intent,
)
from hedgekit.riskkernel.context import (
    AccountState,
    EvaluationContext,
    FeeBounds,
    MarketView,
    RiskLimits,
)
from hedgekit.riskkernel.floor import worst_case_cost, worst_case_equity
from hedgekit.riskkernel.modes import (
    REARM_CONFIRMATION_PHRASE,
    IllegalModeTransitionError,
    KillReArmError,
    Mode,
    ModeCeilingExceededError,
    ModeStateMachine,
)
from hedgekit.riskkernel.process import (
    InMemoryKernelLedgerWriter,
    KernelLedgerWriter,
    LoggingKernelLedgerWriter,
    RiskKernel,
)

__all__ = [
    "DEFAULT_CHECKS",
    "REARM_CONFIRMATION_PHRASE",
    "AccountState",
    "Check",
    "CheckResult",
    "Decision",
    "EvaluationContext",
    "FeeBounds",
    "IllegalModeTransitionError",
    "InMemoryKernelLedgerWriter",
    "KernelLedgerWriter",
    "KillReArmError",
    "LoggingKernelLedgerWriter",
    "MarketView",
    "Mode",
    "ModeCeilingExceededError",
    "ModeStateMachine",
    "OrderIntent",
    "RiskKernel",
    "RiskLimits",
    "evaluate_intent",
    "worst_case_cost",
    "worst_case_equity",
]
