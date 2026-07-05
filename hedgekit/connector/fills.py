"""SPEC S17.4 pessimistic paper-fill primitives, in pure integer arithmetic.

This module is the normative kernel of hedgekit's paper-trading realism model:
a taker order walks the recorded order book best-first and never fills cheaper
than the book allows, and a resting order fills only when the recorded market
*trades through* its limit price -- a mere touch (a print exactly at the limit)
is never a fill. Every simulated cost is rounded *against* the trader: money
amounts overstate (ceiling), fill sizes understate (floor), so a backtest can
never flatter itself.

Two pessimism knobs, both in parts-per-million, default to 25%:

    * :data:`DEFAULT_MAX_PARTICIPATION_PPM` caps how much of the eligible
      recorded depth a single simulated order may consume (SPEC S9.5).
    * :data:`DEFAULT_FEE_HAIRCUT_PPM` adds a slippage haircut on top of the
      modeled fee (SPEC S17.4).

:data:`PAPER_FILL_MODEL_VERSION` is a sha256 digest of a canonical description
built from those defining constants (SPEC S13.6): it changes if and only if the
model itself changes, so a recorded backtest can be tied to the exact fill
model that produced it.

This package is float-denylisted by ``scripts/lint_no_floats.py``: there is no
``/`` true division, no ``float`` literal, cast, or annotation anywhere here.
All money/size math is integer arithmetic routed through
:func:`hedgekit.numeric.divide` and :func:`hedgekit.numeric.money_from_price_and_count`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from hedgekit.connector.models import OrderBookLevel
from hedgekit.numeric import (
    ContractCentis,
    MoneyMicros,
    RoundingDirection,
    divide,
    money_from_price_and_count,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from hedgekit.connector.fees import FeeModel
    from hedgekit.numeric import PricePips

#: Parts-per-million denominator: every ppm knob is scaled out by this divisor.
_PPM_DENOMINATOR: Final[int] = 1_000_000

#: Default slippage haircut added on top of the modeled fee: 25% (SPEC S17.4).
DEFAULT_FEE_HAIRCUT_PPM: Final[int] = 250_000

#: Default cap on how much recorded depth one simulated order may take: 25%
#: (SPEC S9.5).
DEFAULT_MAX_PARTICIPATION_PPM: Final[int] = 250_000

#: Money always rounds *up* (over-count cost); the trader never gets a break.
_MONEY_ROUNDING: Final[RoundingDirection] = RoundingDirection.OVERSTATE_COST

#: Fill sizes always round *down* (under-count equity); a cap never rounds up.
_SIZE_ROUNDING: Final[RoundingDirection] = RoundingDirection.UNDERSTATE_EQUITY

#: Canonical, human-readable description of the fill model, assembled from its
#: defining constants and rounding rules. Hashing this (rather than a hand-typed
#: literal) makes :data:`PAPER_FILL_MODEL_VERSION` change if and only if a
#: defining constant or documented behavior changes (SPEC S13.6).
_MODEL_CANON: Final[str] = ";".join(
    (
        "hedgekit.paper-fill-model",
        "spec=S17.4",
        f"default_fee_haircut_ppm={DEFAULT_FEE_HAIRCUT_PPM}",
        f"default_max_participation_ppm={DEFAULT_MAX_PARTICIPATION_PPM}",
        f"ppm_denominator={_PPM_DENOMINATOR}",
        f"money_rounding={_MONEY_ROUNDING.name}",
        f"size_rounding={_SIZE_ROUNDING.name}",
        "taker=walk-eligible-levels-best-first-capped-by-participation",
        "resting=trade-through-only-touch-never-fills-capped-by-participation",
    )
)

#: SPEC S13.6 model-version fingerprint: the sha256 hex digest of the canonical
#: model description. A 64-character lowercase hex string.
PAPER_FILL_MODEL_VERSION: Final[str] = hashlib.sha256(
    _MODEL_CANON.encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True)
class TradePrint:
    """A single recorded trade print used to fill resting orders.

    Attributes:
        price: The price the trade printed at, in pips.
        quantity: The traded size, in contract-centis.
        ts: When the trade printed.
    """

    price: PricePips
    quantity: ContractCentis
    ts: datetime


@dataclass(frozen=True, slots=True)
class TakerFillResult:
    """The outcome of walking the recorded book for a taker order.

    Attributes:
        consumed: The per-level slices actually taken, best-first; each carries
            the price walked and the quantity consumed at that price.
        filled: The total quantity filled, in contract-centis.
        book_cost: The recorded-book cost of the fill, in micros (exact sum of
            ``price * quantity`` per consumed level).
        fee: The modeled trading fee over the consumed levels, in micros.
        haircut: The slippage haircut added on top of ``fee``, in micros.
        total_cost: ``book_cost + fee + haircut``, in micros -- always an
            overstatement, per SPEC S17.4's pessimistic mandate.
    """

    consumed: tuple[OrderBookLevel, ...]
    filled: ContractCentis
    book_cost: MoneyMicros
    fee: MoneyMicros
    haircut: MoneyMicros
    total_cost: MoneyMicros


def participation_cap(
    depth: ContractCentis, *, max_participation_ppm: int
) -> ContractCentis:
    """Return the most of ``depth`` a single order may consume, floored.

    The cap is ``floor(depth * max_participation_ppm / 1_000_000)`` -- rounded
    *down* so a fractional cap never lets an order take more depth than its
    participation share allows (SPEC S9.5).

    Args:
        depth: The eligible recorded depth, in contract-centis.
        max_participation_ppm: The participation cap, in parts-per-million.

    Returns:
        The capped depth, in contract-centis.
    """
    capped = divide(
        depth.value * max_participation_ppm, _PPM_DENOMINATOR, rounding=_SIZE_ROUNDING
    )
    return ContractCentis(capped)


def _eligible_levels(
    levels: Sequence[OrderBookLevel], limit: PricePips
) -> tuple[OrderBookLevel, ...]:
    """Return the levels at-or-better than ``limit`` (price ``<=`` limit).

    Args:
        levels: The book side to filter, best-first.
        limit: The crossing order's limit price.

    Returns:
        The at-or-better levels, in their original best-first order.
    """
    return tuple(level for level in levels if level.price <= limit)


def _consume_levels(
    eligible: Sequence[OrderBookLevel], target: int, fee_model: FeeModel
) -> tuple[tuple[OrderBookLevel, ...], int, int]:
    """Walk ``eligible`` best-first, consuming up to ``target`` centis.

    The last consumed level may be taken only partially, landing the running
    total exactly on ``target``. Book cost overstates (ceiling, though the
    price-times-count product is always exact) and the fee is the sum of the
    per-level worst-case fee on each consumed slice. When ``target`` is zero the
    loop consumes nothing, so the fee model is never asked for a zero-size fee
    (which it rejects).

    Args:
        eligible: The at-or-better levels, best-first.
        target: The total quantity to consume, in contract-centis.
        fee_model: The schedule pricing each consumed slice's worst-case fee.

    Returns:
        A triple of the consumed slices, the total book cost in micros, and the
        total fee in micros.
    """
    consumed: list[OrderBookLevel] = []
    book_cost = 0
    fee = 0
    remaining = target
    for level in eligible:
        if remaining <= 0:
            break
        take = min(remaining, level.quantity.value)
        consumed.append(OrderBookLevel(level.price, ContractCentis(take)))
        book_cost += money_from_price_and_count(
            level.price, ContractCentis(take), rounding=_MONEY_ROUNDING
        ).value
        fee += fee_model.max_trading_fee_micros(level.price.value, take)
        remaining -= take
    return tuple(consumed), book_cost, fee


def walk_taker_fill(
    levels: Sequence[OrderBookLevel],
    limit: PricePips,
    requested: ContractCentis,
    fee_model: FeeModel,
    *,
    haircut_ppm: int,
    max_participation_ppm: int,
) -> TakerFillResult:
    """Walk the recorded book for a taker order, pessimistically (SPEC S17.4).

    Only levels at-or-better than ``limit`` are eligible; the fill is capped at
    ``max_participation_ppm`` of that eligible depth (floored) and by the
    requested size, then walked best-first across the eligible levels. Book
    cost, fee, and haircut are each rounded against the trader and summed into
    ``total_cost``. A cap that floors to zero yields a costless zero fill.

    Args:
        levels: The crossed book side, best-first (ascending price for a buy).
        limit: The order's limit price; levels worse than this are excluded.
        requested: The requested size, in contract-centis.
        fee_model: The fee schedule applied to each consumed slice.
        haircut_ppm: The slippage haircut on the modeled fee, in ppm.
        max_participation_ppm: The participation cap, in ppm (SPEC S9.5).

    Returns:
        The :class:`TakerFillResult` describing what filled and what it cost.
    """
    eligible = _eligible_levels(levels, limit)
    eligible_depth = ContractCentis(sum(level.quantity.value for level in eligible))
    allowed = participation_cap(
        eligible_depth, max_participation_ppm=max_participation_ppm
    )
    filled = min(requested.value, allowed.value)
    consumed, book_cost, fee = _consume_levels(eligible, filled, fee_model)
    haircut = divide(fee * haircut_ppm, _PPM_DENOMINATOR, rounding=_MONEY_ROUNDING)
    return TakerFillResult(
        consumed=consumed,
        filled=ContractCentis(filled),
        book_cost=MoneyMicros(book_cost),
        fee=MoneyMicros(fee),
        haircut=MoneyMicros(haircut),
        total_cost=MoneyMicros(book_cost + fee + haircut),
    )


def resting_fill_quantity(
    limit: PricePips,
    remaining: ContractCentis,
    prints: Sequence[TradePrint],
    depth_at_or_better: ContractCentis,
    *,
    max_participation_ppm: int,
) -> ContractCentis:
    """Return how much of a resting buy fills against a stream of trade prints.

    A print strictly *through* the limit (price ``<`` limit for a resting buy)
    contributes its full quantity; a print exactly at the limit is a touch and
    contributes nothing -- a touch is never a fill (SPEC S17.4). The realized
    fill is the smallest of the remaining order size, the participation cap on
    the recorded at-or-better depth, and the total trade-through volume.

    Args:
        limit: The resting order's limit price.
        remaining: The order's unfilled size, in contract-centis.
        prints: The recorded trade prints to test against the limit.
        depth_at_or_better: Recorded same-side depth at prices at-or-better than
            the limit, feeding the participation cap.
        max_participation_ppm: The participation cap, in ppm (SPEC S9.5).

    Returns:
        The realized fill quantity, in contract-centis.
    """
    through_volume = sum(
        print_.quantity.value for print_ in prints if print_.price < limit
    )
    cap = participation_cap(
        depth_at_or_better, max_participation_ppm=max_participation_ppm
    )
    return ContractCentis(min(remaining.value, cap.value, through_volume))
