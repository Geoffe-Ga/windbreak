"""SPEC S6.2 fee schedules and their worst-case fee bounds, in integer micros.

A :class:`FeeModel` carries a schedule's maker/taker/settlement rates in
parts-per-million and answers two upper-bound questions used by the risk and
sizing paths:

    * :meth:`FeeModel.max_trading_fee_micros` -- the worst-case per-trade fee at
      a given price and size, using the higher of the maker/taker rate applied
      to the quadratic ``count * price * (1 - price)`` bound (the most any fill
      at that price/size could cost).
    * :meth:`FeeModel.max_settlement_fee_micros` -- the worst-case settlement
      fee over a ``$1``/contract payout.

Both round *up* to the next whole cent (fees round in the venue's favor, so an
upper bound must too) and return a plain ``int`` count of micros. Every step is
integer arithmetic routed through :func:`hedgekit.numeric.divide`: this package
is float-denylisted by ``scripts/lint_no_floats.py``, so there is no ``/``, no
``float`` literal, and no ``float`` annotation anywhere here.
"""

from __future__ import annotations

from dataclasses import dataclass

from hedgekit.numeric import RoundingDirection, divide

#: Pips per payout-dollar: a price is in units of 1e-4 ($/contract), so a full
#: ``$1`` payout is 10_000 pips and ``(10_000 - price_pips)`` is the pip-scaled
#: complement ``(1 - price)`` used by the quadratic bound.
_PIPS_PER_DOLLAR = 10_000

#: Inclusive price bounds, in pips: 0 (``$0.0000``) to a full ``$1`` payout.
_MIN_PRICE_PIPS = 0
_MAX_PRICE_PIPS = _PIPS_PER_DOLLAR

#: Micros per cent: a cent is 1e-2 dollars and a micro is 1e-6 dollars, so one
#: cent is 10_000 micros. Fee bounds are computed in whole cents then scaled up.
_MICROS_PER_CENT = 10_000

#: Denominator taking the trading-fee numerator to whole cents. The numerator
#: ``rate_ppm * count_centis * price_pips * (10_000 - price_pips)`` carries the
#: scale product 1e-6 (ppm) * 1e-2 (centis) * 1e-4 (pips) * 1e-4 (pips) = 1e-16
#: dollars; multiplying by 100 to reach cents leaves a 1e-14 divisor.
_TRADING_CENT_DENOMINATOR = 10**14

#: Denominator taking the settlement-fee numerator to whole cents. The numerator
#: ``settlement_fee_ppm * count_centis`` (over a ``$1`` payout) carries the scale
#: 1e-6 (ppm) * 1e-2 (centis) = 1e-8 dollars; multiplying by 100 to reach cents
#: leaves a 1e-6 divisor.
_SETTLEMENT_CENT_DENOMINATOR = 10**6

#: Rounding used for every fee bound: over-count so an upper bound is never
#: understated (fees always round in the exchange's favor).
_FEE_ROUNDING = RoundingDirection.OVERSTATE_COST


class UnknownFeeModelError(RuntimeError):
    """Raised when a fee schedule cannot be resolved or parsed.

    A venue integration must fail closed on an unrecognized series, an
    unreachable schedule, or a schedule whose shape it does not understand --
    misreading a fee schedule is worse than admitting ignorance.
    """


def _require_non_bool_int(value: int, field_name: str) -> None:
    """Guard that ``value`` is a true, non-``bool`` integer.

    A ``bool`` is an ``int`` subclass, so ``True``/``False`` must be rejected
    before any numeric use lest a stray boolean masquerade as ``1``/``0``.

    Args:
        value: The candidate integer.
        field_name: The owning field's name, surfaced in the error message.

    Raises:
        TypeError: If ``value`` is a ``bool`` or is not an ``int``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{field_name} must be a non-bool int, got {type(value).__name__}"
        )


def _require_non_negative_ppm(value: int, field_name: str) -> None:
    """Guard that a ppm rate field is a non-``bool`` int and non-negative.

    Args:
        value: The candidate ppm rate.
        field_name: The owning field's name, surfaced in the error message.

    Raises:
        TypeError: If ``value`` is a ``bool`` or not an ``int``.
        ValueError: If ``value`` is negative (zero is allowed: a zero fee).
    """
    _require_non_bool_int(value, field_name)
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative, got {value}")


def _require_positive_count(value: int) -> None:
    """Guard that a contract-centis count is a non-``bool`` int and positive.

    Args:
        value: The candidate contract-centis count.

    Raises:
        TypeError: If ``value`` is a ``bool`` or not an ``int``.
        ValueError: If ``value`` is not strictly positive.
    """
    _require_non_bool_int(value, "count_centis")
    if value <= 0:
        raise ValueError(f"count_centis must be positive, got {value}")


def _require_price_pips(value: int) -> None:
    """Guard that a price is a non-``bool`` int within ``[0, 10_000]`` pips.

    Args:
        value: The candidate price, in pips.

    Raises:
        TypeError: If ``value`` is a ``bool`` or not an ``int``.
        ValueError: If ``value`` is outside the inclusive pip range.
    """
    _require_non_bool_int(value, "price_pips")
    if not _MIN_PRICE_PIPS <= value <= _MAX_PRICE_PIPS:
        raise ValueError(
            f"price_pips must be in [{_MIN_PRICE_PIPS}, {_MAX_PRICE_PIPS}], "
            f"got {value}"
        )


@dataclass(frozen=True, slots=True)
class FeeModel:
    """A fee schedule's maker/taker/settlement rates, in parts-per-million.

    Attributes:
        schedule_id: The schedule's identifier; must be non-empty.
        maker_fee_ppm: Maker fee, in ppm (a non-negative, non-``bool`` int).
        taker_fee_ppm: Taker fee, in ppm (a non-negative, non-``bool`` int).
        settlement_fee_ppm: Settlement fee, in ppm (a non-negative int).
    """

    schedule_id: str
    maker_fee_ppm: int
    taker_fee_ppm: int
    settlement_fee_ppm: int

    def __post_init__(self) -> None:
        """Validate the identifier and every ppm rate at construction.

        Raises:
            TypeError: If a ppm field is a ``bool`` or non-``int``.
            ValueError: If ``schedule_id`` is empty or a ppm field is negative.
        """
        if not self.schedule_id:
            raise ValueError("schedule_id must be non-empty")
        _require_non_negative_ppm(self.maker_fee_ppm, "maker_fee_ppm")
        _require_non_negative_ppm(self.taker_fee_ppm, "taker_fee_ppm")
        _require_non_negative_ppm(self.settlement_fee_ppm, "settlement_fee_ppm")

    def max_trading_fee_micros(self, price_pips: int, count_centis: int) -> int:
        """Return the worst-case per-trade fee, in micros, at a price and size.

        Uses the higher of the maker/taker rate against the quadratic bound
        ``count * price * (1 - price)`` -- the maximum any resting or crossing
        fill at that price/size could cost -- then rounds up to a whole cent.
        The exact-integer numerator carries the combined scale 1e-16 dollars;
        dividing by :data:`_TRADING_CENT_DENOMINATOR` yields whole cents, scaled
        to micros by :data:`_MICROS_PER_CENT`.

        Args:
            price_pips: The fill price, in pips; must be within ``[0, 10_000]``.
            count_centis: The fill size, in contract-centis; must be positive.

        Returns:
            The ceiling-rounded upper-bound fee, in micros (a plain ``int``).

        Raises:
            TypeError: If either argument is a ``bool`` or non-``int``.
            ValueError: If ``price_pips`` is out of range or ``count_centis`` is
                not positive.
        """
        _require_price_pips(price_pips)
        _require_positive_count(count_centis)
        rate_ppm = max(self.maker_fee_ppm, self.taker_fee_ppm)
        numerator = (
            rate_ppm * count_centis * price_pips * (_PIPS_PER_DOLLAR - price_pips)
        )
        cents = divide(numerator, _TRADING_CENT_DENOMINATOR, rounding=_FEE_ROUNDING)
        return cents * _MICROS_PER_CENT

    def max_settlement_fee_micros(self, count_centis: int) -> int:
        """Return the worst-case settlement fee, in micros, for a size.

        Bounds the fee over a ``$1``/contract payout: the exact-integer
        numerator ``settlement_fee_ppm * count_centis`` carries the scale 1e-8
        dollars, so dividing by :data:`_SETTLEMENT_CENT_DENOMINATOR` yields whole
        cents, scaled to micros by :data:`_MICROS_PER_CENT`, rounded up.

        Args:
            count_centis: The settled size, in contract-centis; must be positive.

        Returns:
            The ceiling-rounded upper-bound fee, in micros (a plain ``int``).

        Raises:
            TypeError: If ``count_centis`` is a ``bool`` or non-``int``.
            ValueError: If ``count_centis`` is not positive.
        """
        _require_positive_count(count_centis)
        numerator = self.settlement_fee_ppm * count_centis
        cents = divide(numerator, _SETTLEMENT_CENT_DENOMINATOR, rounding=_FEE_ROUNDING)
        return cents * _MICROS_PER_CENT
