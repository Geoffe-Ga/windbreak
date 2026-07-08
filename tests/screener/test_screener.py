"""Tests for `windbreak.screener.screener.Screener`, the real §16 screener
(issue #21).

`windbreak.screener.screener` does not exist yet, so importing it fails
collection with `ModuleNotFoundError: No module named
'windbreak.screener.screener'` -- the expected Gate 1 RED state for issue #21.

The production API these tests pin:

    * `Screener(config, writer, *, clock, acknowledgements=())` -- `config` is
      a `windbreak.config.ScreenerConfig`, `writer` is an
      `windbreak.connector.snapshot.EventLedgerWriter`, `clock` is a
      zero-argument callable returning the current `datetime` ("now"),
      `acknowledgements` is a tuple of `LegalRiskAcknowledgement`. At
      construction, exactly one `LEGAL_RISK_ACK` event is emitted per supplied
      acknowledgement.
    * `Screener.screen(market, stats) -> ScreenResult` -- runs all four
      filters (`category_filter`, `min_volume_filter`, `min_depth_filter`,
      `horizon_filter`) against `config`/`clock()`, and appends exactly one
      `SCREEN_DECISION` event to `writer` whose JSON-safe payload has exactly
      the keys `ticker`, `eligible`, `blocked_by`, and `filters` (a mapping of
      each of the four canonical filter names to
      `{"passed": bool, "measured": int}`, never a float).
    * `ScreenResult(ticker, eligible, blocked_by, filters)`,
      `LegalRiskAcknowledgement(category, reason)`, and the filter-module's
      `BookStats`/`FilterResult` are all frozen, slotted dataclasses.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from windbreak.config import HorizonDays, ScreenerConfig
from windbreak.connector.models import NormalizedMarket
from windbreak.connector.snapshot import (
    SCREEN_DECISION_EVENT,
    InMemoryEventLedgerWriter,
)
from windbreak.numeric import ContractCentis, MoneyMicros
from windbreak.screener.filters import (
    CATEGORY_BLOCKLIST,
    HORIZON_DAYS,
    MIN_DEPTH,
    MIN_VOLUME_24H,
    BookStats,
    FilterResult,
    category_filter,
    horizon_filter,
    min_depth_filter,
    min_volume_filter,
)
from windbreak.screener.screener import (
    LEGAL_RISK_ACK_EVENT,
    LegalRiskAcknowledgement,
    Screener,
    ScreenResult,
)

#: A fixed reference "now"; every test's `clock` returns this constant.
_NOW = datetime(2026, 1, 1, tzinfo=UTC)

#: The canonical filter-name order `blocked_by` must respect.
_CANONICAL_ORDER = (CATEGORY_BLOCKLIST, MIN_VOLUME_24H, MIN_DEPTH, HORIZON_DAYS)

#: A pool of categories spanning an ordinary category, every default-config
#: blocklist entry, and the fail-closed legally-risky category.
_CATEGORY_POOL = (
    "economics",
    "politics",
    "sports",
    "crypto_price",
    "celebrity",
    "insider_prone",
)


def _clock() -> datetime:
    """Return the fixed reference "now" used by every `Screener` under test."""
    return _NOW


def _market(
    *,
    category: str = "economics",
    close_time: datetime = _NOW + timedelta(days=30),
) -> NormalizedMarket:
    """Build a valid `NormalizedMarket` with only `category`/`close_time` varied.

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


def _passing_stats(config: ScreenerConfig) -> BookStats:
    """Build `BookStats` that exactly clear `config`'s volume and depth floors."""
    return BookStats(
        volume_24h_micros=MoneyMicros(config.min_volume_24h_micros),
        depth_contract_centis=ContractCentis(config.min_depth_contract_centis),
    )


def _failing_stats() -> BookStats:
    """Build `BookStats` with zero volume and zero depth: fails both filters."""
    return BookStats(
        volume_24h_micros=MoneyMicros(0),
        depth_contract_centis=ContractCentis(0),
    )


# --- issue's exact examples ---------------------------------------------------


def test_sports_market_is_blocked_by_category_filter_alone() -> None:
    """A sports market under default config is blocked by category alone."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    market = _market(category="sports", close_time=_NOW + timedelta(days=30))
    stats = _passing_stats(config)

    result = screener.screen(market, stats)

    assert result.eligible is False
    assert result.blocked_by == (CATEGORY_BLOCKLIST,)


def test_thin_depth_market_ledgers_a_failing_min_depth_filter_entry() -> None:
    """A thin-depth market's ledgered decision records a failing depth entry."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    market = _market(category="economics", close_time=_NOW + timedelta(days=30))
    stats = BookStats(
        volume_24h_micros=MoneyMicros(config.min_volume_24h_micros),
        depth_contract_centis=ContractCentis(config.min_depth_contract_centis - 1),
    )

    screener.screen(market, stats)

    payload = writer.events_by_type(SCREEN_DECISION_EVENT)[-1].payload
    assert payload["filters"][MIN_DEPTH]["passed"] is False
    assert type(payload["filters"][MIN_DEPTH]["measured"]) is int


# --- SCREEN_DECISION event shape and cardinality ------------------------------


def test_screen_appends_exactly_one_screen_decision_event_per_call() -> None:
    """Every `screen()` call appends exactly one `SCREEN_DECISION` event."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    market = _market()
    stats = _passing_stats(config)

    screener.screen(market, stats)
    screener.screen(market, stats)
    screener.screen(market, stats)

    assert len(writer.events_by_type(SCREEN_DECISION_EVENT)) == 3


def test_screen_decision_payload_has_exactly_the_expected_keys() -> None:
    """The payload has exactly `ticker`, `eligible`, `blocked_by`, `filters`."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    market = _market()
    stats = _passing_stats(config)

    screener.screen(market, stats)

    payload = writer.events_by_type(SCREEN_DECISION_EVENT)[-1].payload
    assert set(payload) == {"ticker", "eligible", "blocked_by", "filters"}
    assert payload["ticker"] == market.ticker


def test_screen_decision_filters_mapping_has_exactly_the_four_names() -> None:
    """`filters` has exactly the four canonical filter names, no more, no fewer."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    market = _market()
    stats = _passing_stats(config)

    screener.screen(market, stats)

    payload = writer.events_by_type(SCREEN_DECISION_EVENT)[-1].payload
    assert set(payload["filters"]) == set(_CANONICAL_ORDER)


def test_screen_decision_each_filter_entry_has_passed_and_measured_only() -> None:
    """Each filter entry is exactly `{"passed": bool, "measured": int}`."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    market = _market(category="sports")
    stats = _failing_stats()

    screener.screen(market, stats)

    payload = writer.events_by_type(SCREEN_DECISION_EVENT)[-1].payload
    for entry in payload["filters"].values():
        assert set(entry) == {"passed", "measured"}
        assert isinstance(entry["passed"], bool)
        assert type(entry["measured"]) is int


def test_eligible_is_true_only_when_every_filter_passes() -> None:
    """`eligible` is True iff all four filters passed."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)

    passing = screener.screen(_market(), _passing_stats(config))
    assert passing.eligible is True

    failing = screener.screen(_market(category="sports"), _passing_stats(config))
    assert failing.eligible is False


def test_blocked_by_lists_failing_filters_in_canonical_order() -> None:
    """`blocked_by` lists every failing filter in canonical filter order."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    # Sports fails category; zero volume/depth fail both floors; close_time
    # equal to `_NOW` gives a zero-day horizon, below the default minimum.
    market = _market(category="sports", close_time=_NOW)
    stats = _failing_stats()

    result = screener.screen(market, stats)

    assert result.blocked_by == _CANONICAL_ORDER


def test_ledgered_blocked_by_matches_canonical_order_when_every_filter_fails() -> None:
    """The ledgered `blocked_by` equals the canonical order when all filters fail."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    market = _market(category="sports", close_time=_NOW)
    stats = _failing_stats()

    screener.screen(market, stats)

    payload = writer.events_by_type(SCREEN_DECISION_EVENT)[-1].payload
    assert payload["blocked_by"] == list(_CANONICAL_ORDER)


def test_ledgered_blocked_by_names_only_the_failing_filters_in_order() -> None:
    """A partial failure ledgers exactly the failing filters, in canonical order.

    Pins the ledgered `blocked_by` (the honest audit trail) independently of the
    returned `ScreenResult`, so it can never silently diverge from the verdict.
    """
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    # A passing category (economics) and ample volume/horizon, but a depth
    # below the floor, so only MIN_DEPTH fails.
    market = _market(category="economics", close_time=_NOW + timedelta(days=30))
    stats = BookStats(
        volume_24h_micros=MoneyMicros(config.min_volume_24h_micros),
        depth_contract_centis=ContractCentis(config.min_depth_contract_centis - 1),
    )

    result = screener.screen(market, stats)

    payload = writer.events_by_type(SCREEN_DECISION_EVENT)[-1].payload
    assert result.blocked_by == (MIN_DEPTH,)
    assert payload["blocked_by"] == [MIN_DEPTH]


def _assert_no_float_leaf(value: object) -> None:
    """Recursively assert that no `float` instance appears anywhere in `value`.

    Args:
        value: A JSON-safe payload value (a mapping, sequence, or scalar).

    Raises:
        AssertionError: If any nested leaf is a `float` instance.
    """
    if isinstance(value, float):
        raise AssertionError(f"unexpected float leaf: {value!r}")
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_no_float_leaf(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _assert_no_float_leaf(item)


def test_screen_decision_payload_contains_no_float_leaf() -> None:
    """The screening-decision payload never contains a `float` leaf."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)
    market = _market(category="sports", close_time=_NOW)
    stats = _failing_stats()

    screener.screen(market, stats)

    payload = writer.events_by_type(SCREEN_DECISION_EVENT)[-1].payload
    _assert_no_float_leaf(payload)


# --- LEGAL_RISK_ACK ------------------------------------------------------------


def test_constructing_screener_emits_one_legal_risk_ack_event_per_acknowledgement() -> (
    None
):
    """Construction emits exactly one `LEGAL_RISK_ACK` event per acknowledgement."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    acknowledgements = (
        LegalRiskAcknowledgement(
            category="sports", reason="operator reviewed sports book"
        ),
        LegalRiskAcknowledgement(
            category="crypto_price", reason="operator reviewed crypto desk"
        ),
    )

    Screener(config, writer, clock=_clock, acknowledgements=acknowledgements)

    assert len(writer.events_by_type(LEGAL_RISK_ACK_EVENT)) == 2


def test_legal_risk_ack_events_carry_each_ack_category_and_reason_in_order() -> None:
    """Each `LEGAL_RISK_ACK` event carries its ack's category/reason, in order."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    acknowledgements = (
        LegalRiskAcknowledgement(
            category="sports", reason="operator reviewed sports book"
        ),
        LegalRiskAcknowledgement(
            category="crypto_price", reason="operator reviewed crypto desk"
        ),
    )

    Screener(config, writer, clock=_clock, acknowledgements=acknowledgements)

    events = writer.events_by_type(LEGAL_RISK_ACK_EVENT)
    assert [event.payload for event in events] == [
        {"category": "sports", "reason": "operator reviewed sports book"},
        {"category": "crypto_price", "reason": "operator reviewed crypto desk"},
    ]


def test_zero_acknowledgements_emits_zero_legal_risk_ack_events() -> None:
    """No acknowledgements means no `LEGAL_RISK_ACK` events are ever emitted."""
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()

    Screener(config, writer, clock=_clock)

    assert writer.events_by_type(LEGAL_RISK_ACK_EVENT) == ()


# --- frozen-ness ---------------------------------------------------------------


def test_screen_result_is_frozen() -> None:
    """`ScreenResult` rejects attribute assignment after construction."""
    result = ScreenResult(ticker="X", eligible=True, blocked_by=(), filters={})

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.eligible = False


def test_book_stats_is_frozen() -> None:
    """`BookStats` rejects attribute assignment after construction."""
    stats = BookStats(
        volume_24h_micros=MoneyMicros(1), depth_contract_centis=ContractCentis(1)
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        stats.volume_24h_micros = MoneyMicros(2)


def test_legal_risk_acknowledgement_is_frozen() -> None:
    """`LegalRiskAcknowledgement` rejects attribute assignment after construction."""
    ack = LegalRiskAcknowledgement(category="sports", reason="reviewed")

    with pytest.raises(dataclasses.FrozenInstanceError):
        ack.reason = "changed my mind"


def test_filter_result_is_frozen() -> None:
    """`FilterResult` rejects attribute assignment after construction."""
    result = FilterResult(passed=True, measured=0)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.passed = False


# --- properties -----------------------------------------------------------


@st.composite
def _markets(draw: st.DrawFn) -> NormalizedMarket:
    """Build a random, closed-set-valid `NormalizedMarket` for property tests."""
    category = draw(st.sampled_from(_CATEGORY_POOL))
    offset_days = draw(st.integers(min_value=-10, max_value=200))
    return _market(category=category, close_time=_NOW + timedelta(days=offset_days))


@st.composite
def _stats(draw: st.DrawFn) -> BookStats:
    """Build random, non-negative `BookStats` for property tests."""
    volume = draw(st.integers(min_value=0, max_value=10_000_000_000))
    depth = draw(st.integers(min_value=0, max_value=1_000_000))
    return BookStats(
        volume_24h_micros=MoneyMicros(volume),
        depth_contract_centis=ContractCentis(depth),
    )


def _independent_blocked_by(
    market: NormalizedMarket, stats: BookStats, config: ScreenerConfig
) -> tuple[str, ...]:
    """Compute the expected `blocked_by` by calling each filter function directly.

    An independent re-derivation (not a restatement of `Screener.screen`'s own
    logic) used to cross-check the screener's aggregation and ordering.
    """
    results = {
        CATEGORY_BLOCKLIST: category_filter(
            market, blocklist=config.category_blocklist, acknowledged_categories=()
        ),
        MIN_VOLUME_24H: min_volume_filter(
            stats, threshold_micros=config.min_volume_24h_micros
        ),
        MIN_DEPTH: min_depth_filter(
            stats, threshold_centis=config.min_depth_contract_centis
        ),
        HORIZON_DAYS: horizon_filter(
            market,
            now=_NOW,
            min_days=config.horizon_days.min,
            max_days=config.horizon_days.max,
        ),
    }
    return tuple(name for name in _CANONICAL_ORDER if not results[name].passed)


@given(market=_markets(), stats=_stats())
def test_a_market_failing_any_filter_is_ineligible_and_named_in_blocked_by(
    market: NormalizedMarket, stats: BookStats
) -> None:
    """Any filter failure makes the market ineligible and names that filter.

    Cross-checks `Screener.screen`'s `eligible`/`blocked_by` against an
    independent, direct computation of each filter for the same inputs.
    """
    config = ScreenerConfig()
    writer = InMemoryEventLedgerWriter()
    screener = Screener(config, writer, clock=_clock)

    result = screener.screen(market, stats)

    expected_blocked_by = _independent_blocked_by(market, stats, config)
    assert result.blocked_by == expected_blocked_by
    assert result.eligible == (len(expected_blocked_by) == 0)
    for name in expected_blocked_by:
        assert name in result.blocked_by


@given(
    market=_markets(),
    stats=_stats(),
    extra_blocked_category=st.sampled_from(_CATEGORY_POOL),
    volume_bump=st.integers(min_value=0, max_value=1_000_000),
    depth_bump=st.integers(min_value=0, max_value=1_000),
    horizon_min_bump=st.integers(min_value=0, max_value=5),
    horizon_max_shrink=st.integers(min_value=0, max_value=5),
)
def test_tightening_config_never_turns_an_ineligible_market_eligible(
    market: NormalizedMarket,
    stats: BookStats,
    extra_blocked_category: str,
    volume_bump: int,
    depth_bump: int,
    horizon_min_bump: int,
    horizon_max_shrink: int,
) -> None:
    """Monotonicity: eligible under a tighter config implies eligible under
    the looser config it was tightened from.

    "Tightened" means: blocklist is a superset, `min_volume`/`min_depth` are
    raised (or held), and the horizon window is narrowed (or held) from both
    ends -- never loosened in any dimension.
    """
    loose_config = ScreenerConfig()
    tight_horizon_min = loose_config.horizon_days.min + horizon_min_bump
    tight_horizon_max = max(
        tight_horizon_min, loose_config.horizon_days.max - horizon_max_shrink
    )
    tight_config = ScreenerConfig(
        category_blocklist=tuple(
            {*loose_config.category_blocklist, extra_blocked_category}
        ),
        min_volume_24h_micros=loose_config.min_volume_24h_micros + volume_bump,
        min_depth_contract_centis=loose_config.min_depth_contract_centis + depth_bump,
        horizon_days=HorizonDays(min=tight_horizon_min, max=tight_horizon_max),
    )
    writer = InMemoryEventLedgerWriter()
    loose_screener = Screener(loose_config, writer, clock=_clock)
    tight_screener = Screener(tight_config, writer, clock=_clock)

    tight_result = tight_screener.screen(market, stats)
    loose_result = loose_screener.screen(market, stats)

    if tight_result.eligible:
        assert loose_result.eligible is True
