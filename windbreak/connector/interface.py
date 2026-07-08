"""The SPEC S7.2 :class:`MarketConnector` protocol and its lookup error.

:class:`MarketConnector` is the structural (``@runtime_checkable``) contract
every exchange adapter satisfies: the exactly-thirteen read-only market-data
methods plus the two trading methods that later issues implement. It exposes
*public, read-only market access* only -- no trade credentials are modeled here
(SPEC S5.2). :class:`UnknownMarketError` subclasses :class:`KeyError` so callers
may catch either a specific "unknown ticker" failure or a generic key miss.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from windbreak.connector.models import (
        BalanceSemantics,
        BalanceSnapshot,
        ExchangeStatus,
        FeeModel,
        Fill,
        NormalizedMarket,
        OpenOrder,
        OrderBookSnapshot,
        Position,
    )


class UnknownMarketError(KeyError):
    """Raised when a ticker is not offered by the connector."""


@runtime_checkable
class MarketConnector(Protocol):
    """The read-only market surface every exchange adapter implements.

    The thirteen methods are exactly those enumerated in SPEC S7.2. Only
    public, read-only market access is modeled: no trade credentials appear in
    this contract (SPEC S5.2). ``place_order`` and ``cancel_order`` are part of
    the surface but are wired by a later issue.
    """

    def list_markets(self) -> tuple[NormalizedMarket, ...]:
        """Return every market the venue currently offers."""
        ...

    def get_market(self, ticker: str) -> NormalizedMarket:
        """Return the market for ``ticker``.

        Args:
            ticker: The market ticker to look up.

        Returns:
            The normalized market.

        Raises:
            UnknownMarketError: If no market has that ticker.
        """
        ...

    def get_order_book(self, ticker: str) -> OrderBookSnapshot:
        """Return the current YES order book for ``ticker``.

        Args:
            ticker: The market ticker to look up.

        Returns:
            The order-book snapshot.

        Raises:
            UnknownMarketError: If no market has that ticker.
        """
        ...

    def get_exchange_status(self) -> ExchangeStatus:
        """Return the exchange's current trading status."""
        ...

    def get_exchange_time(self) -> datetime:
        """Return the exchange's current server time."""
        ...

    def get_balance_semantics(self) -> BalanceSemantics:
        """Return the venue's balance-interpretation semantics."""
        ...

    def get_balances(self) -> BalanceSnapshot:
        """Return the account's current balances."""
        ...

    def get_positions(self) -> tuple[Position, ...]:
        """Return the account's open positions."""
        ...

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Return the account's resting open orders."""
        ...

    def get_fills(self, since: datetime) -> tuple[Fill, ...]:
        """Return fills executed strictly after ``since``.

        Args:
            since: The exclusive lower bound; only fills with a later timestamp
                are returned.

        Returns:
            The matching fills.
        """
        ...

    def get_fee_model(self, market_or_series: str) -> FeeModel:
        """Return the fee schedule applicable to a market or series.

        Args:
            market_or_series: The market ticker or series key to look up.

        Returns:
            The applicable fee model.
        """
        ...

    def place_order(self, normalized_intent: object, approval_token: object) -> object:
        """Place an order from a normalized intent and an approval token.

        Args:
            normalized_intent: The normalized order intent to submit.
            approval_token: The risk-kernel approval authorizing the order.

        Returns:
            A venue-specific placement receipt.
        """
        ...

    def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order by its identifier.

        Args:
            order_id: The venue order identifier to cancel.
        """
        ...
