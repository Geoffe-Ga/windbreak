"""Sanctioned integer division with an explicit conservative-rounding direction.

windbreak forbids bare ``//`` on money/price/probability paths: every rounding
decision must be visible at the call site so an auditor can see which way a
remainder was dropped. :func:`divide` is the single approved integer division,
always paired with a :class:`RoundingDirection`.

Sign semantics (both directions are sign-safe for any operand signs):
    * ``OVERSTATE_COST`` always rounds toward positive infinity (ceiling),
      computed as ``-(-numerator // denominator)``. Used where over-counting is
      the safe error (e.g. the cost/liability side).
    * ``UNDERSTATE_EQUITY`` always rounds toward negative infinity (floor),
      computed as Python's native ``numerator // denominator``. Used where
      under-counting is the safe error (e.g. the equity/asset side).

No float ever appears in this module.
"""

from __future__ import annotations

import enum


class RoundingDirection(enum.Enum):
    """The two conservative-rounding directions used across windbreak.

    Attributes:
        OVERSTATE_COST: Round toward positive infinity (ceiling). Conservative
            for costs and liabilities, where erring high is the safe side.
        UNDERSTATE_EQUITY: Round toward negative infinity (floor). Conservative
            for equity and assets, where erring low is the safe side.
    """

    OVERSTATE_COST = enum.auto()
    UNDERSTATE_EQUITY = enum.auto()


def divide(numerator: int, denominator: int, *, rounding: RoundingDirection) -> int:
    """Divide two integers, rounding in the given conservative direction.

    The result is exact when ``denominator`` evenly divides ``numerator``;
    otherwise the remainder is dropped toward positive infinity for
    ``OVERSTATE_COST`` or toward negative infinity for ``UNDERSTATE_EQUITY``.
    Both directions are sign-safe: they behave correctly regardless of the sign
    of either operand (unlike a naive ``(n + d - 1) // d`` ceiling).

    Args:
        numerator: The dividend.
        denominator: The divisor. Must be non-zero.
        rounding: Required keyword-only rounding direction. Passing it
            positionally, or omitting it, raises ``TypeError``.

    Returns:
        The integer quotient rounded in the requested direction.

    Raises:
        ZeroDivisionError: If ``denominator`` is zero.
    """
    if denominator == 0:
        msg = "divide() denominator must be non-zero"
        raise ZeroDivisionError(msg)
    if rounding is RoundingDirection.OVERSTATE_COST:
        return -(-numerator // denominator)
    return numerator // denominator
