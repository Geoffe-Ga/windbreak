"""The Kalshi connector: a public, read-only adapter over Kalshi's v2 REST API.

Per SPEC S5.2 this package models only *public, read-only market access* and
carries no trade credentials. It targets Kalshi's current API generation
(SPEC S7.1) at ``https://api.elections.kalshi.com/trade-api/v2`` and delivers
the read-only market-data surface: :class:`KalshiConnector` normalizes markets,
order books, exchange status, and server time, applying a binary-only product
allowlist and ledgering a :data:`PRODUCT_REFUSED_EVENT` for every refused
product. ``list_markets`` follows Kalshi's ``cursor`` pagination across every
page of ``/markets`` and ``/events`` (bounded by a hard cap that raises
:class:`KalshiPaginationError` rather than looping forever) and fails closed on
a single unnormalizable binary by ledgering a :data:`MARKET_MALFORMED_EVENT`.

Scope fence -- methods that intentionally raise :class:`NotImplementedError`
until later work wires them:

    * ``place_order`` / ``cancel_order`` -- the order path (milestone M4).
    * ``get_balances`` / ``get_balance_semantics`` / ``get_positions`` /
      ``get_open_orders`` / ``get_fills`` / ``get_fee_model`` -- balance, fee,
      and account access (issue #3).

Everything on the price/money path uses :mod:`hedgekit.numeric` scaled-integer
types -- never floats (enforced by ``scripts/lint_no_floats.py``).
"""

from hedgekit.connector.kalshi.adapter import (
    KalshiConnector,
    KalshiPaginationError,
)
from hedgekit.connector.kalshi.client import (
    KalshiApiError,
    KalshiClient,
    KalshiResponse,
)
from hedgekit.connector.kalshi.normalize import (
    MARKET_MALFORMED_EVENT,
    PRODUCT_REFUSED_EVENT,
)

__all__ = [
    "MARKET_MALFORMED_EVENT",
    "PRODUCT_REFUSED_EVENT",
    "KalshiApiError",
    "KalshiClient",
    "KalshiConnector",
    "KalshiPaginationError",
    "KalshiResponse",
]
