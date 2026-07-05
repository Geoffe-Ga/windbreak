"""Failing-first tests for hedgekit.riskkernel.checks (issue #29, RED).

Issue #29 gives the Risk Kernel its 24 SPEC S10.3 pre-trade veto checks as a
deliberate *stub*: every check exists, is individually callable, and is wired
into `evaluate_intent`'s fail-closed evaluation loop, but each one vetoes
unconditionally with reason "not implemented" -- no real risk logic ships in
this issue. That stub shape is exactly what this module pins.

`hedgekit/riskkernel/checks.py` does not exist yet, so the import below fails
the whole module at collection with
`ModuleNotFoundError: No module named 'hedgekit.riskkernel.checks'` -- the
expected Gate 1 RED state for issue #29. Once the module exists, this file
pins: the exact 24 SPEC S10.3 check names (in sequence); that every check
individually vetoes with reason "not implemented"; that `evaluate_intent`
runs all of them and is fail-closed (a raising check becomes a veto reason
rather than propagating, and checks after it still run); that `OrderIntent`
is frozen and carries only scaled-int numeric types (never floats); and that
`CheckResult`/`Decision` are themselves frozen result types.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING

import pytest

from hedgekit.numeric.types import (
    ContractCentis,
    MoneyMicros,
    PricePips,
    ProbabilityPpm,
)
from hedgekit.riskkernel.checks import (
    DEFAULT_CHECKS,
    CheckResult,
    OrderIntent,
    evaluate_intent,
)

if TYPE_CHECKING:
    from hedgekit.riskkernel.checks import Check

#: The exact SPEC S10.3 check-name sequence `DEFAULT_CHECKS` must expose, in
#: this exact order.
EXPECTED_CHECK_NAMES: tuple[str, ...] = (
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

#: The number of SPEC S10.3 checks; asserted separately from the name
#: sequence so a length-only mutation (e.g. a dropped duplicate name) cannot
#: hide behind the tuple-equality check alone.
_EXPECTED_CHECK_COUNT = 24

#: Immutable scaled-int defaults for :func:`make_intent`, held as module-level
#: singletons so they are not reconstructed in the function's argument defaults
#: (ruff B008); the wrapper types are frozen, so sharing one instance is safe.
_DEFAULT_PRICE = PricePips(5000)
_DEFAULT_SIZE = ContractCentis(1000)
_DEFAULT_MAX_NOTIONAL = MoneyMicros(50_000_000)
_DEFAULT_IMPLIED_PROBABILITY = ProbabilityPpm(520_000)


def make_intent(
    *,
    intent_id: str = "intent-0001",
    market_ticker: str = "PRES-2028-DEM",
    outcome: str = "yes",
    action: str = "buy",
    price: PricePips = _DEFAULT_PRICE,
    size: ContractCentis = _DEFAULT_SIZE,
    max_notional: MoneyMicros = _DEFAULT_MAX_NOTIONAL,
    implied_probability: ProbabilityPpm = _DEFAULT_IMPLIED_PROBABILITY,
) -> OrderIntent:
    """Build a valid `OrderIntent`, with any field overridable by keyword.

    Args:
        intent_id: The intent's unique identifier.
        market_ticker: The exchange ticker the intent targets.
        outcome: The market outcome the intent trades (e.g. "yes"/"no").
        action: The trade action (e.g. "buy"/"sell").
        price: The limit price, in pips.
        size: The contract count, in centis.
        max_notional: The notional cap, in money-micros.
        implied_probability: The forecast-implied probability, in ppm.

    Returns:
        A fully populated, valid `OrderIntent`.
    """
    return OrderIntent(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        price=price,
        size=size,
        max_notional=max_notional,
        implied_probability=implied_probability,
    )


class _RaisingCheck:
    """A check double that raises instead of returning a `CheckResult`.

    Stands in for a real check whose implementation has a bug, so
    `evaluate_intent`'s fail-closed contract can be exercised without
    depending on any real check's internals.
    """

    name = "raising_check"

    def __call__(self, intent: OrderIntent) -> CheckResult:
        """Raise unconditionally, ignoring `intent`.

        Args:
            intent: Unused; accepted only to match the check-callable shape.

        Raises:
            RuntimeError: Always, with a fixed, recognizable message.
        """
        raise RuntimeError("boom")


class _PassingCheck:
    """A check double that approves (does not veto) the intent.

    No real check passes yet -- every SPEC S10.3 stub vetoes -- so this double
    is the only way to exercise `evaluate_intent`'s non-veto branch: a check
    whose `CheckResult.vetoed` is False must contribute no reason, proving the
    pipeline distinguishes pass from veto rather than hard-coding a veto for
    every check it runs.
    """

    name = "passing_check"

    def __call__(self, intent: OrderIntent) -> CheckResult:
        """Approve unconditionally, ignoring `intent`.

        Args:
            intent: Unused; accepted only to match the check-callable shape.

        Returns:
            A non-vetoing `CheckResult`.
        """
        return CheckResult(vetoed=False, reason="approved")


# --- OrderIntent: frozen, scaled-int-only ---------------------------------------


def test_order_intent_is_frozen() -> None:
    """Mutating any field of a constructed `OrderIntent` raises."""
    intent = make_intent()

    with pytest.raises(FrozenInstanceError):
        intent.action = "sell"  # type: ignore[misc]


def test_order_intent_numeric_fields_are_scaled_int_types_never_floats() -> None:
    """Every money/price/size/probability field is a scaled-int wrapper type
    whose underlying `.value` is a true `int`, never a `float`.
    """
    intent = make_intent()

    assert isinstance(intent.price, PricePips)
    assert isinstance(intent.size, ContractCentis)
    assert isinstance(intent.max_notional, MoneyMicros)
    assert isinstance(intent.implied_probability, ProbabilityPpm)

    for scaled_value in (
        intent.price,
        intent.size,
        intent.max_notional,
        intent.implied_probability,
    ):
        assert type(scaled_value.value) is int
        assert not isinstance(scaled_value.value, bool)


def test_order_intent_string_fields_hold_the_constructed_values() -> None:
    """The four str identity fields round-trip exactly through construction."""
    intent = make_intent(
        intent_id="intent-xyz",
        market_ticker="TICKER-X",
        outcome="no",
        action="sell",
    )

    assert intent.intent_id == "intent-xyz"
    assert intent.market_ticker == "TICKER-X"
    assert intent.outcome == "no"
    assert intent.action == "sell"


def test_order_intent_rejects_a_new_attribute_outside_its_slots() -> None:
    """`OrderIntent` is slots-based: assigning an undeclared attribute raises
    `AttributeError`, not a silent `__dict__` write.
    """
    intent = make_intent()
    forbidden_attribute = "not_a_declared_field"

    with pytest.raises(AttributeError):
        setattr(intent, forbidden_attribute, "nope")


# --- DEFAULT_CHECKS: exact SPEC S10.3 name sequence -----------------------------


def test_default_checks_has_exactly_24_checks() -> None:
    """`DEFAULT_CHECKS` has exactly the SPEC S10.3 count of 24 checks."""
    assert len(DEFAULT_CHECKS) == _EXPECTED_CHECK_COUNT


def test_default_checks_names_match_spec_10_3_exactly_in_order() -> None:
    """`DEFAULT_CHECKS`' `.name`s equal the pinned SPEC S10.3 sequence
    exactly, in order -- catches reordering as well as substitution.
    """
    assert tuple(check.name for check in DEFAULT_CHECKS) == EXPECTED_CHECK_NAMES


def test_default_checks_names_are_the_exact_spec_10_3_set() -> None:
    """`DEFAULT_CHECKS`' `.name`s are exactly the SPEC S10.3 set (no
    duplicates, nothing extra, nothing missing), independent of order.
    """
    names = [check.name for check in DEFAULT_CHECKS]
    assert set(names) == set(EXPECTED_CHECK_NAMES)
    assert len(names) == len(set(names)), "check names must be unique"


@pytest.mark.parametrize("check", DEFAULT_CHECKS, ids=list(EXPECTED_CHECK_NAMES))
def test_each_default_check_vetoes_a_valid_intent_as_not_implemented(
    check: Check,
) -> None:
    """Every default check, called directly with a valid intent, returns a
    veto with reason "not implemented" -- no real risk logic ships yet.
    """
    intent = make_intent()

    result = check(intent)

    assert result.vetoed is True
    assert result.reason == "not implemented"


# --- evaluate_intent: runs all checks, fail-closed ------------------------------


def test_evaluate_intent_vetoes_with_one_not_implemented_reason_per_check() -> None:
    """`evaluate_intent` over `DEFAULT_CHECKS` is vetoed with exactly 24
    "not implemented" reasons, one per check.
    """
    intent = make_intent()

    decision = evaluate_intent(intent)

    assert decision.vetoed is True
    assert decision.reasons == ("not implemented",) * _EXPECTED_CHECK_COUNT


def test_evaluate_intent_fail_closed_converts_a_raised_exception_to_a_veto() -> None:
    """A check that raises is converted to a veto reason -- the exception
    never escapes `evaluate_intent` -- and checks positioned after the
    raising one in the sequence still run (fail-closed, not fail-stop).
    """
    intent = make_intent()
    checks = (*DEFAULT_CHECKS[:2], _RaisingCheck(), *DEFAULT_CHECKS[2:])

    decision = evaluate_intent(intent, checks=checks)

    assert decision.vetoed is True
    assert len(decision.reasons) == len(checks) == _EXPECTED_CHECK_COUNT + 1
    assert decision.reasons[0] == "not implemented"
    assert decision.reasons[1] == "not implemented"
    assert decision.reasons[2] == "raising_check: error: boom"
    assert decision.reasons[3] == "not implemented"
    assert decision.reasons[-1] == "not implemented"


def test_evaluate_intent_omits_reasons_for_checks_that_pass() -> None:
    """A non-vetoing check contributes no reason; only the vetoing checks do.

    Interleaving passing checks among the real (all-vetoing) stubs proves the
    aggregation collects a reason iff a check vetoes -- the branch that a
    veto-everything skeleton, where nothing passes on its own, otherwise never
    exercises.
    """
    intent = make_intent()
    checks = (_PassingCheck(), *DEFAULT_CHECKS[:1], _PassingCheck())

    decision = evaluate_intent(intent, checks=checks)

    assert decision.vetoed is True
    assert decision.reasons == ("not implemented",)


def test_evaluate_intent_with_only_passing_checks_is_not_vetoed() -> None:
    """When every check passes, the aggregate decision does not veto.

    The counterpart to the all-vetoing default: `vetoed` is False and no
    reasons are collected, so `evaluate_intent` cannot be veto-by-default.
    """
    intent = make_intent()

    decision = evaluate_intent(intent, checks=(_PassingCheck(), _PassingCheck()))

    assert decision.vetoed is False
    assert decision.reasons == ()


def test_evaluate_intent_with_an_empty_checks_sequence_is_not_vetoed() -> None:
    """With zero checks, there is nothing to veto on: `vetoed` is False and
    `reasons` is empty -- the boundary case the fail-closed loop must not
    accidentally veto-by-default on.
    """
    intent = make_intent()

    decision = evaluate_intent(intent, checks=())

    assert decision.vetoed is False
    assert decision.reasons == ()


# --- Frozen result types ---------------------------------------------------------


def test_check_result_is_frozen() -> None:
    """`CheckResult` instances reject attribute mutation."""
    result = CheckResult(vetoed=True, reason="not implemented")

    with pytest.raises(FrozenInstanceError):
        result.reason = "changed"  # type: ignore[misc]


def test_decision_is_frozen() -> None:
    """`Decision` instances (as returned by `evaluate_intent`) reject
    attribute mutation.
    """
    decision = evaluate_intent(make_intent())

    with pytest.raises(FrozenInstanceError):
        decision.vetoed = False  # type: ignore[misc]
