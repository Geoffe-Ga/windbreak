"""The four pure market-eligibility filters of the real §16 screener.

Each filter is a small, pure, deterministic function: it takes the thing it
screens (a :class:`~hedgekit.connector.models.NormalizedMarket` or a
:class:`BookStats`) plus keyword-only integer thresholds, and returns a
:class:`FilterResult` carrying a ``passed`` verdict and the plain-``int``
``measured`` quantity that drove it. Identical inputs always yield an identical
result.

The filters embody SPEC §16's screener policy:

    * :func:`category_filter` -- blocks configured categories and fail-closes on
      the legally-risky categories in :data:`LEGALLY_RISKY_CATEGORIES` unless
      explicitly acknowledged. Config always wins: a category in the blocklist
      stays blocked even when acknowledged.
    * :func:`min_volume_filter` -- an inclusive 24h-volume floor.
    * :func:`min_depth_filter` -- an inclusive book-depth floor.
    * :func:`horizon_filter` -- an inclusive whole-day resolution-horizon window,
      measured by integer floor so a partial day never rounds up.

Every ``measured`` value is a bare ``int`` off a scaled-integer unit's
``.value``; no float ever appears, satisfying ``scripts/lint_no_floats.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Collection
    from datetime import datetime

    from hedgekit.connector.models import NormalizedMarket
    from hedgekit.numeric import ContractCentis, MoneyMicros

#: Canonical filter name (and SCREEN_DECISION key) for the category filter.
CATEGORY_BLOCKLIST: Final = "category_blocklist"

#: Canonical filter name (and SCREEN_DECISION key) for the 24h-volume floor.
MIN_VOLUME_24H: Final = "min_volume_24h_micros"

#: Canonical filter name (and SCREEN_DECISION key) for the book-depth floor.
MIN_DEPTH: Final = "min_depth_contract_centis"

#: Canonical filter name (and SCREEN_DECISION key) for the horizon window.
HORIZON_DAYS: Final = "horizon_days"

#: Categories that fail closed: blocked unless explicitly acknowledged, even
#: when absent from the configured blocklist. Trading these carries legal risk
#: an operator must consciously accept (SPEC §16).
LEGALLY_RISKY_CATEGORIES: Final[frozenset[str]] = frozenset({"sports"})

#: Seconds in one whole day, the divisor for the integer horizon floor.
_SECONDS_PER_DAY: Final = 86_400


@dataclass(frozen=True, slots=True)
class BookStats:
    """Order-book liquidity statistics for a single market.

    Attributes:
        volume_24h_micros: Trailing 24-hour traded volume, in micro-dollars.
        depth_contract_centis: Resting book depth, in contract-centis.
    """

    volume_24h_micros: MoneyMicros
    depth_contract_centis: ContractCentis


@dataclass(frozen=True, slots=True)
class FilterResult:
    """The outcome of applying one filter to a market.

    Attributes:
        passed: Whether the market cleared this filter.
        measured: The plain-``int`` quantity the verdict was drawn from (e.g.
            the measured volume, depth, or whole-day horizon; ``1``/``0`` as a
            blocked/allowed flag for the category filter).
    """

    passed: bool
    measured: int


def category_filter(
    market: NormalizedMarket,
    *,
    blocklist: Collection[str],
    acknowledged_categories: Collection[str],
) -> FilterResult:
    """Screen a market on its topical category.

    The market is blocked when its category is in ``blocklist``, or when the
    category is legally risky (in :data:`LEGALLY_RISKY_CATEGORIES`) and has not
    been acknowledged. Config wins: blocklist membership is never waived by an
    acknowledgement.

    Args:
        market: The market to screen.
        blocklist: Configured categories to block outright.
        acknowledged_categories: Legally-risky categories the operator has
            explicitly accepted.

    Returns:
        A :class:`FilterResult` whose ``measured`` is ``1`` when blocked and
        ``0`` when allowed.
    """
    category = market.category
    blocked = category in blocklist or (
        category in LEGALLY_RISKY_CATEGORIES and category not in acknowledged_categories
    )
    return FilterResult(passed=not blocked, measured=1 if blocked else 0)


def min_volume_filter(stats: BookStats, *, threshold_micros: int) -> FilterResult:
    """Screen a market against an inclusive 24-hour volume floor.

    Args:
        stats: The market's order-book statistics.
        threshold_micros: The minimum acceptable 24h volume, in micro-dollars.

    Returns:
        A :class:`FilterResult` that passes iff the measured volume is greater
        than or equal to ``threshold_micros``.
    """
    measured = stats.volume_24h_micros.value
    return FilterResult(passed=measured >= threshold_micros, measured=measured)


def min_depth_filter(stats: BookStats, *, threshold_centis: int) -> FilterResult:
    """Screen a market against an inclusive book-depth floor.

    Args:
        stats: The market's order-book statistics.
        threshold_centis: The minimum acceptable depth, in contract-centis.

    Returns:
        A :class:`FilterResult` that passes iff the measured depth is greater
        than or equal to ``threshold_centis``.
    """
    measured = stats.depth_contract_centis.value
    return FilterResult(passed=measured >= threshold_centis, measured=measured)


def horizon_filter(
    market: NormalizedMarket,
    *,
    now: datetime,
    min_days: int,
    max_days: int,
) -> FilterResult:
    """Screen a market against an inclusive whole-day resolution-horizon window.

    The horizon is the number of whole days between ``now`` and the market's
    close time, computed as the integer floor of the elapsed seconds divided by
    the seconds in a day: a partial day never rounds up. ``total_seconds()``
    yields a float, so it is truncated to an ``int`` *before* the floor-division
    -- no true division or float ever touches the money path.

    Args:
        market: The market to screen.
        now: The reference "now" the horizon is measured from.
        min_days: The inclusive lower bound of the acceptable window, in days.
        max_days: The inclusive upper bound of the acceptable window, in days.

    Returns:
        A :class:`FilterResult` whose ``measured`` is the whole-day horizon and
        which passes iff ``min_days <= horizon <= max_days``.
    """
    delta = market.close_time - now
    horizon = int(delta.total_seconds()) // _SECONDS_PER_DAY
    return FilterResult(passed=min_days <= horizon <= max_days, measured=horizon)
