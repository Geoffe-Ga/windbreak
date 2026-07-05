"""Pre-trade veto checks for the Risk Kernel (SPEC S10.3).

This module ships the 24 SPEC S10.3 pre-trade checks as a deliberate *stub*:
every check exists, is individually callable with an :class:`OrderIntent`, and
is wired into :func:`evaluate_intent`'s fail-closed loop, but each one vetoes
unconditionally with reason ``"not implemented"`` -- no real risk logic lands
in this issue. The checks are table-driven from a single name sequence and a
shared stub callable, so adding real logic later means replacing one factory,
not editing 24 near-identical bodies.

:func:`evaluate_intent` runs every check fail-closed: a check that *raises* is
converted into a veto reason (``"{name}: error: {exc}"``) rather than
propagating, and the checks after it still run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hedgekit.numeric.types import (
        ContractCentis,
        MoneyMicros,
        PricePips,
        ProbabilityPpm,
    )

#: The reason every stub check vetoes with until real logic ships.
NOT_IMPLEMENTED_REASON = "not implemented"


@dataclass(frozen=True)
class OrderIntent:
    """A normalized order intent submitted to the Risk Kernel for veto review.

    The dataclass is ``frozen`` (immutable): assigning to any attribute -- a
    declared field or an undeclared name -- raises
    :class:`dataclasses.FrozenInstanceError`, itself an :class:`AttributeError`
    subclass, so no attribute can ever be added or mutated after construction.
    ``slots`` is deliberately not enabled here: on CPython, combining
    ``frozen`` with ``slots`` routes an undeclared-attribute assignment through
    a stale ``super()`` cell (CPython issue #91126) that raises ``TypeError``
    instead of the immutability error, so plain ``frozen`` gives the stronger,
    correct rejection.

    Every numeric field is a :mod:`hedgekit.numeric` scaled-integer type, never
    a float (SPEC S6.1); the identity fields are plain strings.

    Attributes:
        intent_id: The intent's unique identifier.
        market_ticker: The exchange ticker the intent targets.
        outcome: The market outcome the intent trades (e.g. ``"yes"``).
        action: The trade action (e.g. ``"buy"``).
        price: The limit price, in pips.
        size: The contract count, in centis.
        max_notional: The notional cap, in money-micros.
        implied_probability: The forecast-implied probability, in ppm.
    """

    intent_id: str
    market_ticker: str
    outcome: str
    action: str
    price: PricePips
    size: ContractCentis
    max_notional: MoneyMicros
    implied_probability: ProbabilityPpm


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of a single pre-trade check.

    Attributes:
        vetoed: Whether the check vetoes the intent.
        reason: A short human-readable reason for the verdict.
    """

    vetoed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class Decision:
    """The Risk Kernel's aggregate verdict over an intent.

    Attributes:
        vetoed: Whether any check vetoed the intent.
        reasons: One reason per vetoing (or raising) check, in evaluation
            order.
        ledgered: Whether this decision has been recorded to the ledger. Pure
            :func:`evaluate_intent` leaves this ``False``; the process-level
            kernel sets it ``True`` once the veto event is persisted.
    """

    vetoed: bool
    reasons: tuple[str, ...]
    ledgered: bool = False


class Check(Protocol):
    """A pre-trade check: a named callable returning a :class:`CheckResult`.

    ``name`` is a read-only property so both a frozen-dataclass field (like
    :class:`_NotImplementedCheck`) and a plain class attribute satisfy the
    protocol; a bare ``name: str`` would demand a *settable* attribute that a
    frozen check cannot provide.
    """

    @property
    def name(self) -> str:
        """The SPEC S10.3 check name."""

    def __call__(self, intent: OrderIntent) -> CheckResult:
        """Evaluate ``intent`` and return this check's verdict.

        Args:
            intent: The order intent to evaluate.

        Returns:
            The check's :class:`CheckResult`.
        """
        ...


@dataclass(frozen=True, slots=True)
class _NotImplementedCheck:
    """A stub check that vetoes every intent as ``"not implemented"``.

    Attributes:
        name: The SPEC S10.3 check name this stub stands in for.
    """

    name: str

    def __call__(self, intent: OrderIntent) -> CheckResult:
        """Veto ``intent`` unconditionally; no real logic ships yet.

        Args:
            intent: The order intent, unused by the stub.

        Returns:
            A veto :class:`CheckResult` with the not-implemented reason.
        """
        del intent  # No real risk logic in this issue; the veto is constant.
        return CheckResult(vetoed=True, reason=NOT_IMPLEMENTED_REASON)


#: The SPEC S10.3 check names, in the exact order they must be evaluated.
_SPEC_10_3_CHECK_NAMES: tuple[str, ...] = (
    "instrument_whitelist",
    "jurisdiction_product_eligibility",
    "mode_permission_ceiling",
    "floor_invariant",
    "balance_reconciliation",
    "position_reconciliation",
    "open_order_reconciliation",
    "fee_upper_bound_present",
    "settlement_fee_upper_bound",
    "concentration_limits",
    "daily_loss_limit",
    "trailing_drawdown_limit",
    "velocity_limits",
    "quote_freshness",
    "forecast_freshness",
    "price_band_compliance",
    "participation_cap_compliance",
    "human_ack_satisfied",
    "approval_token_uniqueness",
    "idempotency_key_uniqueness",
    "clock_skew_limit",
    "exchange_status_ok",
    "pipeline_heartbeat_ok",
    "reduce_only_provable",
)

#: The default pre-trade check sequence, one stub per SPEC S10.3 name in order.
DEFAULT_CHECKS: tuple[Check, ...] = tuple(
    _NotImplementedCheck(name) for name in _SPEC_10_3_CHECK_NAMES
)


def _run_check(check: Check, intent: OrderIntent) -> str | None:
    """Run one check fail-closed, returning its veto reason or ``None``.

    A check that raises is converted into a veto reason rather than
    propagating, so one buggy check can never let an intent through or abort
    the whole evaluation.

    Args:
        check: The check to run.
        intent: The order intent to evaluate.

    Returns:
        The veto reason string if the check vetoes or raises, else ``None``.
    """
    try:
        result = check(intent)
    except Exception as exc:  # Fail-closed: a raising check becomes a veto.
        return f"{check.name}: error: {exc}"
    return result.reason if result.vetoed else None


def evaluate_intent(
    intent: OrderIntent, checks: tuple[Check, ...] = DEFAULT_CHECKS
) -> Decision:
    """Evaluate an intent against every check, fail-closed.

    Args:
        intent: The order intent to evaluate.
        checks: The checks to run, in order. Defaults to :data:`DEFAULT_CHECKS`.

    Returns:
        A :class:`Decision` carrying one reason per vetoing (or raising) check,
        in evaluation order; ``vetoed`` is True iff any reason was collected.
    """
    reasons: list[str] = []
    for check in checks:
        reason = _run_check(check, intent)
        if reason is not None:
            reasons.append(reason)
    return Decision(vetoed=bool(reasons), reasons=tuple(reasons))
