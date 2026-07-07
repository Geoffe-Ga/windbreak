"""Process B: the Risk Kernel veto authority (SPEC S5.1-S5.3).

The Risk Kernel is the sole holder of the approval-token **signing** key and
runs with read-only exchange credentials (SPEC S5.2). It validates normalized
order intents, reserves capital, and signs single-use approval tokens. Per the
SPEC S5.3 import boundary, only this package may import the approval-token
signing key handle; a future import-linter check will enforce that rule in CI.

This package root re-exports the mode machine (:mod:`~hedgekit.riskkernel.modes`),
the pre-trade checks (:mod:`~hedgekit.riskkernel.checks`), the process skeleton
(:mod:`~hedgekit.riskkernel.process`), the capital-reservation ledger and
approval pipeline (:mod:`~hedgekit.riskkernel.reservations`), and the approval-
token issuer (:mod:`~hedgekit.riskkernel.tokens`). It deliberately does **not**
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
from hedgekit.riskkernel.demotion import (
    TRIGGER_ACTIONS,
    DemotionAction,
    DemotionTrigger,
    resolve_demotion,
)
from hedgekit.riskkernel.floor import worst_case_cost, worst_case_equity
from hedgekit.riskkernel.governance import (
    DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS,
    ChangeOrigin,
    CoolOffActiveError,
    FloorGovernance,
    ForbiddenOriginError,
    LoweringAlreadyPendingError,
    NonceMismatchError,
    NoPendingLowerError,
    PendingFloorLower,
)
from hedgekit.riskkernel.human_ack import (
    DEFAULT_HUMAN_ACK_TTL_SECONDS,
    AckLapsedError,
    DuplicateAckRequestError,
    HumanAckQueue,
    PendingHumanAck,
    Releaser,
    UnknownApprovalError,
)
from hedgekit.riskkernel.kill import (
    DashboardChallengeError,
    DashboardKillStub,
    DirectiveSink,
    KillFileWatcher,
    KillIntegration,
    KillSwitch,
    KillTrigger,
    ReconciliationMismatchMonitor,
)
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
from hedgekit.riskkernel.promotion import (
    SIGNIFICANCE_OVERRIDE_ACK_PHRASE,
    Comparison,
    CriterionResult,
    GateCriterion,
    GateEvidence,
    OverrideAcknowledgementError,
    PromotionDecision,
    PromotionGate,
    build_promotion_gates,
    effective_mode_ceiling,
    evaluate_promotion,
    override_applied_in,
)
from hedgekit.riskkernel.reservations import (
    ApprovalOutcome,
    ApprovalPipeline,
    DuplicateReservationError,
    Reservation,
    ReservationLedger,
)
from hedgekit.riskkernel.tokens import DEFAULT_TOKEN_TTL_SECONDS, TokenIssuer

__all__ = [
    "DEFAULT_CHECKS",
    "DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS",
    "DEFAULT_HUMAN_ACK_TTL_SECONDS",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "REARM_CONFIRMATION_PHRASE",
    "SIGNIFICANCE_OVERRIDE_ACK_PHRASE",
    "TRIGGER_ACTIONS",
    "AccountState",
    "AckLapsedError",
    "ApprovalOutcome",
    "ApprovalPipeline",
    "ChangeOrigin",
    "Check",
    "CheckResult",
    "Comparison",
    "CoolOffActiveError",
    "CriterionResult",
    "DashboardChallengeError",
    "DashboardKillStub",
    "Decision",
    "DemotionAction",
    "DemotionTrigger",
    "DirectiveSink",
    "DuplicateAckRequestError",
    "DuplicateReservationError",
    "EvaluationContext",
    "FeeBounds",
    "FloorGovernance",
    "ForbiddenOriginError",
    "GateCriterion",
    "GateEvidence",
    "HumanAckQueue",
    "IllegalModeTransitionError",
    "InMemoryKernelLedgerWriter",
    "KernelLedgerWriter",
    "KillFileWatcher",
    "KillIntegration",
    "KillReArmError",
    "KillSwitch",
    "KillTrigger",
    "LoggingKernelLedgerWriter",
    "LoweringAlreadyPendingError",
    "MarketView",
    "Mode",
    "ModeCeilingExceededError",
    "ModeStateMachine",
    "NoPendingLowerError",
    "NonceMismatchError",
    "OrderIntent",
    "OverrideAcknowledgementError",
    "PendingFloorLower",
    "PendingHumanAck",
    "PromotionDecision",
    "PromotionGate",
    "ReconciliationMismatchMonitor",
    "Releaser",
    "Reservation",
    "ReservationLedger",
    "RiskKernel",
    "RiskLimits",
    "TokenIssuer",
    "UnknownApprovalError",
    "build_promotion_gates",
    "effective_mode_ceiling",
    "evaluate_intent",
    "evaluate_promotion",
    "override_applied_in",
    "resolve_demotion",
    "worst_case_cost",
    "worst_case_equity",
]
