"""Worst-case equity and cost arithmetic for the Risk Kernel (SPEC S10.4).

The floor invariant compares a worst-case account equity against a worst-case
order cost: an open is only admissible if, even under the most adverse
resolution of every uncertain quantity, the resulting equity still clears the
configured floor. This module provides the two exact-integer primitives that
comparison is built from:

    * :func:`worst_case_equity` -- available cash plus the guaranteed terminal
      value of open positions, less every capital claim that could yet
      materialize (pending reservations, unresolved fee upper bounds, and a
      reconciliation-uncertainty buffer).
    * :func:`worst_case_cost` -- the notional of the order (rounded so a cost is
      never *under*-stated), plus worst-case trading and settlement fees and a
      rounding buffer.

Every term is a :mod:`hedgekit.numeric` scaled-integer type and every operation
is exact integer arithmetic: within-unit ``+``/``-`` on :class:`MoneyMicros`
and the sanctioned :func:`money_from_price_and_count` conversion. No float ever
appears (SPEC S6.1, enforced by ``scripts/lint_no_floats.py``), and the notional
term is deliberately routed through :data:`RoundingDirection.OVERSTATE_COST` so
a dropped remainder can only ever make a cost look larger, never smaller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hedgekit.numeric import RoundingDirection, money_from_price_and_count

if TYPE_CHECKING:
    from hedgekit.numeric.types import ContractCentis, MoneyMicros, PricePips


def worst_case_equity(
    *,
    exchange_verified_available_cash: MoneyMicros,
    guaranteed_terminal_value_of_positions: MoneyMicros,
    pending_kernel_reservations: MoneyMicros,
    unresolved_fee_upper_bounds: MoneyMicros,
    reconciliation_uncertainty_buffer: MoneyMicros,
) -> MoneyMicros:
    """Return the worst-case account equity, in micros (SPEC S10.4).

    Sums the two terms that can only *help* solvency (verified cash and the
    guaranteed terminal value of open positions) and subtracts the three that
    can only *hurt* it (capital already reserved by the kernel, the upper bound
    on unresolved fees, and the reconciliation-uncertainty buffer). The result
    is therefore a floor on equity: any real-world resolution of the uncertain
    terms leaves equity no lower than this.

    Args:
        exchange_verified_available_cash: Cash the exchange has confirmed is
            available, in micros.
        guaranteed_terminal_value_of_positions: The value open positions are
            guaranteed to be worth at resolution, in micros.
        pending_kernel_reservations: Capital the kernel has already reserved
            against in-flight approvals, in micros.
        unresolved_fee_upper_bounds: The upper bound on fees not yet finalized,
            in micros.
        reconciliation_uncertainty_buffer: A buffer covering state the kernel
            has not yet reconciled against the exchange, in micros.

    Returns:
        The worst-case equity, in micros.
    """
    return (
        exchange_verified_available_cash
        + guaranteed_terminal_value_of_positions
        - pending_kernel_reservations
        - unresolved_fee_upper_bounds
        - reconciliation_uncertainty_buffer
    )


def worst_case_cost(
    price: PricePips,
    size: ContractCentis,
    *,
    max_trading_fee: MoneyMicros,
    max_settlement_fee: MoneyMicros,
    rounding_buffer: MoneyMicros,
) -> MoneyMicros:
    """Return the worst-case cost of an order, in micros (SPEC S10.4).

    Adds the order notional to the worst-case trading fee, the worst-case
    settlement fee, and a rounding buffer. The notional is computed via
    :func:`money_from_price_and_count` with
    :data:`RoundingDirection.OVERSTATE_COST`, so that whenever a remainder must
    be dropped it is dropped toward a *larger* cost -- a cost may be overstated,
    never understated.

    Args:
        price: The order limit price, in pips.
        size: The order size, in contract-centis.
        max_trading_fee: The worst-case trading fee, in micros.
        max_settlement_fee: The worst-case settlement fee, in micros.
        rounding_buffer: A buffer absorbing residual rounding uncertainty, in
            micros.

    Returns:
        The worst-case cost, in micros.
    """
    notional = money_from_price_and_count(
        price, size, rounding=RoundingDirection.OVERSTATE_COST
    )
    return notional + max_trading_fee + max_settlement_fee + rounding_buffer
