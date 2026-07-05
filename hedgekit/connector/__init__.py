"""Shared component: the read-only exchange connector surface.

Per SPEC S5.2, this package models only *public, read-only market access* --
it carries no trade credentials. It defines the :class:`MarketConnector`
protocol (SPEC S7.2), the normalized SPEC S6.2 models, a fixture-backed
:class:`FakeExchange`, and the :class:`MarketSnapshotTask` that records a
market snapshot and screening decision per market through an
:class:`EventLedgerWriter` seam. Everything on the price/money path uses
:mod:`hedgekit.numeric` scaled-integer types -- never floats (enforced by
``scripts/lint_no_floats.py``).
"""

from hedgekit.connector.fake import FakeExchange
from hedgekit.connector.interface import MarketConnector, UnknownMarketError
from hedgekit.connector.models import (
    BalanceSemantics,
    BalanceSnapshot,
    ExchangeStatus,
    FeeModel,
    Fill,
    NormalizedMarket,
    OpenOrder,
    OrderBookLevel,
    OrderBookSnapshot,
    Position,
    market_to_payload,
)
from hedgekit.connector.snapshot import (
    MARKET_SNAPSHOT_EVENT,
    SCREEN_DECISION_EVENT,
    ConnectorEvent,
    EventLedgerWriter,
    InMemoryEventLedgerWriter,
    LoggingEventLedgerWriter,
    MarketSnapshotTask,
)

__all__ = [
    "MARKET_SNAPSHOT_EVENT",
    "SCREEN_DECISION_EVENT",
    "BalanceSemantics",
    "BalanceSnapshot",
    "ConnectorEvent",
    "EventLedgerWriter",
    "ExchangeStatus",
    "FakeExchange",
    "FeeModel",
    "Fill",
    "InMemoryEventLedgerWriter",
    "LoggingEventLedgerWriter",
    "MarketConnector",
    "MarketSnapshotTask",
    "NormalizedMarket",
    "OpenOrder",
    "OrderBookLevel",
    "OrderBookSnapshot",
    "Position",
    "UnknownMarketError",
    "market_to_payload",
]
