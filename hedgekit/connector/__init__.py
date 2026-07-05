"""Shared component: the read-only exchange connector surface.

Per SPEC S5.2, this package models only *public, read-only market access* --
it carries no trade credentials. It defines the :class:`MarketConnector`
protocol (SPEC S7.2), the normalized SPEC S6.2 models, a fixture-backed
:class:`FakeExchange`, and the :class:`MarketSnapshotTask` that records a
market snapshot and screening decision per market through an
:class:`EventLedgerWriter` seam. Issue #20 adds the fail-closed data-quality
surface: caller-scoped freshness checks (:mod:`hedgekit.connector.freshness`),
schema-drift validation (:mod:`hedgekit.connector.validation`), and rate
limiting / retry / circuit breaking (:mod:`hedgekit.connector.resilience`).
Everything on the price/money path uses :mod:`hedgekit.numeric` scaled-integer
types -- never floats (enforced by ``scripts/lint_no_floats.py``).
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
from hedgekit.connector.freshness import StaleSnapshotError, ensure_fresh, is_fresh
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
from hedgekit.connector.resilience import (
    CONNECTOR_HALT_EVENT,
    CircuitBreaker,
    CircuitState,
    ConnectorHaltError,
    MaintenanceHaltError,
    ResiliencePolicy,
    ResilientCaller,
    TokenBucket,
)
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
from hedgekit.connector.validation import (
    SCHEMA_ANOMALY_EVENT,
    ResponseSchema,
    SchemaAnomalyHaltError,
    SchemaRegistry,
    SchemaValidator,
    kalshi_default_schema_registry,
)

__all__ = [
    "CONNECTOR_HALT_EVENT",
    "DEFAULT_FEE_HAIRCUT_PPM",
    "DEFAULT_MAX_PARTICIPATION_PPM",
    "MARKET_SNAPSHOT_EVENT",
    "PAPER_FILL_MODEL_VERSION",
    "SCHEMA_ANOMALY_EVENT",
    "SCREEN_DECISION_EVENT",
    "BalanceSemantics",
    "BalanceSnapshot",
    "CancelCollateralRelease",
    "CircuitBreaker",
    "CircuitState",
    "ConnectorEvent",
    "ConnectorHaltError",
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
    "MaintenanceHaltError",
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
    "ResiliencePolicy",
    "ResilientCaller",
    "ResponseSchema",
    "SchemaAnomalyHaltError",
    "SchemaRegistry",
    "SchemaValidator",
    "StaleSnapshotError",
    "TakerFillResult",
    "TokenBucket",
    "TradePrint",
    "UnknownFeeModelError",
    "UnknownMarketError",
    "UnsettledProceeds",
    "ensure_fresh",
    "is_fresh",
    "kalshi_default_schema_registry",
    "market_to_payload",
    "participation_cap",
    "resting_fill_quantity",
    "walk_taker_fill",
]
