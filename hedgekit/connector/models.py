"""SPEC S6.2 normalized exchange models and their JSON-safe projection.

Every model here is a frozen, slotted dataclass describing one facet of an
exchange's public, read-only surface: markets, order books, balances, fills,
positions, and fee schedules. Arithmetic-bearing quantities (prices, contract
counts, money) are carried as hedgekit's scaled-integer unit types from
:mod:`hedgekit.numeric` -- never floats -- so this package is on the money path
guarded by ``scripts/lint_no_floats.py``.

:class:`NormalizedMarket` validates its closed-set and integrality invariants in
``__post_init__`` so a malformed upstream payload fails loudly at construction.
:func:`market_to_payload` renders a market into a JSON-safe mapping (datetimes
as ISO-8601 ``Z`` strings, no float leaf anywhere) for ledger/event emission.

The two richest facets -- :class:`BalanceSemantics` (balance-interpretation
enums) and :class:`FeeModel` (fee schedules and their integer fee bounds) --
live in the sibling :mod:`hedgekit.connector.semantics` and
:mod:`hedgekit.connector.fees` modules and are re-exported here so callers can
keep importing the whole SPEC S6.2 surface from one place.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hedgekit.connector.fees import FeeModel as FeeModel
from hedgekit.connector.semantics import BalanceSemantics as BalanceSemantics

if TYPE_CHECKING:
    from typing import Literal

    from hedgekit.numeric import ContractCentis, MoneyMicros, PricePips

#: The only market structure hedgekit trades today: a binary contract whose
#: maximum loss is fully pre-funded by posted collateral (SPEC S6.2).
_MARKET_TYPES: frozenset[str] = frozenset({"fully_collateralized_binary"})

#: The closed set of jurisdiction eligibility verdicts a market may carry.
_JURISDICTION_STATUSES: frozenset[str] = frozenset(
    {"eligible", "ineligible", "unknown"}
)


def _require_market_type(value: str) -> None:
    """Reject a ``market_type`` outside the sanctioned closed set.

    Args:
        value: The candidate market-type string.

    Raises:
        ValueError: If ``value`` is not a recognized market type. The message
            names the offending ``market_type`` field.
    """
    if value not in _MARKET_TYPES:
        allowed = ", ".join(sorted(_MARKET_TYPES))
        raise ValueError(f"market_type must be one of {{{allowed}}}, got {value!r}")


def _require_jurisdiction(value: str) -> None:
    """Reject a ``jurisdiction_status`` outside the sanctioned closed set.

    Args:
        value: The candidate jurisdiction-status string.

    Raises:
        ValueError: If ``value`` is not a recognized status. The message names
            the offending ``jurisdiction_status`` field.
    """
    if value not in _JURISDICTION_STATUSES:
        allowed = ", ".join(sorted(_JURISDICTION_STATUSES))
        raise ValueError(
            f"jurisdiction_status must be one of {{{allowed}}}, got {value!r}"
        )


def _require_positive_unit_int(value: int, field_name: str) -> None:
    """Guard that a plain-int unit field is a true, positive integer.

    The bool/int convention mirrors :meth:`hedgekit.numeric.types._IntUnit`:
    a stray ``bool`` (an ``int`` subclass) must never masquerade as a tick or
    minimum-order size, so it is rejected before the positivity check.

    Args:
        value: The candidate integer.
        field_name: The owning field's name, surfaced in the error message.

    Raises:
        TypeError: If ``value`` is a ``bool`` or is not an ``int``.
        ValueError: If ``value`` is not strictly positive.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{field_name} must be a non-bool int, got {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(f"{field_name} must be positive, got {value}")


@dataclass(frozen=True, slots=True)
class NormalizedMarket:
    """A single exchange market normalized to hedgekit's SPEC S6.2 schema.

    Attributes:
        exchange: The exchange identifier the market came from.
        ticker: The market's unique ticker on that exchange.
        event_ticker: The parent event's ticker.
        title: Human-readable market question.
        resolution_criteria: Prose describing how the market resolves.
        category: The market's topical category.
        close_time: When trading closes.
        expected_resolution_time: When resolution is expected, or None.
        market_type: The contract structure; must be a sanctioned type.
        price_tick_pips: Minimum price increment, in pips (a positive int).
        min_order_contract_centis: Minimum order size, in contract-centis (a
            positive int).
        fractional_trading_enabled: Whether sub-contract sizing is allowed.
        mutually_exclusive_group_id: Group of mutually exclusive markets, or
            None when the market stands alone.
        jurisdiction_status: Eligibility verdict; one of the sanctioned values.
        raw_exchange_payload_hash: Non-empty hash of the source payload, for
            provenance.
    """

    exchange: str
    ticker: str
    event_ticker: str
    title: str
    resolution_criteria: str
    category: str
    close_time: datetime
    expected_resolution_time: datetime | None
    market_type: Literal["fully_collateralized_binary"]
    price_tick_pips: int
    min_order_contract_centis: int
    fractional_trading_enabled: bool
    mutually_exclusive_group_id: str | None
    jurisdiction_status: Literal["eligible", "ineligible", "unknown"]
    raw_exchange_payload_hash: str

    def __post_init__(self) -> None:
        """Validate the closed-set, integrality, and provenance invariants.

        Raises:
            TypeError: If a unit int field is a ``bool`` or non-``int``.
            ValueError: If ``market_type`` or ``jurisdiction_status`` is
                unrecognized, a unit int field is non-positive, or the payload
                hash is empty.
        """
        _require_market_type(self.market_type)
        _require_jurisdiction(self.jurisdiction_status)
        _require_positive_unit_int(self.price_tick_pips, "price_tick_pips")
        _require_positive_unit_int(
            self.min_order_contract_centis, "min_order_contract_centis"
        )
        if not self.raw_exchange_payload_hash:
            raise ValueError("raw_exchange_payload_hash must be non-empty")


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    """One price level in a market's YES order book.

    Attributes:
        price: The level's price, in pips.
        quantity: The resting size at that price, in contract-centis.
    """

    price: PricePips
    quantity: ContractCentis


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """A point-in-time view of a market's YES bids and asks.

    Attributes:
        ticker: The market this book belongs to.
        yes_bids: Resting YES bids, best-first.
        yes_asks: Resting YES asks, best-first.
        fetched_at: When the snapshot was taken.
    """

    ticker: str
    yes_bids: tuple[OrderBookLevel, ...]
    yes_asks: tuple[OrderBookLevel, ...]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class ExchangeStatus:
    """The exchange's trading status at a moment in time.

    Attributes:
        status: Whether the exchange is open, paused, or closed.
        fetched_at: When the status was observed.
    """

    status: Literal["open", "paused", "closed"]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    """A point-in-time account balance.

    Attributes:
        total: The total account balance, in micros.
        available: The balance available to trade, in micros.
        fetched_at: When the balance was observed.
    """

    total: MoneyMicros
    available: MoneyMicros
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class Position:
    """An open position in a single market.

    Attributes:
        ticker: The market the position is held in.
        quantity: The net contract count held, in contract-centis.
        average_price: The position's average entry price, in pips.
    """

    ticker: str
    quantity: ContractCentis
    average_price: PricePips


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """A resting (unfilled or partially filled) order.

    Attributes:
        id: The venue's order identifier.
        ticker: The market the order rests in.
        side: Whether the order is on the YES or NO side.
        price: The order's limit price, in pips.
        quantity: The order's size, in contract-centis.
    """

    id: str
    ticker: str
    side: Literal["yes", "no"]
    price: PricePips
    quantity: ContractCentis


@dataclass(frozen=True, slots=True)
class Fill:
    """A single executed trade.

    Attributes:
        id: The venue's fill identifier.
        ticker: The market that traded.
        side: Whether the fill was on the YES or NO side.
        price: The execution price, in pips.
        quantity: The executed size, in contract-centis.
        ts: When the fill occurred (used for ``get_fills`` since-filtering).
    """

    id: str
    ticker: str
    side: Literal["yes", "no"]
    price: PricePips
    quantity: ContractCentis
    ts: datetime


def _iso_z(moment: datetime) -> str:
    """Render a datetime as ISO-8601 UTC with a trailing ``Z``.

    Args:
        moment: The (timezone-aware) datetime to render; normalized to UTC.

    Returns:
        A string like ``2024-12-18T19:00:00.000000Z``.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _jsonable_field(value: object) -> object:
    """Convert one market field value into a JSON-safe leaf.

    Args:
        value: The raw field value. Datetimes become ISO-8601 ``Z`` strings;
            every other value (str, int, bool, None) is already JSON-safe and
            passes through unchanged. No float is ever produced.

    Returns:
        The JSON-safe projection of ``value``.
    """
    return _iso_z(value) if isinstance(value, datetime) else value


def market_to_payload(market: NormalizedMarket) -> dict[str, object]:
    """Project a market into a JSON-safe mapping keyed by its field names.

    The mapping is stable and lossless: keys are the dataclass field names
    verbatim, datetimes are ISO-8601 ``Z`` strings (None stays None), and the
    plain-int unit fields remain ints -- there is never a float leaf anywhere.

    Args:
        market: The market to project.

    Returns:
        A JSON-serializable mapping of every field of ``market``.
    """
    return {
        field.name: _jsonable_field(getattr(market, field.name))
        for field in fields(market)
    }
