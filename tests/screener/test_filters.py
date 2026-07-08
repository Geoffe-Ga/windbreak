"""Tests for `windbreak.screener.filters`, the real SPEC S16 filters (issue #21).

`windbreak.screener.filters` does not exist yet, so importing it fails
collection with `ModuleNotFoundError: No module named
'windbreak.screener.filters'` -- the expected Gate 1 RED state for issue #21.

Each filter is assumed to be a pure function taking the thing it screens plus
keyword-only integer thresholds, and returning a `FilterResult(passed,
measured)`:

    * `category_filter(market, *, blocklist, acknowledged_categories)` --
      blocked when `market.category` is in `blocklist`, OR the category is in
      the fail-closed `LEGALLY_RISKY_CATEGORIES` set and not present in
      `acknowledged_categories`. Config wins: a category present in both
      `blocklist` and `acknowledged_categories` stays blocked.
    * `min_volume_filter(stats, *, threshold_micros)` -- passes iff
      `stats.volume_24h_micros.value >= threshold_micros` (inclusive).
    * `min_depth_filter(stats, *, threshold_centis)` -- passes iff
      `stats.depth_contract_centis.value >= threshold_centis` (inclusive).
    * `horizon_filter(market, *, now, min_days, max_days)` -- computes the
      whole-day horizon as the integer floor of `(market.close_time - now)` in
      seconds, floor-divided by 86_400, and passes iff
      `min_days <= horizon <= max_days` (inclusive both ends).

All four are pure and deterministic: identical inputs always produce an
identical `FilterResult`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from windbreak.connector.models import NormalizedMarket
from windbreak.numeric import ContractCentis, MoneyMicros
from windbreak.screener.filters import (
    LEGALLY_RISKY_CATEGORIES,
    BookStats,
    category_filter,
    horizon_filter,
    min_depth_filter,
    min_volume_filter,
)

#: A fixed reference "now" for every horizon computation below, so every test
#: reasons about the same wall-clock moment.
_NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: A pool of categories spanning: an ordinary category, every default-config
#: blocklist entry, and the fail-closed legally-risky category ("sports" is
#: both).
_CATEGORY_POOL = (
    "economics",
    "politics",
    "sports",
    "crypto_price",
    "celebrity",
    "insider_prone",
)


def _market(
    *,
    category: str = "economics",
    close_time: datetime = _NOW + timedelta(days=30),
) -> NormalizedMarket:
    """Build a valid `NormalizedMarket` with only `category`/`close_time` varied.

    Mirrors the `_market` shape in `tests/connector/test_screener.py`; every
    other field is a fixed, closed-set-valid placeholder irrelevant to the
    filter under test.

    Args:
        category: The market's topical category.
        close_time: When the market closes.

    Returns:
        A `NormalizedMarket` instance.
    """
    return NormalizedMarket(
        exchange="fake-exchange",
        ticker="KXFED-24DEC",
        event_ticker="KXFED-24",
        title="Fed raises rates in December 2024?",
        resolution_criteria="Resolves YES if the FOMC raises rates.",
        category=category,
        close_time=close_time,
        expected_resolution_time=None,
        market_type="fully_collateralized_binary",
        price_tick_pips=100,
        min_order_contract_centis=100,
        fractional_trading_enabled=False,
        mutually_exclusive_group_id=None,
        jurisdiction_status="eligible",
        raw_exchange_payload_hash="sha256:abc123",
    )


# --- LEGALLY_RISKY_CATEGORIES ------------------------------------------------


def test_legally_risky_categories_contains_sports() -> None:
    """Sports is a fail-closed, legally-risky category regardless of config."""
    assert "sports" in LEGALLY_RISKY_CATEGORIES


# --- category_filter ----------------------------------------------------------


def test_category_absent_from_blocklist_and_not_risky_passes() -> None:
    """An ordinary category outside the blocklist and not risky passes."""
    market = _market(category="economics")

    result = category_filter(market, blocklist=(), acknowledged_categories=())

    assert result.passed is True
    assert result.measured == 0


def test_category_in_blocklist_is_blocked() -> None:
    """A non-risky category present in the configured blocklist is blocked."""
    market = _market(category="celebrity")

    result = category_filter(
        market, blocklist=("celebrity",), acknowledged_categories=()
    )

    assert result.passed is False
    assert result.measured == 1


def test_sports_removed_from_blocklist_without_ack_still_fails_closed() -> None:
    """Sports is legally risky: absence from the blocklist alone never opens it."""
    market = _market(category="sports")

    result = category_filter(market, blocklist=(), acknowledged_categories=())

    assert result.passed is False
    assert result.measured == 1


def test_sports_removed_from_blocklist_with_matching_ack_passes() -> None:
    """An explicit acknowledgement for sports lifts the fail-closed block."""
    market = _market(category="sports")

    result = category_filter(market, blocklist=(), acknowledged_categories=("sports",))

    assert result.passed is True
    assert result.measured == 0


def test_sports_in_blocklist_with_ack_present_still_blocked() -> None:
    """Config wins over acknowledgement: blocklist membership is never waived."""
    market = _market(category="sports")

    result = category_filter(
        market, blocklist=("sports",), acknowledged_categories=("sports",)
    )

    assert result.passed is False
    assert result.measured == 1


@given(
    category=st.sampled_from(_CATEGORY_POOL),
    blocklist=st.frozensets(st.sampled_from(_CATEGORY_POOL)),
    acknowledged=st.frozensets(st.sampled_from(_CATEGORY_POOL)),
)
def test_category_filter_is_deterministic(
    category: str, blocklist: frozenset[str], acknowledged: frozenset[str]
) -> None:
    """Calling `category_filter` twice with identical inputs is idempotent."""
    market = _market(category=category)

    first = category_filter(
        market, blocklist=blocklist, acknowledged_categories=acknowledged
    )
    second = category_filter(
        market, blocklist=blocklist, acknowledged_categories=acknowledged
    )

    assert first == second


# --- min_volume_filter ----------------------------------------------------------


def _stats_with_volume(volume: int) -> BookStats:
    """Build a `BookStats` with a given 24h volume and an irrelevant depth."""
    return BookStats(
        volume_24h_micros=MoneyMicros(volume),
        depth_contract_centis=ContractCentis(1),
    )


def test_min_volume_exactly_at_threshold_passes() -> None:
    """The threshold is inclusive: volume equal to it passes."""
    result = min_volume_filter(_stats_with_volume(1000), threshold_micros=1000)

    assert result.passed is True
    assert result.measured == 1000


def test_min_volume_one_below_threshold_is_blocked() -> None:
    """Volume one micro-dollar below the threshold is blocked."""
    result = min_volume_filter(_stats_with_volume(999), threshold_micros=1000)

    assert result.passed is False
    assert result.measured == 999


def test_min_volume_one_above_threshold_passes() -> None:
    """Volume one micro-dollar above the threshold passes."""
    result = min_volume_filter(_stats_with_volume(1001), threshold_micros=1000)

    assert result.passed is True
    assert result.measured == 1001


def test_min_volume_measured_is_a_plain_int() -> None:
    """`measured` is a plain `int`, never a wrapped unit type or bool."""
    result = min_volume_filter(_stats_with_volume(1000), threshold_micros=1000)

    assert type(result.measured) is int


@given(volume=st.integers(min_value=0, max_value=10_000_000_000))
def test_min_volume_filter_is_deterministic(volume: int) -> None:
    """Calling `min_volume_filter` twice with identical inputs is idempotent."""
    stats = _stats_with_volume(volume)

    first = min_volume_filter(stats, threshold_micros=1000)
    second = min_volume_filter(stats, threshold_micros=1000)

    assert first == second


# --- min_depth_filter ------------------------------------------------------


def _stats_with_depth(depth: int) -> BookStats:
    """Build a `BookStats` with a given book depth and an irrelevant volume."""
    return BookStats(
        volume_24h_micros=MoneyMicros(1),
        depth_contract_centis=ContractCentis(depth),
    )


def test_min_depth_exactly_at_threshold_passes() -> None:
    """The threshold is inclusive: depth equal to it passes."""
    result = min_depth_filter(_stats_with_depth(10000), threshold_centis=10000)

    assert result.passed is True
    assert result.measured == 10000


def test_min_depth_one_below_threshold_is_blocked() -> None:
    """Depth one contract-centi below the threshold is blocked."""
    result = min_depth_filter(_stats_with_depth(9999), threshold_centis=10000)

    assert result.passed is False
    assert result.measured == 9999


def test_min_depth_one_above_threshold_passes() -> None:
    """Depth one contract-centi above the threshold passes."""
    result = min_depth_filter(_stats_with_depth(10001), threshold_centis=10000)

    assert result.passed is True
    assert result.measured == 10001


def test_min_depth_measured_is_a_plain_int() -> None:
    """`measured` is a plain `int`, never a wrapped unit type or bool."""
    result = min_depth_filter(_stats_with_depth(10000), threshold_centis=10000)

    assert type(result.measured) is int


@given(depth=st.integers(min_value=0, max_value=1_000_000))
def test_min_depth_filter_is_deterministic(depth: int) -> None:
    """Calling `min_depth_filter` twice with identical inputs is idempotent."""
    stats = _stats_with_depth(depth)

    first = min_depth_filter(stats, threshold_centis=10000)
    second = min_depth_filter(stats, threshold_centis=10000)

    assert first == second


# --- horizon_filter --------------------------------------------------------


def _market_at_horizon(days: int, *, hours: int = 0) -> NormalizedMarket:
    """Build a market whose `close_time` is `days` (plus `hours`) after `_NOW`."""
    return _market(close_time=_NOW + timedelta(days=days, hours=hours))


def test_horizon_one_day_below_minimum_is_blocked() -> None:
    """A horizon one whole day short of the minimum is blocked."""
    market = _market_at_horizon(1)

    result = horizon_filter(market, now=_NOW, min_days=2, max_days=120)

    assert result.passed is False
    assert result.measured == 1


def test_horizon_exactly_at_minimum_passes() -> None:
    """The minimum bound is inclusive: horizon equal to it passes."""
    market = _market_at_horizon(2)

    result = horizon_filter(market, now=_NOW, min_days=2, max_days=120)

    assert result.passed is True
    assert result.measured == 2


def test_horizon_mid_range_passes() -> None:
    """A horizon comfortably inside the bounds passes."""
    market = _market_at_horizon(60)

    result = horizon_filter(market, now=_NOW, min_days=2, max_days=120)

    assert result.passed is True
    assert result.measured == 60


def test_horizon_exactly_at_maximum_passes() -> None:
    """The maximum bound is inclusive: horizon equal to it passes."""
    market = _market_at_horizon(120)

    result = horizon_filter(market, now=_NOW, min_days=2, max_days=120)

    assert result.passed is True
    assert result.measured == 120


def test_horizon_one_day_above_maximum_is_blocked() -> None:
    """A horizon one whole day beyond the maximum is blocked."""
    market = _market_at_horizon(121)

    result = horizon_filter(market, now=_NOW, min_days=2, max_days=120)

    assert result.passed is False
    assert result.measured == 121


def test_horizon_uses_whole_day_floor_not_ceiling() -> None:
    """A close_time 5 days and 23 hours out measures a horizon of 5, not 6.

    Pins the "integer floor of seconds // 86_400" semantics: the partial day
    must never round up.
    """
    market = _market_at_horizon(5, hours=23)

    result = horizon_filter(market, now=_NOW, min_days=2, max_days=120)

    assert result.passed is True
    assert result.measured == 5


@given(offset_days=st.integers(min_value=-30, max_value=200))
def test_horizon_filter_is_deterministic(offset_days: int) -> None:
    """Calling `horizon_filter` twice with identical inputs is idempotent."""
    market = _market_at_horizon(offset_days)

    first = horizon_filter(market, now=_NOW, min_days=2, max_days=120)
    second = horizon_filter(market, now=_NOW, min_days=2, max_days=120)

    assert first == second
