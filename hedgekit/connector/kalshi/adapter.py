"""The :class:`KalshiConnector` adapter over Kalshi's public read-only surface.

:class:`KalshiConnector` implements the SPEC S7.2
:class:`~hedgekit.connector.interface.MarketConnector` protocol against a
:class:`~hedgekit.connector.kalshi.client.KalshiClient`. It exposes only the
public, read-only market-data methods this issue delivers -- ``list_markets``,
``get_market``, ``get_order_book``, ``get_exchange_status``, and
``get_exchange_time`` -- normalizing each raw payload through
:mod:`hedgekit.connector.kalshi.normalize`. ``list_markets`` applies the binary
allowlist and ledgers one ``PRODUCT_REFUSED`` event per refused product through
an injected :class:`~hedgekit.connector.snapshot.EventLedgerWriter`, isolating a
broken writer so one bad record never aborts a run.

The trading and account methods (order path is milestone M4; balances, fees,
positions, fills are issue #3) raise :class:`NotImplementedError` until those
issues wire them. This module sits on the money path guarded by
``scripts/lint_no_floats.py``: no ``/`` or ``float`` appears anywhere.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

from hedgekit.connector.interface import UnknownMarketError
from hedgekit.connector.kalshi.client import KalshiApiError
from hedgekit.connector.kalshi.normalize import (
    MARKET_MALFORMED_EVENT,
    PRODUCT_REFUSED_EVENT,
    gate_product,
    normalize_exchange_status,
    normalize_market,
    normalize_order_book,
    payload_hash,
)
from hedgekit.connector.snapshot import ConnectorEvent

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    from hedgekit.connector.kalshi.client import KalshiClient
    from hedgekit.connector.models import (
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
    from hedgekit.connector.snapshot import EventLedgerWriter

_LOGGER = logging.getLogger("hedgekit.connector.kalshi")

#: Hard upper bound on pages a single cursor-paginated fetch will follow. A
#: healthy Kalshi catalog is far below this; the cap exists solely so a venue
#: that returns a never-emptying cursor (a bug or a runaway loop) fails loudly
#: via :class:`KalshiPaginationError` instead of spinning forever.
_MAX_PAGES: Final = 1000

#: Malformed-market normalization failures are total functions of the payload,
#: so these are the only exception types :meth:`KalshiConnector.list_markets`
#: degrades to a ledgered ``MARKET_MALFORMED`` event: a missing required field
#: (``KeyError``), an unparseable value (``ValueError``, e.g. a bad timestamp),
#: or a wrong-typed money leaf (``TypeError`` from a scaled-integer unit).
_MALFORMED_MARKET_ERRORS: Final = (KeyError, ValueError, TypeError)

#: Message naming the milestone that wires the order path (place/cancel).
_ORDER_PATH_DEFERRAL: Final = "the order path is deferred to milestone M4"

#: Message naming the issue that wires account/balance/fee access.
_ACCOUNT_DEFERRAL: Final = "balance, fee, and account access is deferred to issue #3"


def _utc_now() -> datetime:
    """Return the current time as a UTC datetime (the default connector clock)."""
    return datetime.now(UTC)


def _iso_timestamp(moment: datetime) -> str:
    """Render a datetime as ISO-8601 UTC with a trailing ``Z``.

    Args:
        moment: The (timezone-aware) datetime to render; normalized to UTC.

    Returns:
        A string like ``2025-06-01T12:00:00.000000Z``.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class KalshiPaginationError(RuntimeError):
    """Raised when a cursor-paginated fetch exceeds :data:`_MAX_PAGES`.

    Kalshi's ``/markets`` and ``/events`` endpoints paginate with an opaque
    ``cursor``; the connector follows it page by page until the cursor empties.
    If it never empties within the safety cap -- a venue bug or a cursor that
    loops on itself -- the connector refuses to spin forever and raises this,
    surfacing the runaway loudly rather than hanging a caller.
    """

    def __init__(self, endpoint: str, max_pages: int) -> None:
        """Initialize with the endpoint and the cap that was exceeded.

        Args:
            endpoint: The path segment that would not stop paginating.
            max_pages: The page cap that was hit.
        """
        self.endpoint = endpoint
        self.max_pages = max_pages
        super().__init__(
            f"{endpoint!r} pagination exceeded the {max_pages}-page safety cap; "
            "refusing to follow an unbounded cursor"
        )


class KalshiConnector:
    """A read-only :class:`MarketConnector` backed by :class:`KalshiClient`."""

    def __init__(
        self,
        client: KalshiClient,
        ledger_writer: EventLedgerWriter,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Initialize the connector.

        Args:
            client: The HTTPS JSON client the connector fetches through.
            ledger_writer: The seam that records ``PRODUCT_REFUSED`` events.
            clock: Returns "now"; injected so snapshots are deterministic in
                tests. Defaults to wall-clock UTC.
        """
        self._client = client
        self._ledger_writer = ledger_writer
        self._clock = clock

    def _paginate(self, endpoint: str, item_key: str) -> list[Mapping[str, Any]]:
        """Follow a Kalshi ``cursor`` across every page of one list endpoint.

        Fetches ``endpoint`` repeatedly, passing each response's non-empty
        ``cursor`` as the ``cursor`` query parameter of the next request, and
        concatenates the ``item_key`` list from every page. The walk is bounded
        by :data:`_MAX_PAGES`: a cursor that never empties raises
        :class:`KalshiPaginationError` rather than looping forever.

        Args:
            endpoint: The single path segment to fetch (``"markets"`` /
                ``"events"``).
            item_key: The response key whose list is aggregated across pages.

        Returns:
            Every item from every page, in page-then-position order.

        Raises:
            KalshiPaginationError: If the cursor is still non-empty after
                :data:`_MAX_PAGES` pages.
        """
        items: list[Mapping[str, Any]] = []
        cursor = ""
        for _ in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else None
            payload = cast(
                "Mapping[str, Any]", self._client.get(endpoint, params=params).payload
            )
            items.extend(payload.get(item_key, []))
            cursor = payload.get("cursor") or ""
            if not cursor:
                return items
        raise KalshiPaginationError(endpoint, _MAX_PAGES)

    def _raw_markets(self) -> list[Mapping[str, Any]]:
        """Fetch every page of ``/markets`` and return the raw market payloads."""
        return self._paginate("markets", "markets")

    def _event_index(self) -> dict[str, Mapping[str, Any]]:
        """Fetch every page of ``/events``, indexing raw events by event ticker."""
        return {
            event["event_ticker"]: event for event in self._paginate("events", "events")
        }

    def list_markets(self) -> tuple[NormalizedMarket, ...]:
        """Return every allowed binary market, ledgering each refusal.

        Non-binary products are excluded and recorded as a single
        ``PRODUCT_REFUSED`` event each. An allowed binary that fails to
        normalize (a missing field or wrong-typed leaf) is likewise excluded
        and recorded as a ``MARKET_MALFORMED`` event, so one bad payload never
        aborts the scan yet is never silently dropped. Every page of both
        ``/markets`` and ``/events`` is followed via their ``cursor``. Only the
        normalized binaries are returned.

        Returns:
            The normalized binary markets the venue currently offers.
        """
        events = self._event_index()
        normalized: list[NormalizedMarket] = []
        for raw in self._raw_markets():
            reason = gate_product(raw)
            if reason is not None:
                self._record_refusal(raw, reason)
                continue
            try:
                market = normalize_market(raw, events.get(raw["event_ticker"]))
            except _MALFORMED_MARKET_ERRORS as exc:
                self._record_malformed(raw, exc)
                continue
            normalized.append(market)
        return tuple(normalized)

    def _record_refusal(self, raw: Mapping[str, Any], reason: str) -> None:
        """Ledger one ``PRODUCT_REFUSED`` event for a refused market.

        Args:
            raw: The refused raw market payload.
            reason: Why the product was refused.
        """
        event = ConnectorEvent(
            event_type=PRODUCT_REFUSED_EVENT,
            payload={
                "ticker": raw.get("ticker"),
                "event_ticker": raw.get("event_ticker"),
                "reason": reason,
                "raw_exchange_payload_hash": payload_hash(raw),
            },
            ts=_iso_timestamp(self._clock()),
        )
        self._record(event)

    def _record_malformed(self, raw: Mapping[str, Any], exc: Exception) -> None:
        """Ledger one ``MARKET_MALFORMED`` event for an unnormalizable binary.

        Args:
            raw: The binary market payload that failed to normalize.
            exc: The normalization failure, named in the ledgered reason.
        """
        event = ConnectorEvent(
            event_type=MARKET_MALFORMED_EVENT,
            payload={
                "ticker": raw.get("ticker"),
                "event_ticker": raw.get("event_ticker"),
                "reason": f"{type(exc).__name__}: {exc}",
                "raw_exchange_payload_hash": payload_hash(raw),
            },
            ts=_iso_timestamp(self._clock()),
        )
        self._record(event)

    def _record(self, event: ConnectorEvent) -> None:
        """Record an event via the writer, isolating any failure.

        Args:
            event: The event to record. A raising writer is logged and
                swallowed so one bad record never aborts a run.
        """
        try:
            self._ledger_writer.record(event)
        except Exception as exc:
            _LOGGER.warning(
                "event ledger writer failed to record %s event: %s",
                event.event_type,
                exc,
                extra={"component": "connector.kalshi"},
            )

    def get_market(self, ticker: str) -> NormalizedMarket:
        """Return the normalized binary market for ``ticker``.

        Args:
            ticker: The market ticker to look up.

        Returns:
            The normalized market.

        Raises:
            UnknownMarketError: If the ticker is refused, not offered, or its
                payload is malformed (fail closed: the malformed binary is
                ledgered as a ``MARKET_MALFORMED`` event before raising).
        """
        for raw in self._raw_markets():
            if raw.get("ticker") != ticker:
                continue
            if gate_product(raw) is not None:
                raise UnknownMarketError(ticker)
            events = self._event_index()
            try:
                return normalize_market(raw, events.get(raw["event_ticker"]))
            except _MALFORMED_MARKET_ERRORS as exc:
                self._record_malformed(raw, exc)
                raise UnknownMarketError(ticker) from exc
        raise UnknownMarketError(ticker)

    def get_order_book(self, ticker: str) -> OrderBookSnapshot:
        """Return the current YES order book for ``ticker``.

        Args:
            ticker: The market ticker to look up.

        Returns:
            The order-book snapshot, stamped with the injected clock.

        Raises:
            UnknownMarketError: If the venue has no book for that ticker.
        """
        try:
            response = self._client.get("markets", ticker, "orderbook")
        except KalshiApiError as exc:
            raise UnknownMarketError(ticker) from exc
        raw = cast("Mapping[str, object]", response.payload)
        return normalize_order_book(ticker, raw, self._clock())

    def get_exchange_status(self) -> ExchangeStatus:
        """Return the exchange's current trading status."""
        response = self._client.get("exchange", "status")
        raw = cast("Mapping[str, object]", response.payload)
        return normalize_exchange_status(raw, self._clock())

    def get_exchange_time(self) -> datetime:
        """Return the exchange server time, falling back to the clock.

        Returns:
            The response ``Date`` header when present; otherwise the injected
            clock's current time.
        """
        response = self._client.get("exchange", "status")
        if response.server_date is not None:
            return response.server_date
        return self._clock()

    def get_balance_semantics(self) -> BalanceSemantics:
        """Raise; account access is deferred (issue #3).

        Raises:
            NotImplementedError: Always; see :data:`_ACCOUNT_DEFERRAL`.
        """
        raise NotImplementedError(_ACCOUNT_DEFERRAL)

    def get_balances(self) -> BalanceSnapshot:
        """Raise; account access is deferred (issue #3).

        Raises:
            NotImplementedError: Always; see :data:`_ACCOUNT_DEFERRAL`.
        """
        raise NotImplementedError(_ACCOUNT_DEFERRAL)

    def get_positions(self) -> tuple[Position, ...]:
        """Raise; account access is deferred (issue #3).

        Raises:
            NotImplementedError: Always; see :data:`_ACCOUNT_DEFERRAL`.
        """
        raise NotImplementedError(_ACCOUNT_DEFERRAL)

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Raise; account access is deferred (issue #3).

        Raises:
            NotImplementedError: Always; see :data:`_ACCOUNT_DEFERRAL`.
        """
        raise NotImplementedError(_ACCOUNT_DEFERRAL)

    def get_fills(self, since: datetime) -> tuple[Fill, ...]:
        """Raise; account access is deferred (issue #3).

        Args:
            since: Unused exclusive lower bound on fill timestamps.

        Raises:
            NotImplementedError: Always; see :data:`_ACCOUNT_DEFERRAL`.
        """
        raise NotImplementedError(_ACCOUNT_DEFERRAL)

    def get_fee_model(self, market_or_series: str) -> FeeModel:
        """Raise; fee access is deferred (issue #3).

        Args:
            market_or_series: Unused market ticker or series key.

        Raises:
            NotImplementedError: Always; see :data:`_ACCOUNT_DEFERRAL`.
        """
        raise NotImplementedError(_ACCOUNT_DEFERRAL)

    def place_order(self, normalized_intent: object, approval_token: object) -> object:
        """Raise; the order path is deferred (milestone M4).

        Args:
            normalized_intent: Unused normalized order intent.
            approval_token: Unused risk-kernel approval token.

        Raises:
            NotImplementedError: Always; see :data:`_ORDER_PATH_DEFERRAL`.
        """
        raise NotImplementedError(_ORDER_PATH_DEFERRAL)

    def cancel_order(self, order_id: str) -> None:
        """Raise; the order path is deferred (milestone M4).

        Args:
            order_id: Unused venue order identifier.

        Raises:
            NotImplementedError: Always; see :data:`_ORDER_PATH_DEFERRAL`.
        """
        raise NotImplementedError(_ORDER_PATH_DEFERRAL)
