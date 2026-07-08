"""Fixed-point unit value types for windbreak's money/price/probability paths.

SPEC S6.1 requires that dollars, prices, contract counts, and probabilities are
carried as scaled integers -- never floats -- and that a value in one unit can
never be silently mixed with a value in another. This module defines four such
types, each a frozen wrapper around a single ``.value: int``:

    * :class:`PricePips`      -- payout-dollars in units of 1e-4 ($/contract).
    * :class:`ContractCentis` -- contracts in units of 1e-2.
    * :class:`MoneyMicros`    -- dollars in units of 1e-6.
    * :class:`ProbabilityPpm` -- probability in units of 1e-6 (parts per million).

Decision rationale -- why a frozen wrapper, not the obvious alternatives:

    * ``NewType`` was rejected because it is a compile-time-only alias: at
      runtime it *is* an ``int``, so ``PricePips(1) + ContractCentis(1)``
      type-checks and silently produces a bare ``int``, and any arithmetic
      "decays" back to ``int``. That defeats the whole unit-isolation goal.
    * Subclassing ``int`` was rejected because narrowing ``__add__`` to
      ``(self, other: Self) -> Self`` is a Liskov violation that ``mypy
      --strict`` rejects: ``int.__add__`` accepts any ``int``. The only way to
      silence it is a banned ``type: ignore``.

    The frozen wrapper gives nominal distinctness with none of that: each type
    is its own class, cross-unit arithmetic is *both* a mypy error *and* a
    runtime ``TypeError``, and because ``__truediv__`` is deliberately never
    defined, ``PricePips(4) / PricePips(2)`` is impossible even at runtime --
    the type-level enforcement of "no floats on the money path".

All display formatting uses integer ``divmod`` on the absolute value with the
sign handled separately, so floor-division-on-negatives can never corrupt a
digit and no float ever appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from windbreak.numeric.rounding import divide

if TYPE_CHECKING:
    from typing import Self

    from windbreak.numeric.rounding import RoundingDirection


def _format_fixed_point(value: int, *, decimals: int, prefix: str, suffix: str) -> str:
    """Render a scaled integer as fixed-point text using integer math only.

    The sign is prefixed separately from a ``divmod`` on ``abs(value)`` so that
    negative values never corrupt a fractional digit, and the whole and
    fractional parts are split at ``10 ** decimals``. No float is involved.

    Args:
        value: The scaled integer to render.
        decimals: Number of fractional digits (must be positive for every
            windbreak unit).
        prefix: Text placed after the optional sign and before the number
            (e.g. ``"$"``), or empty.
        suffix: Text placed after the number (e.g. ``"%"``), or empty.

    Returns:
        The formatted string, e.g. ``"-$0.500000"`` for value ``-500_000`` with
        six decimals and a ``"$"`` prefix.
    """
    scale = 10**decimals
    whole, fraction = divmod(abs(value), scale)
    sign = "-" if value < 0 else ""
    return f"{sign}{prefix}{whole}.{fraction:0{decimals}d}{suffix}"


@dataclass(frozen=True, slots=True)
class _IntUnit:
    """Shared frozen, slotted base for the fixed-point unit value types.

    Subclasses inherit construction validation, within-unit arithmetic and
    ordering, and cross-unit isolation, and add only a unit-specific
    :meth:`__str__`. The base is nominally distinct from ``int`` and is neither
    a ``NewType`` nor an ``int`` subclass (see the module docstring).

    Attributes:
        value: The scaled integer payload.
    """

    value: int

    def __post_init__(self) -> None:
        """Validate that ``value`` is a true, non-bool integer.

        Raises:
            TypeError: If ``value`` is a ``bool`` (an ``int`` subclass that must
                not slip through) or is not an ``int`` at all.
        """
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError(
                f"{type(self).__name__} requires a non-bool int, "
                f"got {type(self.value).__name__}"
            )

    def _require_same_unit(self, other: Self) -> None:
        """Guard that ``other`` is exactly this unit type, for arithmetic.

        Args:
            other: The right-hand operand.

        Raises:
            TypeError: If ``other`` is a different unit type. Mixing units
                (e.g. price plus contract count) is nonsensical and must fail
                loudly rather than produce a garbage integer.
        """
        if type(self) is not type(other):
            raise TypeError(
                f"cannot combine {type(self).__name__} with {type(other).__name__}"
            )

    def __add__(self, other: Self) -> Self:
        """Return the within-unit sum as the same unit type."""
        self._require_same_unit(other)
        return type(self)(self.value + other.value)

    def __sub__(self, other: Self) -> Self:
        """Return the within-unit difference as the same unit type."""
        self._require_same_unit(other)
        return type(self)(self.value - other.value)

    def __neg__(self) -> Self:
        """Return the negation as the same unit type."""
        return type(self)(-self.value)

    def __mul__(self, scalar: int) -> Self:
        """Return this value scaled by a dimensionless integer, same unit type.

        Scalar multiplication is by a plain ``int`` factor (e.g. a repeat
        count), never by another unit value: multiplying two unit values
        would change the dimension (contracts times contracts is not
        contracts) and is deliberately unsupported. ``bool`` is rejected for
        the same reason construction rejects it -- a stray boolean must never
        masquerade as the factor ``1``.

        Args:
            scalar: A dimensionless integer factor.

        Returns:
            The scaled value as the same unit type.

        Raises:
            TypeError: If ``scalar`` is a ``bool``, a ``float``, another unit
                value, or anything other than a true ``int``.
        """
        if isinstance(scalar, bool) or not isinstance(scalar, int):
            raise TypeError(
                f"cannot multiply {type(self).__name__} by "
                f"{type(scalar).__name__}; scalar must be a non-bool int"
            )
        return type(self)(self.value * scalar)

    def __rmul__(self, scalar: int) -> Self:
        """Return ``scalar * self``; scalar multiplication is commutative."""
        return self.__mul__(scalar)

    def __lt__(self, other: Self) -> bool:
        """Order by scaled value; only defined within a single unit."""
        self._require_same_unit(other)
        return self.value < other.value

    def __le__(self, other: Self) -> bool:
        """Order by scaled value; only defined within a single unit."""
        self._require_same_unit(other)
        return self.value <= other.value

    def __gt__(self, other: Self) -> bool:
        """Order by scaled value; only defined within a single unit."""
        self._require_same_unit(other)
        return self.value > other.value

    def __ge__(self, other: Self) -> bool:
        """Order by scaled value; only defined within a single unit."""
        self._require_same_unit(other)
        return self.value >= other.value


class PricePips(_IntUnit):
    """Payout-dollar price in units of 1e-4 (pips); e.g. 4567 == ``$0.4567``.

    A plain (non-redecorated) subclass keeps the base dataclass's ``__init__``,
    ``__eq__``, and ``__hash__`` while remaining a nominally distinct type;
    ``__slots__ = ()`` preserves the base's slotted, dict-free layout.
    """

    __slots__ = ()

    def __str__(self) -> str:
        """Render as dollars with four decimals, e.g. ``$0.4567``."""
        return _format_fixed_point(self.value, decimals=4, prefix="$", suffix="")


class ContractCentis(_IntUnit):
    """Contract count in units of 1e-2 (centis); e.g. 300 == ``3.00``."""

    __slots__ = ()

    def __str__(self) -> str:
        """Render as a bare number with two decimals, e.g. ``3.00``."""
        return _format_fixed_point(self.value, decimals=2, prefix="", suffix="")


class MoneyMicros(_IntUnit):
    """Dollar amount in units of 1e-6 (micros); e.g. 1_370_100 == ``$1.370100``."""

    __slots__ = ()

    def __str__(self) -> str:
        """Render as dollars with six decimals, e.g. ``$1.370100``."""
        return _format_fixed_point(self.value, decimals=6, prefix="$", suffix="")


class ProbabilityPpm(_IntUnit):
    """Probability in units of 1e-6 (ppm); shown as a percent, e.g. ``45.6700%``.

    A ppm value is 1e-6 probability, so as a percentage it is
    ``value / 1e6 * 100 == value / 1e4`` -- four decimal places with a ``%``
    suffix (e.g. 456_700 ppm == ``45.6700%``).
    """

    __slots__ = ()

    def __str__(self) -> str:
        """Render as a percentage with four decimals, e.g. ``45.6700%``."""
        return _format_fixed_point(self.value, decimals=4, prefix="", suffix="%")


def money_from_price_and_count(
    price: PricePips,
    count: ContractCentis,
    *,
    rounding: RoundingDirection,
) -> MoneyMicros:
    """Convert a price and a contract count into a money amount.

    Unit derivation: ``price`` is in pips (1e-4 $/contract) and ``count`` is in
    centis (1e-2 contracts), so ``price * count`` has scale
    ``1e-4 * 1e-2 == 1e-6``, which is exactly the micros unit of
    :class:`MoneyMicros`. The result in micros is therefore the *exact* integer
    product ``price.value * count.value`` -- there is never a remainder to
    round. ``rounding`` still routes through :func:`divide` (with denominator 1)
    so the conservative-rounding API is used uniformly on every money path, even
    where it is inert; both directions yield the identical value.

    Args:
        price: The price, in pips.
        count: The contract count, in centis.
        rounding: Required keyword-only rounding direction (inert here because
            the product is exact, but part of the sanctioned API surface).

    Returns:
        The money amount, in micros.
    """
    micros = divide(price.value * count.value, 1, rounding=rounding)
    return MoneyMicros(micros)
