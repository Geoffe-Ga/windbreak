"""Failing-first tests for hedgekit.riskkernel.checks (issues #30 and #31, RED).

Issue #30 gave 15 of the 24 SPEC S10.3 pre-trade checks their real logic
(instrument whitelist, mode/ceiling, the floor invariant, fee-bound presence,
concentration, daily loss, trailing drawdown, velocity, quote/forecast
freshness, price band, participation cap, clock skew, and reduce-only
provability), each reading a full `EvaluationContext` rather than the
`OrderIntent` alone. Issue #31 promotes two more from stub to real logic --
`approval_token_uniqueness` and `idempotency_key_uniqueness`, each reading a
new `EvaluationContext.used_intent_ids` / `.used_idempotency_keys` set -- so
17 of the 24 SPEC S10.3 checks are now real; the remaining 7 stay deliberate
stubs that still veto, each naming the GitHub issue that will replace it.

`hedgekit/riskkernel/context.py` does not yet declare `used_intent_ids` /
`used_idempotency_keys` (this file's `conftest` import alone triggers the
collection failure), so importing anything here fails collection with
`ModuleNotFoundError`/`TypeError` -- the expected Gate 1 RED state for issue
#31. Once `context.py` and the two real checks exist, this file pins: the
exact boundary of every real check (the precise value that passes vs. the
one unit past it that vetoes); that every `None` optional input (fee bounds,
timestamps, depth, open position) vetoes; that an unknown `action` vetoes
every check that branches on open/close; the unchanged 24-name SPEC S10.3
order; that a fully-permissive context leaves *only* the 7 stub checks
vetoing, each naming its blocking issue; the fail-closed error-conversion
contract; and the frozen result types.

This file supersedes and deletes `tests/riskkernel/test_checks_stub.py`:
every behavior that file pinned (frozen `OrderIntent`/`CheckResult`/
`Decision`, the 24-name sequence, fail-closed error conversion, veto/pass
aggregation) is re-pinned here against the new `(intent, context)` call
signature, so the older, `OrderIntent`-only-signature file would otherwise
merely duplicate (and, for its 24-checks-all-veto assumption, contradict) it.
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
    evaluate_intent,
)
from hedgekit.riskkernel.modes import Mode
from tests.riskkernel.conftest import (
    DEFAULT_MARKET_TICKER,
    DEFAULT_NOW_EPOCH_S,
    make_context,
    make_intent,
)

if TYPE_CHECKING:
    from hedgekit.riskkernel.checks import Check

#: The exact SPEC S10.3 check-name sequence, in this exact order -- unchanged
#: by issue #30 (only each check's internals and arity change).
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

_EXPECTED_CHECK_COUNT = 24

#: The 17 checks now real after issues #30 and #31, in SPEC S10.3 order.
#: `approval_token_uniqueness` / `idempotency_key_uniqueness` are the two
#: issue #31 promotes from stub to real logic.
REAL_CHECK_NAMES: tuple[str, ...] = (
    "instrument_whitelist",
    "mode_permission_ceiling",
    "floor_invariant",
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
    "approval_token_uniqueness",
    "idempotency_key_uniqueness",
    "clock_skew_limit",
    "reduce_only_provable",
)

#: The 7 checks that remain deliberate stubs after issue #31, each blocked on
#: a later issue -- `None` for `jurisdiction_product_eligibility`, which has
#: no tracking issue yet (a follow-up to file, not invented here).
_STUB_ISSUE_NUMBERS: dict[str, int | None] = {
    "jurisdiction_product_eligibility": None,
    "balance_reconciliation": 32,
    "position_reconciliation": 32,
    "open_order_reconciliation": 32,
    "human_ack_satisfied": 34,
    "exchange_status_ok": 32,
    "pipeline_heartbeat_ok": 32,
}

STUB_CHECK_NAMES: tuple[str, ...] = tuple(_STUB_ISSUE_NUMBERS)

#: Every check callable, keyed by its own `.name` -- built from each check's
#: self-reported name (not by zipping against `EXPECTED_CHECK_NAMES`), so a
#: per-check test's result is independent of the separately-pinned ordering
#: test below.
_CHECK_BY_NAME: dict[str, Check] = {check.name: check for check in DEFAULT_CHECKS}


def _real_check(name: str) -> Check:
    """Look up one of the 24 checks (real or stub) by name, for direct
    invocation.

    Args:
        name: The check's SPEC S10.3 name.

    Returns:
        The check callable from `DEFAULT_CHECKS`.
    """
    return _CHECK_BY_NAME[name]


class _RaisingCheck:
    """A check double that raises instead of returning a `CheckResult`."""

    name = "raising_check"

    def __call__(self, intent: object, context: object) -> CheckResult:
        """Raise unconditionally, ignoring both arguments.

        Args:
            intent: Unused; accepted only to match the check-callable shape.
            context: Unused; accepted only to match the check-callable shape.

        Raises:
            RuntimeError: Always, with a fixed, recognizable message.
        """
        raise RuntimeError("boom")


class _PassingCheck:
    """A check double that approves (does not veto) the intent."""

    name = "passing_check"

    def __call__(self, intent: object, context: object) -> CheckResult:
        """Approve unconditionally, ignoring both arguments.

        Args:
            intent: Unused; accepted only to match the check-callable shape.
            context: Unused; accepted only to match the check-callable shape.

        Returns:
            A non-vetoing `CheckResult`.
        """
        return CheckResult(vetoed=False, reason="approved")


# --- Sanity on the fixtures themselves -------------------------------------------


def test_real_and_stub_name_sets_partition_the_24_spec_names_exactly() -> None:
    """`REAL_CHECK_NAMES` and `STUB_CHECK_NAMES` are disjoint and together
    equal the full 24-name SPEC S10.3 set -- protects the taxonomy this whole
    file's pipeline-level tests assume.
    """
    assert len(REAL_CHECK_NAMES) == 17
    assert len(STUB_CHECK_NAMES) == 7
    assert set(REAL_CHECK_NAMES).isdisjoint(STUB_CHECK_NAMES)
    assert set(REAL_CHECK_NAMES) | set(STUB_CHECK_NAMES) == set(EXPECTED_CHECK_NAMES)


def test_make_context_defaults_make_every_real_check_pass() -> None:
    """`make_context()` paired with `make_intent()` passes all 17 real
    checks -- the load-bearing fixture assumption every per-check boundary
    test below relies on to isolate its one overridden field.
    """
    intent = make_intent()
    context = make_context()

    for name in REAL_CHECK_NAMES:
        result = _real_check(name)(intent, context)
        assert result.vetoed is False, f"{name} unexpectedly vetoed: {result.reason}"


# --- instrument_whitelist ---------------------------------------------------------


def test_instrument_whitelist_passes_when_ticker_is_listed() -> None:
    """A ticker present in the whitelist passes."""
    result = _real_check("instrument_whitelist")(make_intent(), make_context())

    assert result.vetoed is False


def test_instrument_whitelist_vetoes_when_ticker_is_absent() -> None:
    """A ticker not in the whitelist vetoes."""
    context = make_context(instrument_whitelist=frozenset({"OTHER-TICKER"}))

    result = _real_check("instrument_whitelist")(make_intent(), context)

    assert result.vetoed is True


def test_instrument_whitelist_vetoes_everything_when_empty() -> None:
    """An empty whitelist vetoes every ticker -- the boundary an
    accidentally-permissive "empty means allow all" mutant would fail.
    """
    context = make_context(instrument_whitelist=frozenset())

    result = _real_check("instrument_whitelist")(make_intent(), context)

    assert result.vetoed is True


# --- mode_permission_ceiling -------------------------------------------------------


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE_MICRO, Mode.LIVE])
def test_mode_permission_ceiling_passes_for_each_permitted_mode(mode: Mode) -> None:
    """PAPER, LIVE_MICRO, and LIVE are each permitted to trade."""
    context = make_context(mode=mode, micro_cap=MoneyMicros(1_000_000_000_000))

    result = _real_check("mode_permission_ceiling")(make_intent(), context)

    assert result.vetoed is False


@pytest.mark.parametrize("mode", [Mode.RESEARCH, Mode.PAUSED, Mode.HALT, Mode.KILLED])
def test_mode_permission_ceiling_vetoes_for_each_unpermitted_mode(mode: Mode) -> None:
    """RESEARCH and every safety mode are not permitted to trade."""
    context = make_context(mode=mode)

    result = _real_check("mode_permission_ceiling")(make_intent(), context)

    assert result.vetoed is True


def test_mode_permission_ceiling_live_micro_passes_at_exact_micro_cap() -> None:
    """In LIVE_MICRO, `total_exposure + worst_case_cost == micro_cap` passes
    (the cap is inclusive): default intent cost is 5_000_000 micros.
    """
    context = make_context(
        mode=Mode.LIVE_MICRO,
        total_exposure=MoneyMicros(0),
        micro_cap=MoneyMicros(5_000_000),
    )

    result = _real_check("mode_permission_ceiling")(make_intent(), context)

    assert result.vetoed is False


def test_mode_permission_ceiling_live_micro_vetoes_one_micro_over_cap() -> None:
    """One micro above `micro_cap` vetoes in LIVE_MICRO."""
    context = make_context(
        mode=Mode.LIVE_MICRO,
        total_exposure=MoneyMicros(0),
        micro_cap=MoneyMicros(4_999_999),
    )

    result = _real_check("mode_permission_ceiling")(make_intent(), context)

    assert result.vetoed is True


@pytest.mark.parametrize(
    "fees_override",
    [
        {"max_trading_fee": None},
        {"max_settlement_fee": None},
    ],
    ids=["trading_fee_none", "settlement_fee_none"],
)
def test_mode_permission_ceiling_live_micro_vetoes_when_a_fee_bound_is_none(
    fees_override: dict[str, object],
) -> None:
    """In LIVE_MICRO, a missing (`None`) fee bound of either kind makes the
    exposure-ceiling cost unprovable, so the check vetoes as `"unprovable"`
    (fail-closed) rather than raising or approving -- the guard that replaced a
    `python -O`-strippable `assert`.
    """
    context = make_context(mode=Mode.LIVE_MICRO, **fees_override)

    result = _real_check("mode_permission_ceiling")(make_intent(), context)

    assert result.vetoed is True
    assert result.reason == "unprovable"


# --- floor_invariant ---------------------------------------------------------------


def test_floor_invariant_open_passes_at_exact_floor_equality() -> None:
    """An open whose `worst_case_equity - worst_case_cost` equals the floor
    exactly passes: default equity 1_000_000_000, default cost 5_000_000.
    """
    context = make_context(floor=MoneyMicros(995_000_000))

    result = _real_check("floor_invariant")(make_intent(action="buy"), context)

    assert result.vetoed is False


def test_floor_invariant_open_vetoes_one_micro_below_floor_equality() -> None:
    """A floor one micro above what the equity affords vetoes the open."""
    context = make_context(floor=MoneyMicros(995_000_001))

    result = _real_check("floor_invariant")(make_intent(action="buy"), context)

    assert result.vetoed is True


def test_floor_invariant_close_ignores_proceeds_and_passes_at_exact_floor() -> None:
    """A provable close's cost is fees + buffer only (price/size proceeds
    are ignored): equity 1_000_000_000 - (300_000 + 150_000 + 75_000) ==
    999_475_000 passes at that exact floor.
    """
    context = make_context(
        floor=MoneyMicros(999_475_000),
        max_trading_fee=MoneyMicros(300_000),
        max_settlement_fee=MoneyMicros(150_000),
        rounding_buffer=MoneyMicros(75_000),
    )
    intent = make_intent(action="sell_to_close", price=PricePips(9_999))

    result = _real_check("floor_invariant")(intent, context)

    assert result.vetoed is False


def test_floor_invariant_provable_close_vetoes_one_micro_below_floor_equality() -> None:
    """The close-side floor boundary is exact too: one micro over vetoes."""
    context = make_context(
        floor=MoneyMicros(999_475_001),
        max_trading_fee=MoneyMicros(300_000),
        max_settlement_fee=MoneyMicros(150_000),
        rounding_buffer=MoneyMicros(75_000),
    )
    intent = make_intent(action="sell_to_close")

    result = _real_check("floor_invariant")(intent, context)

    assert result.vetoed is True


def test_floor_invariant_unknown_action_vetoes_as_unprovable() -> None:
    """An action that is neither an open nor a provable close vetoes as
    `"unprovable"` -- the exact reason SPEC S10.3 names.
    """
    intent = make_intent(action="hold")

    result = _real_check("floor_invariant")(intent, make_context())

    assert result.vetoed is True
    assert result.reason == "unprovable"


@pytest.mark.parametrize(
    "fees_override",
    [
        {"max_trading_fee": None},
        {"max_settlement_fee": None},
    ],
    ids=["trading_fee_none", "settlement_fee_none"],
)
def test_floor_invariant_vetoes_as_unprovable_when_any_fee_bound_is_none(
    fees_override: dict[str, object],
) -> None:
    """A missing (`None`) fee bound of either kind vetoes as `"unprovable"`,
    regardless of action.
    """
    context = make_context(**fees_override)

    result = _real_check("floor_invariant")(make_intent(), context)

    assert result.vetoed is True
    assert result.reason == "unprovable"


# --- fee_upper_bound_present / settlement_fee_upper_bound -----------------------


def test_fee_upper_bound_present_passes_when_trading_fee_bound_present() -> None:
    """A present (non-`None`) trading fee bound passes, even if the
    settlement bound is `None` -- this check reads only the trading fee.
    """
    context = make_context(max_trading_fee=MoneyMicros(0), max_settlement_fee=None)

    result = _real_check("fee_upper_bound_present")(make_intent(), context)

    assert result.vetoed is False


def test_fee_upper_bound_present_vetoes_when_trading_fee_bound_is_none() -> None:
    """A `None` trading fee bound vetoes."""
    context = make_context(max_trading_fee=None)

    result = _real_check("fee_upper_bound_present")(make_intent(), context)

    assert result.vetoed is True


def test_settlement_fee_upper_bound_passes_when_settlement_fee_bound_present() -> None:
    """A present settlement fee bound passes, even if the trading bound is
    `None` -- this check reads only the settlement fee.
    """
    context = make_context(max_settlement_fee=MoneyMicros(0), max_trading_fee=None)

    result = _real_check("settlement_fee_upper_bound")(make_intent(), context)

    assert result.vetoed is False


def test_settlement_fee_upper_bound_vetoes_when_settlement_fee_bound_is_none() -> None:
    """A `None` settlement fee bound vetoes."""
    context = make_context(max_settlement_fee=None)

    result = _real_check("settlement_fee_upper_bound")(make_intent(), context)

    assert result.vetoed is True


# --- concentration_limits ----------------------------------------------------------

#: Each of the four concentration dimensions: (account exposure field, limits
#: cap-percentage field). At a 10% cap over the default $1,000 equity, the
#: threshold is exactly 100_000_000 micros; less the default 5_000_000-micro
#: cost, the boundary exposure is 95_000_000.
_CONCENTRATION_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("market_exposure", "max_pos_market_pct_ppm"),
    ("event_exposure", "max_pos_event_pct_ppm"),
    ("bucket_exposure", "max_pos_bucket_pct_ppm"),
    ("total_exposure", "max_pos_total_pct_ppm"),
)

_CONCENTRATION_CAP_PPM = 100_000
_CONCENTRATION_BOUNDARY_EXPOSURE_MICROS = 95_000_000


@pytest.mark.parametrize(
    ("exposure_field", "cap_field"),
    _CONCENTRATION_DIMENSIONS,
    ids=[pair[0] for pair in _CONCENTRATION_DIMENSIONS],
)
def test_concentration_limits_passes_at_exact_threshold(
    exposure_field: str, cap_field: str
) -> None:
    """Each dimension passes when `exposure + cost` equals the floored
    percentage-of-equity threshold exactly.
    """
    context = make_context(
        **{
            cap_field: _CONCENTRATION_CAP_PPM,
            exposure_field: MoneyMicros(_CONCENTRATION_BOUNDARY_EXPOSURE_MICROS),
        }
    )

    result = _real_check("concentration_limits")(make_intent(), context)

    assert result.vetoed is False


@pytest.mark.parametrize(
    ("exposure_field", "cap_field"),
    _CONCENTRATION_DIMENSIONS,
    ids=[pair[0] for pair in _CONCENTRATION_DIMENSIONS],
)
def test_concentration_limits_vetoes_one_micro_over_threshold(
    exposure_field: str, cap_field: str
) -> None:
    """Each dimension vetoes one micro past that same threshold."""
    context = make_context(
        **{
            cap_field: _CONCENTRATION_CAP_PPM,
            exposure_field: MoneyMicros(_CONCENTRATION_BOUNDARY_EXPOSURE_MICROS + 1),
        }
    )

    result = _real_check("concentration_limits")(make_intent(), context)

    assert result.vetoed is True


@pytest.mark.parametrize(
    "fees_override",
    [
        {"max_trading_fee": None},
        {"max_settlement_fee": None},
    ],
    ids=["trading_fee_none", "settlement_fee_none"],
)
def test_concentration_limits_vetoes_when_a_fee_bound_is_none(
    fees_override: dict[str, object],
) -> None:
    """A missing (`None`) fee bound of either kind makes the order cost
    unprovable, so `concentration_limits` vetoes as `"unprovable"` (fail-closed)
    instead of raising or approving.
    """
    context = make_context(**fees_override)

    result = _real_check("concentration_limits")(make_intent(), context)

    assert result.vetoed is True
    assert result.reason == "unprovable"


def test_concentration_limits_charges_a_close_its_full_notional() -> None:
    """`concentration_limits` charges the full worst-case notional even for a
    `sell_to_close`, matching a buy. Pinned at the one-micro-over-threshold
    exposure: the close still vetoes, which it would not if closes were charged
    only fees+buffer (that far-smaller cost would keep `exposure + cost` under
    the cap). This pins the deliberate conservative choice in `_order_cost` --
    only `floor_invariant` reduces a provable close's cost.
    """
    context = make_context(
        max_pos_market_pct_ppm=_CONCENTRATION_CAP_PPM,
        market_exposure=MoneyMicros(_CONCENTRATION_BOUNDARY_EXPOSURE_MICROS + 1),
    )

    result = _real_check("concentration_limits")(
        make_intent(action="sell_to_close"), context
    )

    assert result.vetoed is True


# --- daily_loss_limit ---------------------------------------------------------------


def test_daily_loss_limit_passes_one_micro_below_threshold() -> None:
    """10% of the default $1,000 `equity_start_of_day` is 100_000_000
    micros; one micro below that passes.
    """
    context = make_context(
        daily_loss_limit_pct_ppm=100_000,
        realized_loss_today=MoneyMicros(99_999_999),
    )

    result = _real_check("daily_loss_limit")(make_intent(), context)

    assert result.vetoed is False


def test_daily_loss_limit_vetoes_at_exact_threshold() -> None:
    """The threshold itself vetoes: `>=`, not `>` -- the boundary is the
    opposite sense from `floor_invariant`'s (there, exact equality passes).
    """
    context = make_context(
        daily_loss_limit_pct_ppm=100_000,
        realized_loss_today=MoneyMicros(100_000_000),
    )

    result = _real_check("daily_loss_limit")(make_intent(), context)

    assert result.vetoed is True


# --- trailing_drawdown_limit ---------------------------------------------------------


def test_trailing_drawdown_limit_passes_one_micro_below_threshold() -> None:
    """Reducing cash by 99_999_999 (from the default $1,000 high-water mark)
    keeps drawdown one micro below the 10% threshold (100_000_000)."""
    context = make_context(
        max_drawdown_pct_ppm=100_000,
        exchange_verified_available_cash=MoneyMicros(900_000_001),
    )

    result = _real_check("trailing_drawdown_limit")(make_intent(), context)

    assert result.vetoed is False


def test_trailing_drawdown_limit_vetoes_at_exact_threshold() -> None:
    """Drawdown exactly at the threshold vetoes (`>=`, matching
    `daily_loss_limit`'s boundary sense)."""
    context = make_context(
        max_drawdown_pct_ppm=100_000,
        exchange_verified_available_cash=MoneyMicros(900_000_000),
    )

    result = _real_check("trailing_drawdown_limit")(make_intent(), context)

    assert result.vetoed is True


# --- velocity_limits ------------------------------------------------------------------


def test_velocity_limits_passes_at_exact_orders_per_hour_boundary() -> None:
    """`orders_last_hour + 1 == max_orders_per_hour` passes (not `>`)."""
    context = make_context(max_orders_per_hour=5, orders_last_hour=4)

    result = _real_check("velocity_limits")(make_intent(), context)

    assert result.vetoed is False


def test_velocity_limits_vetoes_one_order_over_the_hourly_cap() -> None:
    """`orders_last_hour + 1 > max_orders_per_hour` vetoes."""
    context = make_context(max_orders_per_hour=5, orders_last_hour=5)

    result = _real_check("velocity_limits")(make_intent(), context)

    assert result.vetoed is True


def test_velocity_limits_passes_at_exact_daily_notional_boundary() -> None:
    """`notional_today + worst_case_cost == max_notional_per_day` passes
    (default cost is 5_000_000 micros)."""
    context = make_context(
        notional_today=MoneyMicros(0), max_notional_per_day=MoneyMicros(5_000_000)
    )

    result = _real_check("velocity_limits")(make_intent(), context)

    assert result.vetoed is False


def test_velocity_limits_vetoes_one_micro_over_the_daily_notional_cap() -> None:
    """One micro past the daily notional cap vetoes."""
    context = make_context(
        notional_today=MoneyMicros(0), max_notional_per_day=MoneyMicros(4_999_999)
    )

    result = _real_check("velocity_limits")(make_intent(), context)

    assert result.vetoed is True


@pytest.mark.parametrize(
    "fees_override",
    [
        {"max_trading_fee": None},
        {"max_settlement_fee": None},
    ],
    ids=["trading_fee_none", "settlement_fee_none"],
)
def test_velocity_limits_vetoes_when_a_fee_bound_is_none(
    fees_override: dict[str, object],
) -> None:
    """A missing (`None`) fee bound of either kind makes the daily-notional
    cost unprovable, so `velocity_limits` vetoes as `"unprovable"` (fail-closed)
    instead of raising or approving.
    """
    context = make_context(**fees_override)

    result = _real_check("velocity_limits")(make_intent(), context)

    assert result.vetoed is True
    assert result.reason == "unprovable"


# --- quote_freshness / forecast_freshness ---------------------------------------


def test_quote_freshness_passes_at_exact_ttl_age() -> None:
    """A quote exactly `quote_ttl_seconds` old (age == ttl) is still fresh."""
    context = make_context(
        quote_snapshot_epoch_s=DEFAULT_NOW_EPOCH_S - 3_600, quote_ttl_seconds=3_600
    )

    result = _real_check("quote_freshness")(make_intent(), context)

    assert result.vetoed is False


def test_quote_freshness_vetoes_one_second_past_ttl() -> None:
    """A quote one second past its ttl is stale."""
    context = make_context(
        quote_snapshot_epoch_s=DEFAULT_NOW_EPOCH_S - 3_601, quote_ttl_seconds=3_600
    )

    result = _real_check("quote_freshness")(make_intent(), context)

    assert result.vetoed is True


def test_quote_freshness_vetoes_a_future_timestamp() -> None:
    """A quote timestamped after `now_epoch_s` vetoes, however fresh its
    age would otherwise appear."""
    context = make_context(quote_snapshot_epoch_s=DEFAULT_NOW_EPOCH_S + 1)

    result = _real_check("quote_freshness")(make_intent(), context)

    assert result.vetoed is True


def test_quote_freshness_vetoes_when_snapshot_epoch_is_none() -> None:
    """A missing quote timestamp vetoes."""
    context = make_context(quote_snapshot_epoch_s=None)

    result = _real_check("quote_freshness")(make_intent(), context)

    assert result.vetoed is True


def test_forecast_freshness_passes_at_exact_ttl_age() -> None:
    """A forecast exactly `forecast_ttl_seconds` old is still fresh."""
    context = make_context(
        forecast_epoch_s=DEFAULT_NOW_EPOCH_S - 3_600, forecast_ttl_seconds=3_600
    )

    result = _real_check("forecast_freshness")(make_intent(), context)

    assert result.vetoed is False


def test_forecast_freshness_vetoes_one_second_past_ttl() -> None:
    """A forecast one second past its ttl is stale."""
    context = make_context(
        forecast_epoch_s=DEFAULT_NOW_EPOCH_S - 3_601, forecast_ttl_seconds=3_600
    )

    result = _real_check("forecast_freshness")(make_intent(), context)

    assert result.vetoed is True


def test_forecast_freshness_vetoes_a_future_timestamp() -> None:
    """A forecast timestamped after `now_epoch_s` vetoes."""
    context = make_context(forecast_epoch_s=DEFAULT_NOW_EPOCH_S + 1)

    result = _real_check("forecast_freshness")(make_intent(), context)

    assert result.vetoed is True


def test_forecast_freshness_vetoes_when_forecast_epoch_is_none() -> None:
    """A missing forecast timestamp vetoes."""
    context = make_context(forecast_epoch_s=None)

    result = _real_check("forecast_freshness")(make_intent(), context)

    assert result.vetoed is True


# --- price_band_compliance --------------------------------------------------------


def test_price_band_compliance_passes_at_each_inclusive_edge() -> None:
    """An open priced at exactly `min_open_price` or `max_open_price` passes
    -- both edges are inclusive.
    """
    context = make_context(
        min_open_price=PricePips(4_000), max_open_price=PricePips(6_000)
    )

    low_edge = _real_check("price_band_compliance")(
        make_intent(price=PricePips(4_000)), context
    )
    high_edge = _real_check("price_band_compliance")(
        make_intent(price=PricePips(6_000)), context
    )

    assert low_edge.vetoed is False
    assert high_edge.vetoed is False


def test_price_band_compliance_vetoes_one_pip_outside_each_edge() -> None:
    """One pip below the minimum, or above the maximum, vetoes."""
    context = make_context(
        min_open_price=PricePips(4_000), max_open_price=PricePips(6_000)
    )

    below = _real_check("price_band_compliance")(
        make_intent(price=PricePips(3_999)), context
    )
    above = _real_check("price_band_compliance")(
        make_intent(price=PricePips(6_001)), context
    )

    assert below.vetoed is True
    assert above.vetoed is True


def test_price_band_compliance_provable_close_passes_regardless_of_price() -> None:
    """A provable close passes even priced outside the open band -- the band
    only constrains opens."""
    context = make_context(
        min_open_price=PricePips(4_000), max_open_price=PricePips(6_000)
    )
    intent = make_intent(action="sell_to_close", price=PricePips(1))

    result = _real_check("price_band_compliance")(intent, context)

    assert result.vetoed is False


def test_price_band_compliance_vetoes_an_unknown_action() -> None:
    """An action that is neither an open nor a close vetoes."""
    result = _real_check("price_band_compliance")(
        make_intent(action="hold"), make_context()
    )

    assert result.vetoed is True


# --- participation_cap_compliance -------------------------------------------------


def test_participation_cap_compliance_passes_at_exact_threshold() -> None:
    """10% of a 10_000-centi visible depth is exactly 1_000 centis, the
    default intent size -- passes at that exact boundary.
    """
    context = make_context(
        visible_depth=ContractCentis(10_000), max_participation_ppm=100_000
    )

    result = _real_check("participation_cap_compliance")(make_intent(), context)

    assert result.vetoed is False


def test_participation_cap_compliance_vetoes_one_centi_over_threshold() -> None:
    """One centi past that threshold vetoes."""
    context = make_context(
        visible_depth=ContractCentis(10_000), max_participation_ppm=100_000
    )
    intent = make_intent(size=ContractCentis(1_001))

    result = _real_check("participation_cap_compliance")(intent, context)

    assert result.vetoed is True


def test_participation_cap_compliance_vetoes_when_visible_depth_is_none() -> None:
    """A missing visible depth vetoes."""
    context = make_context(visible_depth=None)

    result = _real_check("participation_cap_compliance")(make_intent(), context)

    assert result.vetoed is True


# --- approval_token_uniqueness (issue #31) ----------------------------------------


def test_approval_token_uniqueness_passes_when_intent_id_is_unused() -> None:
    """An intent id absent from `context.used_intent_ids` passes."""
    context = make_context(used_intent_ids=frozenset())

    result = _real_check("approval_token_uniqueness")(make_intent(), context)

    assert result.vetoed is False


def test_approval_token_uniqueness_vetoes_when_intent_id_already_used() -> None:
    """An intent id already present in `context.used_intent_ids` vetoes --
    the kernel never issues a second approval token for the same intent.
    """
    intent = make_intent(intent_id="dup-intent")
    context = make_context(used_intent_ids=frozenset({"dup-intent"}))

    result = _real_check("approval_token_uniqueness")(intent, context)

    assert result.vetoed is True


def test_approval_token_uniqueness_passes_when_only_other_ids_are_used() -> None:
    """A `used_intent_ids` set containing *other* intent ids does not veto
    this unrelated intent -- pins membership, not "any ids used at all".
    """
    intent = make_intent(intent_id="fresh-intent")
    context = make_context(used_intent_ids=frozenset({"other-1", "other-2"}))

    result = _real_check("approval_token_uniqueness")(intent, context)

    assert result.vetoed is False


# --- idempotency_key_uniqueness (issue #31) ---------------------------------------


def test_idempotency_key_uniqueness_passes_when_key_is_unused() -> None:
    """An idempotency key absent from `context.used_idempotency_keys` passes."""
    context = make_context(used_idempotency_keys=frozenset())

    result = _real_check("idempotency_key_uniqueness")(make_intent(), context)

    assert result.vetoed is False


def test_idempotency_key_uniqueness_vetoes_when_key_already_used() -> None:
    """An idempotency key already present in `context.used_idempotency_keys`
    vetoes -- the kernel never double-reserves against the same key.
    """
    intent = make_intent(idempotency_key="dup-idem")
    context = make_context(used_idempotency_keys=frozenset({"dup-idem"}))

    result = _real_check("idempotency_key_uniqueness")(intent, context)

    assert result.vetoed is True


def test_idempotency_key_uniqueness_passes_when_only_other_keys_are_used() -> None:
    """A `used_idempotency_keys` set containing *other* keys does not veto
    this unrelated intent -- pins membership, not "any keys used at all".
    """
    intent = make_intent(idempotency_key="fresh-idem")
    context = make_context(used_idempotency_keys=frozenset({"other-1", "other-2"}))

    result = _real_check("idempotency_key_uniqueness")(intent, context)

    assert result.vetoed is False


# --- clock_skew_limit -------------------------------------------------------------


def test_clock_skew_limit_passes_at_exact_skew_boundary() -> None:
    """A skew exactly at `clock_skew_max_seconds` passes (equal passes)."""
    context = make_context(
        exchange_clock_epoch_s=DEFAULT_NOW_EPOCH_S - 3_600, clock_skew_max_seconds=3_600
    )

    result = _real_check("clock_skew_limit")(make_intent(), context)

    assert result.vetoed is False


@pytest.mark.parametrize("direction", [-1, 1], ids=["behind", "ahead"])
def test_clock_skew_limit_vetoes_one_second_past_the_boundary_either_direction(
    direction: int,
) -> None:
    """One second past the skew boundary vetoes, whether the exchange clock
    is behind or ahead of `now_epoch_s` -- `abs()` is symmetric.
    """
    skewed_epoch = DEFAULT_NOW_EPOCH_S + direction * 3_601
    context = make_context(
        exchange_clock_epoch_s=skewed_epoch, clock_skew_max_seconds=3_600
    )

    result = _real_check("clock_skew_limit")(make_intent(), context)

    assert result.vetoed is True


def test_clock_skew_limit_vetoes_when_exchange_clock_epoch_is_none() -> None:
    """A missing exchange clock timestamp vetoes."""
    context = make_context(exchange_clock_epoch_s=None)

    result = _real_check("clock_skew_limit")(make_intent(), context)

    assert result.vetoed is True


# --- reduce_only_provable ----------------------------------------------------------


def test_reduce_only_provable_passes_any_open_regardless_of_open_position() -> None:
    """Opens are "not applicable" to reduce-only provability and pass even
    with no recorded open position.
    """
    context = make_context(open_position=None)

    result = _real_check("reduce_only_provable")(make_intent(action="buy"), context)

    assert result.vetoed is False


def test_reduce_only_provable_close_passes_at_size_equal_to_open_position() -> None:
    """A close sized exactly equal to the open position passes."""
    context = make_context(open_position=ContractCentis(1_000))
    intent = make_intent(action="sell_to_close", size=ContractCentis(1_000))

    result = _real_check("reduce_only_provable")(intent, context)

    assert result.vetoed is False


def test_reduce_only_provable_close_vetoes_one_centi_over_the_open_position() -> None:
    """A close one centi larger than the open position vetoes -- it would
    flip to a net-new position, not reduce one.
    """
    context = make_context(open_position=ContractCentis(999))
    intent = make_intent(action="sell_to_close", size=ContractCentis(1_000))

    result = _real_check("reduce_only_provable")(intent, context)

    assert result.vetoed is True


def test_reduce_only_provable_close_vetoes_when_open_position_is_none() -> None:
    """A close with no recorded open position vetoes -- there is nothing on
    record to prove it reduces."""
    context = make_context(open_position=None)
    intent = make_intent(action="sell_to_close")

    result = _real_check("reduce_only_provable")(intent, context)

    assert result.vetoed is True


def test_reduce_only_provable_vetoes_an_unknown_action() -> None:
    """An action that is neither an open nor a close vetoes."""
    intent = make_intent(action="hold")

    result = _real_check("reduce_only_provable")(intent, make_context())

    assert result.vetoed is True


# --- Pipeline: order, stub survivors, fail-closed, frozen types -------------------


def test_default_checks_names_match_spec_10_3_exactly_in_order() -> None:
    """`DEFAULT_CHECKS`' `.name`s equal the pinned SPEC S10.3 sequence
    exactly, in order -- unchanged by issue #30."""
    assert len(DEFAULT_CHECKS) == _EXPECTED_CHECK_COUNT
    assert tuple(check.name for check in DEFAULT_CHECKS) == EXPECTED_CHECK_NAMES


def test_default_checks_over_permissive_context_leaves_only_stubs_vetoing() -> None:
    """Given `make_intent()`/`make_context()` (tuned so every real check
    passes), `evaluate_intent` is still vetoed -- but with exactly the 7 stub
    reasons, in SPEC S10.3 order, each naming its blocking issue (where one is
    known). This is the test that proves which 17 checks are now real.
    """
    intent = make_intent()
    context = make_context()

    decision = evaluate_intent(intent, context)

    assert decision.vetoed is True
    stub_positions = [name for name in EXPECTED_CHECK_NAMES if name in STUB_CHECK_NAMES]
    assert len(stub_positions) == 7
    assert len(decision.reasons) == 7
    for reason, name in zip(decision.reasons, stub_positions, strict=True):
        issue_number = _STUB_ISSUE_NUMBERS[name]
        if issue_number is None:
            assert reason
        else:
            assert f"#{issue_number}" in reason, f"{name}: {reason!r}"


@pytest.mark.parametrize("name", STUB_CHECK_NAMES)
def test_each_stub_check_still_vetoes_a_valid_intent(name: str) -> None:
    """Called directly, every stub check still vetoes over a fully
    permissive context -- issues #30/#31 together only promote 17 of the 24
    checks to real logic."""
    result = _real_check(name)(make_intent(), make_context())

    assert result.vetoed is True


def test_evaluate_intent_fail_closed_converts_a_raised_exception_to_a_veto() -> None:
    """A check that raises is converted to a veto reason -- the exception
    never escapes `evaluate_intent` -- and checks positioned after the
    raising one still run.
    """
    intent = make_intent()
    context = make_context()
    checks = (*DEFAULT_CHECKS[:2], _RaisingCheck(), *DEFAULT_CHECKS[2:])

    decision = evaluate_intent(intent, context, checks=checks)

    assert decision.vetoed is True
    assert "raising_check: error: boom" in decision.reasons


def test_evaluate_intent_omits_reasons_for_checks_that_pass() -> None:
    """A non-vetoing check contributes no reason; only vetoing checks do."""
    intent = make_intent()
    context = make_context()
    checks = (
        _PassingCheck(),
        _real_check("jurisdiction_product_eligibility"),
        _PassingCheck(),
    )

    decision = evaluate_intent(intent, context, checks=checks)

    assert decision.vetoed is True
    assert len(decision.reasons) == 1


def test_evaluate_intent_with_only_passing_checks_is_not_vetoed() -> None:
    """When every check passes, the aggregate decision does not veto."""
    decision = evaluate_intent(
        make_intent(), make_context(), checks=(_PassingCheck(), _PassingCheck())
    )

    assert decision.vetoed is False
    assert decision.reasons == ()


def test_evaluate_intent_with_an_empty_checks_sequence_is_not_vetoed() -> None:
    """With zero checks, there is nothing to veto on."""
    decision = evaluate_intent(make_intent(), make_context(), checks=())

    assert decision.vetoed is False
    assert decision.reasons == ()


# --- OrderIntent: frozen, scaled-int-only (migrated from test_checks_stub.py) --------


def test_order_intent_is_frozen() -> None:
    """Mutating any field of a constructed `OrderIntent` raises."""
    intent = make_intent()

    # `setattr` on a runtime-bound name is the sanctioned idiom: a literal
    # `intent.action = ...` on a frozen dataclass is a static mypy error, not
    # the runtime `FrozenInstanceError` this test pins; a literal `setattr`
    # name in turn trips ruff B010, so the field name is bound at runtime.
    frozen_field = "action"
    with pytest.raises(FrozenInstanceError):
        setattr(intent, frozen_field, "sell")


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
        action="sell_to_close",
    )

    assert intent.intent_id == "intent-xyz"
    assert intent.market_ticker == "TICKER-X"
    assert intent.outcome == "no"
    assert intent.action == "sell_to_close"


def test_order_intent_rejects_a_new_attribute() -> None:
    """`OrderIntent` is a frozen dataclass: assigning any undeclared
    attribute raises `AttributeError` (via `FrozenInstanceError`).
    """
    intent = make_intent()
    # The attribute name is bound at runtime (not a literal) because assigning an
    # *undeclared* attribute cannot be written as `intent.not_a_declared_field =
    # ...` -- that is a static attr-defined error, not the runtime rejection this
    # test pins. `setattr` on a runtime name is the sanctioned idiom for it.
    undeclared_attribute = "not_a_declared_field"

    with pytest.raises(AttributeError):
        setattr(intent, undeclared_attribute, "nope")


def test_check_result_is_frozen() -> None:
    """`CheckResult` instances reject attribute mutation."""
    result = CheckResult(vetoed=True, reason="blocked by issue #32")

    frozen_field = "reason"
    with pytest.raises(FrozenInstanceError):
        setattr(result, frozen_field, "changed")


def test_decision_is_frozen() -> None:
    """`Decision` instances (as returned by `evaluate_intent`) reject
    attribute mutation.
    """
    decision = evaluate_intent(make_intent(), make_context())

    frozen_field = "vetoed"
    with pytest.raises(FrozenInstanceError):
        setattr(decision, frozen_field, False)


def test_default_market_ticker_fixture_matches_default_intent_ticker() -> None:
    """Sanity check on the shared fixtures: the conftest constant and
    `make_intent()`'s default ticker agree, so `instrument_whitelist` passes
    by default as every other per-check test assumes.
    """
    assert make_intent().market_ticker == DEFAULT_MARKET_TICKER
