"""Shared component: numeric primitives for probabilities and prices.

Houses the deterministic fixed-point numeric helpers shared across the pipeline
and risk components: the four scaled-integer unit value types (:class:`PricePips`,
:class:`ContractCentis`, :class:`MoneyMicros`, :class:`ProbabilityPpm`), the
canonical :func:`money_from_price_and_count` conversion, and the sanctioned
:func:`divide` integer division with its :class:`RoundingDirection`. No float is
ever used on these money/price/probability paths (enforced by
``scripts/lint_no_floats.py``).
"""

from __future__ import annotations

from hedgekit.numeric.rounding import RoundingDirection, divide
from hedgekit.numeric.types import (
    ContractCentis,
    MoneyMicros,
    PricePips,
    ProbabilityPpm,
    money_from_price_and_count,
)

__all__ = [
    "ContractCentis",
    "MoneyMicros",
    "PricePips",
    "ProbabilityPpm",
    "RoundingDirection",
    "divide",
    "money_from_price_and_count",
]
