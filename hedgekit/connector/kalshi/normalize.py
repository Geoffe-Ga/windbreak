"""Pure normalization of raw Kalshi payloads into hedgekit's SPEC S6.2 models.

Every function here is a total, side-effect-free transform from a raw Kalshi
JSON mapping into a normalized model from :mod:`hedgekit.connector.models`. All
prices and sizes convert to hedgekit's scaled-integer unit types
(:class:`~hedgekit.numeric.PricePips`, :class:`~hedgekit.numeric.ContractCentis`)
with zero float intermediaries: this module sits on the money path guarded by
``scripts/lint_no_floats.py`` (SPEC S6.1), so only ``*``, ``+``, ``-`` and
``//`` integer arithmetic appears -- never ``/`` or ``float(...)``. Every
cents-to-pips and contract-to-centis conversion here is an exact multiplication
by :data:`PRICE_PIPS_PER_CENT` / :data:`CENTIS_PER_CONTRACT` (1 cent = 100
pips, 1 contract = 100 centis): none loses precision, so SPEC S6.1's
conservative-rounding direction never has to be chosen in this module.

The product gate is an *allowlist*: :func:`gate_product` normalizes only
``"binary"`` markets, refusing every other (or absent) ``market_type`` so a
newly-invented Kalshi product -- including any margin, perp, or other
derivative surface Kalshi might expose -- is refused by default rather than
silently mis-normalized (SPEC S1.1 invariant 2; SPEC S7.1).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

from hedgekit.connector.models import (
    ExchangeStatus,
    NormalizedMarket,
    OrderBookLevel,
    OrderBookSnapshot,
)
from hedgekit.ledger import canonical_json
from hedgekit.numeric import ContractCentis, PricePips

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any, Literal

#: Pips per US cent: a one-cent price increment is exactly 100 pips.
PRICE_PIPS_PER_CENT: Final = 100

#: Contract-centis per whole contract: one contract is exactly 100 centis.
CENTIS_PER_CONTRACT: Final = 100

#: Default price tick, in pips, when a market omits ``tick_size`` (a 1c tick).
DEFAULT_PRICE_TICK_PIPS: Final = 100

#: Minimum order size, in contract-centis, applied to every Kalshi market.
MIN_ORDER_CONTRACT_CENTIS: Final = 100

#: The full YES+NO price of a binary contract, in cents (a resolved dollar):
#: a NO price of ``c`` cents implies a YES price of ``100 - c`` cents.
_FULL_PRICE_CENTS: Final = 100

#: The exchange identifier stamped on every market this connector normalizes.
KALSHI_EXCHANGE: Final = "kalshi"

#: The only raw Kalshi ``market_type`` the allowlist admits.
_BINARY: Final = "binary"

#: The normalized market-type Kalshi binaries map to (SPEC S6.2).
_NORMALIZED_MARKET_TYPE: Final = "fully_collateralized_binary"

#: Kalshi exposes no eligibility signal, so jurisdiction is always unknown
#: (SPEC S20 Q3) -- never inferred as ``"eligible"``.
_JURISDICTION_UNKNOWN: Final = "unknown"

#: Ledger event type recorded when a non-binary product is refused.
PRODUCT_REFUSED_EVENT: Final = "PRODUCT_REFUSED"

#: Ledger event type recorded when an allowed binary market fails to normalize
#: (a required field is missing or a leaf has the wrong type). Emitting this and
#: skipping the market keeps one malformed payload from aborting a whole scan,
#: while still never silently dropping it (SPEC S1.1 invariant 2).
MARKET_MALFORMED_EVENT: Final = "MARKET_MALFORMED"

#: The three narrowed exchange-status literals (fake.py ``_STATUS_BY_NAME`` idiom).
_STATUS_OPEN: Final = "open"
_STATUS_PAUSED: Final = "paused"
_STATUS_CLOSED: Final = "closed"


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a UTC-normalized datetime.

    Args:
        value: An ISO-8601 string, e.g. ``2024-12-18T19:00:00Z``.

    Returns:
        The timezone-aware datetime, normalized to UTC.
    """
    return datetime.fromisoformat(value).astimezone(UTC)


def _parse_optional_dt(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 timestamp, preserving ``None``.

    Args:
        value: An ISO-8601 string, or None.

    Returns:
        The parsed UTC datetime, or None when ``value`` is None.
    """
    return None if value is None else _parse_dt(value)


def payload_hash(raw: Mapping[str, object]) -> str:
    """Return a stable SHA-256 hex digest of a raw exchange payload.

    The digest is taken over the canonical (key-sorted, whitespace-free) JSON
    encoding, so it depends only on the payload's contents -- never on dict
    insertion order -- giving a reproducible provenance fingerprint.

    Args:
        raw: The raw exchange payload to fingerprint.

    Returns:
        The 64-character lowercase-hex SHA-256 digest.
    """
    return hashlib.sha256(canonical_json(dict(raw)).encode("utf-8")).hexdigest()


def gate_product(raw_market: Mapping[str, object]) -> str | None:
    """Return why a market is refused, or None when it is an allowed binary.

    This is an allowlist, not a denylist: only ``market_type == "binary"``
    passes. Any other type -- or a payload missing ``market_type`` entirely --
    is refused so a newly-invented product is never silently normalized.

    Args:
        raw_market: The raw Kalshi market payload to gate.

    Returns:
        None when the market is an allowed binary; otherwise a non-empty reason
        string naming the offending type (containing ``"missing"`` when the
        ``market_type`` key is absent).
    """
    if "market_type" not in raw_market:
        return "market_type is missing"
    market_type = raw_market["market_type"]
    if market_type == _BINARY:
        return None
    return f"refused non-binary market_type: {market_type!r}"


def _as_mapping(raw: Mapping[str, object]) -> Mapping[str, Any]:
    """Re-view a raw payload as a ``str``-keyed ``Any`` mapping.

    JSON payloads are dynamically typed, so this narrows a raw ``object``-valued
    mapping to one whose values read as ``Any`` -- letting field access stay
    ergonomic while a bad leaf still fails loudly at unit-wrapping time.

    Args:
        raw: The raw payload to re-view.

    Returns:
        The same mapping, typed for ergonomic field access.
    """
    return cast("Mapping[str, Any]", raw)


def _is_grouped(raw_event: Mapping[str, object] | None) -> bool:
    """Return whether a market's event marks it mutually exclusive.

    Feeds :attr:`NormalizedMarket.mutually_exclusive_group_id`, which downstream
    coherence checks use to sum probabilities across an event's outcomes
    (SPEC S8.7).

    Args:
        raw_event: The market's parent event payload, or None when standalone.

    Returns:
        True only when an event is supplied and its ``mutually_exclusive`` flag
        is truthy.
    """
    return raw_event is not None and bool(raw_event.get("mutually_exclusive"))


def _tick_pips(market: Mapping[str, Any]) -> int:
    """Return a market's price tick in pips, defaulting a missing tick to 1c.

    Args:
        market: The raw market payload.

    Returns:
        The price tick in pips: :data:`DEFAULT_PRICE_TICK_PIPS` when
        ``tick_size`` is absent, else the cents tick multiplied into pips.
    """
    raw_tick = market.get("tick_size")
    if raw_tick is None:
        return DEFAULT_PRICE_TICK_PIPS
    return int(raw_tick) * PRICE_PIPS_PER_CENT


def normalize_market(
    raw_market: Mapping[str, object], raw_event: Mapping[str, object] | None
) -> NormalizedMarket:
    """Normalize one raw Kalshi binary market into a :class:`NormalizedMarket`.

    Cents-denominated fields convert to pips by exact multiplication; the
    jurisdiction status is always ``"unknown"`` (SPEC S20 Q3, since Kalshi
    exposes no per-market eligibility signal); and the group id is the event
    ticker exactly when the parent event is mutually exclusive, feeding
    downstream coherence checks across the event's outcomes (SPEC S8.7).
    :meth:`NormalizedMarket.__post_init__` remains the loud backstop for any
    invariant this function does not pre-check.

    Args:
        raw_market: The raw Kalshi market payload (a ``/markets`` list entry).
        raw_event: The market's parent event payload, or None when standalone.

    Returns:
        The normalized market.
    """
    market = _as_mapping(raw_market)
    group_id = market["event_ticker"] if _is_grouped(raw_event) else None
    return NormalizedMarket(
        exchange=KALSHI_EXCHANGE,
        ticker=market["ticker"],
        event_ticker=market["event_ticker"],
        title=market["title"],
        resolution_criteria=market["rules_primary"],
        category=market["category"],
        close_time=_parse_dt(market["close_time"]),
        expected_resolution_time=_parse_optional_dt(
            market.get("expected_expiration_time")
        ),
        market_type=_NORMALIZED_MARKET_TYPE,
        price_tick_pips=_tick_pips(market),
        min_order_contract_centis=MIN_ORDER_CONTRACT_CENTIS,
        fractional_trading_enabled=False,
        mutually_exclusive_group_id=group_id,
        jurisdiction_status=_JURISDICTION_UNKNOWN,
        raw_exchange_payload_hash=payload_hash(raw_market),
    )


def _price_value(level: OrderBookLevel) -> int:
    """Return a level's price in pips, the key both book sides sort on."""
    return level.price.value


def _yes_bid_level(pair: Sequence[int]) -> OrderBookLevel:
    """Build a YES bid level from a raw ``[cents, count]`` pair.

    Args:
        pair: A raw ``[price_cents, contract_count]`` YES entry. A non-int
            leaf (e.g. a float) fails loudly when wrapped in a unit type.

    Returns:
        The YES bid level, priced in pips and sized in contract-centis.
    """
    cents, count = pair[0], pair[1]
    return OrderBookLevel(
        price=PricePips(cents * PRICE_PIPS_PER_CENT),
        quantity=ContractCentis(count * CENTIS_PER_CONTRACT),
    )


def _no_bid_as_yes_ask(pair: Sequence[int]) -> OrderBookLevel:
    """Invert a raw NO bid ``[cents, count]`` pair into a YES ask level.

    A resting NO bid at ``c`` cents is, in YES terms, an offer to sell YES at
    ``100 - c`` cents, so it becomes a YES ask at that inverted price.

    Args:
        pair: A raw ``[price_cents, contract_count]`` NO entry.

    Returns:
        The equivalent YES ask level, priced in pips and sized in
        contract-centis.
    """
    cents, count = pair[0], pair[1]
    yes_cents = _FULL_PRICE_CENTS - cents
    return OrderBookLevel(
        price=PricePips(yes_cents * PRICE_PIPS_PER_CENT),
        quantity=ContractCentis(count * CENTIS_PER_CONTRACT),
    )


def _order_book_map(raw: Mapping[str, object]) -> Mapping[str, object]:
    """Return the raw ``orderbook`` sub-mapping, or an empty one when absent."""
    inner = raw.get("orderbook")
    return inner if isinstance(inner, Mapping) else {}


def _raw_side(book: Mapping[str, object], key: str) -> list[Sequence[int]]:
    """Return one raw book side as a list of ``[cents, count]`` pairs.

    Args:
        book: The raw ``orderbook`` sub-mapping.
        key: The side to extract, ``"yes"`` or ``"no"``.

    Returns:
        The raw price/count pairs for that side; an empty list when the side is
        absent, None, or not a list.
    """
    side = book.get(key)
    if not isinstance(side, list):
        return []
    return cast("list[Sequence[int]]", side)


def normalize_order_book(
    ticker: str, raw: Mapping[str, object], fetched_at: datetime
) -> OrderBookSnapshot:
    """Normalize a raw Kalshi order book into an :class:`OrderBookSnapshot`.

    YES levels become YES bids (best-first: descending price); NO levels invert
    into YES asks (best-first: ascending price). Absent, None, or empty sides
    yield empty tuples rather than missing fields.

    Args:
        ticker: The market the book belongs to.
        raw: The raw payload shaped ``{"orderbook": {"yes": ..., "no": ...}}``.
        fetched_at: When the snapshot was taken.

    Returns:
        The normalized order-book snapshot.
    """
    book = _order_book_map(raw)
    yes_bids = tuple(
        sorted(
            (_yes_bid_level(pair) for pair in _raw_side(book, "yes")),
            key=_price_value,
            reverse=True,
        )
    )
    yes_asks = tuple(
        sorted(
            (_no_bid_as_yes_ask(pair) for pair in _raw_side(book, "no")),
            key=_price_value,
        )
    )
    return OrderBookSnapshot(
        ticker=ticker, yes_bids=yes_bids, yes_asks=yes_asks, fetched_at=fetched_at
    )


def _resolve_status(raw: Mapping[str, object]) -> Literal["open", "paused", "closed"]:
    """Map raw active flags to the exchange-status literal domain.

    Args:
        raw: The raw payload with ``exchange_active`` / ``trading_active`` flags.

    Returns:
        ``"open"`` when both flags are set, ``"paused"`` when the exchange is up
        but trading is halted, and ``"closed"`` when the exchange is down.
    """
    if not bool(raw.get("exchange_active")):
        return _STATUS_CLOSED
    if bool(raw.get("trading_active")):
        return _STATUS_OPEN
    return _STATUS_PAUSED


def normalize_exchange_status(
    raw: Mapping[str, object], fetched_at: datetime
) -> ExchangeStatus:
    """Normalize raw exchange active-flags into an :class:`ExchangeStatus`.

    Args:
        raw: The raw ``{"exchange_active": ..., "trading_active": ...}`` payload.
        fetched_at: When the status was observed.

    Returns:
        The normalized exchange status.
    """
    return ExchangeStatus(status=_resolve_status(raw), fetched_at=fetched_at)
