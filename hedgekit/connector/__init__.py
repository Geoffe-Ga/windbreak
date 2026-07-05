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
from hedgekit.connector.fees import FeeModel, UnknownFeeModelError
from hedgekit.connector.fills import (
    DEFAULT_FEE_HAIRCUT_PPM,
    DEFAULT_MAX_PARTICIPATION_PPM,
    PAPER_FILL_MODEL_VERSION,
    TakerFillResult,
    TradePrint,
    participation_cap,
    resting_fill_quantity,
    walk_taker_fill,
)
from hedgekit.connector.interface import MarketConnector, UnknownMarketError
from hedgekit.connector.models import (
    BalanceSnapshot,
    ExchangeStatus,
    Fill,
    NormalizedMarket,
    OpenOrder,
    OrderBookLevel,
    OrderBookSnapshot,
    Position,
    market_to_payload,
)
from hedgekit.connector.paper import PaperExchange, PaperOrderIntent
from hedgekit.connector.semantics import (
    BalanceSemantics,
    CancelCollateralRelease,
    FeeDebitTiming,
    FeeRounding,
    HaltedMarketBehavior,
    OrderCollateralInAvailable,
    OrderCollateralInTotal,
    PartialFillRepresentation,
    UnsettledProceeds,
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
    "DEFAULT_FEE_HAIRCUT_PPM",
    "DEFAULT_MAX_PARTICIPATION_PPM",
    "MARKET_SNAPSHOT_EVENT",
    "PAPER_FILL_MODEL_VERSION",
    "SCREEN_DECISION_EVENT",
    "BalanceSemantics",
    "BalanceSnapshot",
    "CancelCollateralRelease",
    "ConnectorEvent",
    "EventLedgerWriter",
    "ExchangeStatus",
    "FakeExchange",
    "FeeDebitTiming",
    "FeeModel",
    "FeeRounding",
    "Fill",
    "HaltedMarketBehavior",
    "InMemoryEventLedgerWriter",
    "LoggingEventLedgerWriter",
    "MarketConnector",
    "MarketSnapshotTask",
    "NormalizedMarket",
    "OpenOrder",
    "OrderBookLevel",
    "OrderBookSnapshot",
    "OrderCollateralInAvailable",
    "OrderCollateralInTotal",
    "PaperExchange",
    "PaperOrderIntent",
    "PartialFillRepresentation",
    "Position",
    "TakerFillResult",
    "TradePrint",
    "UnknownFeeModelError",
    "UnknownMarketError",
    "UnsettledProceeds",
    "market_to_payload",
    "participation_cap",
    "resting_fill_quantity",
    "walk_taker_fill",
]
