"""SPEC S7.5 :class:`PaperExchange`: a replay-driven pessimistic paper trader.

:class:`PaperExchange` is a :class:`~hedgekit.connector.interface.MarketConnector`
that simulates trading against *recorded* market data. A session is a per-ticker
sequence of steps, each pairing an order-book snapshot with the trade prints
that followed it. A crossing order fills immediately via the pessimistic taker
walk (:func:`hedgekit.connector.fills.walk_taker_fill`); its unfilled remainder
rests, and resting orders fill on later steps only when the recorded market
*trades through* their limit (:func:`hedgekit.connector.fills.resting_fill_quantity`).

The connector reuses :mod:`hedgekit.connector.fake`'s JSON loader helpers for
the static market/balance/fee fixtures (DRY) and adds a ``sessions.json`` reader
for the book-and-print replay. Balances are static: this simulator models fills
and resting orders, *not* balance debits (out of scope for issue #19).

Consistency guard (issue #18): a taker walk can span multiple book levels, each
needing its own :class:`~hedgekit.connector.models.Fill` price, so the
constructor rejects any balance semantics whose ``partial_fill_representation``
is not :attr:`PartialFillRepresentation.PER_FILL_RECORDS`.

This package is float-denylisted by ``scripts/lint_no_floats.py``: no ``/`` true
division, no ``float`` literal, cast, or annotation appears here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from hedgekit.connector.fake import (
    _book_from_dict,
    _load_balance_semantics,
    _load_balances,
    _load_exchange,
    _load_fee_models,
    _load_markets,
    _parse_dt,
    _read_json,
)
from hedgekit.connector.fills import (
    DEFAULT_FEE_HAIRCUT_PPM,
    DEFAULT_MAX_PARTICIPATION_PPM,
    PAPER_FILL_MODEL_VERSION,
    TradePrint,
    resting_fill_quantity,
    walk_taker_fill,
)
from hedgekit.connector.interface import UnknownMarketError
from hedgekit.connector.models import Fill, OpenOrder, OrderBookLevel
from hedgekit.connector.semantics import PartialFillRepresentation
from hedgekit.numeric import ContractCentis, PricePips

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from typing import Any, Literal

    from hedgekit.connector.fees import FeeModel
    from hedgekit.connector.models import (
        BalanceSemantics,
        BalanceSnapshot,
        ExchangeStatus,
        NormalizedMarket,
        OrderBookSnapshot,
        Position,
    )

__all__ = ["PAPER_FILL_MODEL_VERSION", "PaperExchange", "PaperOrderIntent"]

#: Fee-schedule key used when a market has no ticker-specific schedule (mirrors
#: :data:`hedgekit.connector.fake._DEFAULT_FEE_KEY`).
_DEFAULT_FEE_KEY: Final[str] = "default"

#: Pips in a full ``$1`` payout: a NO price is the complement ``10_000 - yes``.
_COMPLEMENT_PIPS: Final[int] = 10_000


@dataclass(frozen=True, slots=True)
class PaperOrderIntent:
    """A normalized order intent accepted by :meth:`PaperExchange.place_order`.

    Attributes:
        ticker: The market to trade.
        side: Whether the order buys the YES or the NO side.
        price: The order's limit price, in pips (a YES or NO price per ``side``).
        quantity: The requested size, in contract-centis.
    """

    ticker: str
    side: Literal["yes", "no"]
    price: PricePips
    quantity: ContractCentis


@dataclass(frozen=True, slots=True)
class _SessionStep:
    """One replay step: a book snapshot and the prints that followed it.

    Attributes:
        book: The order-book snapshot recorded at this step.
        trades: The trade prints recorded during this step, in order.
    """

    book: OrderBookSnapshot
    trades: tuple[TradePrint, ...]


@dataclass(frozen=True, slots=True)
class PaperPlacement:
    """The receipt returned by :meth:`PaperExchange.place_order`.

    Attributes:
        fills: The fills emitted by the immediate taker walk (one per consumed
            book level), in walk order.
        resting_order: The order left resting for the unfilled remainder, or
            None when the order filled completely.
    """

    fills: tuple[Fill, ...]
    resting_order: OpenOrder | None


def _trade_print_from_dict(data: Mapping[str, Any]) -> TradePrint:
    """Build a :class:`TradePrint` from one raw ``trades`` fixture entry."""
    return TradePrint(
        price=PricePips(data["price"]),
        quantity=ContractCentis(data["quantity"]),
        ts=_parse_dt(data["ts"]),
    )


def _session_step_from_dict(ticker: str, data: Mapping[str, Any]) -> _SessionStep:
    """Build a :class:`_SessionStep` from one raw session-step fixture entry."""
    return _SessionStep(
        book=_book_from_dict(ticker, data["book"]),
        trades=tuple(_trade_print_from_dict(trade) for trade in data["trades"]),
    )


def _load_sessions(directory: Path) -> dict[str, tuple[_SessionStep, ...]]:
    """Load ``sessions.json`` into a ticker-keyed tuple of replay steps."""
    data = _read_json(directory.joinpath("sessions.json"))
    return {
        ticker: tuple(_session_step_from_dict(ticker, step) for step in steps)
        for ticker, steps in data.items()
    }


class PaperExchange:
    """A replay-driven, pessimistic paper-trading :class:`MarketConnector`.

    The connector replays a per-ticker session of recorded books and trade
    prints. Crossing orders fill immediately via the pessimistic taker walk;
    remainders rest and fill only on a later step's trade-through. Balances are
    static (no debit modeling -- out of scope for issue #19).

    Attributes:
        markets: Ticker-keyed normalized markets.
        sessions: Ticker-keyed replay steps.
        exchange_status: The exchange's trading status.
        exchange_time: The exchange's server time.
        balances: The account's (static) balances.
        balance_semantics: The venue's balance-interpretation semantics.
        fee_models: Fee schedules keyed by ticker (plus a ``default``).
        haircut_ppm: The slippage haircut applied to modeled fees, in ppm.
        max_participation_ppm: The participation cap on recorded depth, in ppm.
    """

    def __init__(
        self,
        *,
        markets: Mapping[str, NormalizedMarket],
        sessions: Mapping[str, tuple[_SessionStep, ...]],
        exchange_status: ExchangeStatus,
        exchange_time: datetime,
        balances: BalanceSnapshot,
        balance_semantics: BalanceSemantics,
        fee_models: Mapping[str, FeeModel],
        haircut_ppm: int = DEFAULT_FEE_HAIRCUT_PPM,
        max_participation_ppm: int = DEFAULT_MAX_PARTICIPATION_PPM,
    ) -> None:
        """Initialize the paper exchange and validate fill-record consistency.

        Args:
            markets: Ticker-keyed normalized markets.
            sessions: Ticker-keyed replay steps.
            exchange_status: The exchange's trading status.
            exchange_time: The exchange's server time.
            balances: The account's static balances.
            balance_semantics: The venue's balance-interpretation semantics.
            fee_models: Fee schedules keyed by ticker (plus a ``default``).
            haircut_ppm: The slippage haircut on modeled fees, in ppm.
            max_participation_ppm: The participation cap on recorded depth, in ppm.

        Raises:
            ValueError: If ``balance_semantics.partial_fill_representation`` is
                not :attr:`PartialFillRepresentation.PER_FILL_RECORDS`; a taker
                walk emits one fill per consumed level, so aggregated partial
                fills would lose per-level prices (issue #18).
        """
        if (
            balance_semantics.partial_fill_representation
            is not PartialFillRepresentation.PER_FILL_RECORDS
        ):
            raise ValueError(
                "partial_fill_representation must be PER_FILL_RECORDS; "
                "the taker walk emits one Fill per consumed book level"
            )
        self.markets = markets
        self.sessions = sessions
        self.exchange_status = exchange_status
        self.exchange_time = exchange_time
        self.balances = balances
        self.balance_semantics = balance_semantics
        self.fee_models = fee_models
        self.haircut_ppm = haircut_ppm
        self.max_participation_ppm = max_participation_ppm
        self._cursor: dict[str, int] = dict.fromkeys(sessions, 0)
        self._resting: list[OpenOrder] = []
        self._fills: list[Fill] = []
        self._order_seq = 0
        self._fill_seq = 0

    @classmethod
    def from_fixture_dir(cls, path: str | Path, **overrides: int) -> PaperExchange:
        """Build a :class:`PaperExchange` from a fixture directory.

        Loads the :class:`~hedgekit.connector.fake.FakeExchange`-shaped static
        fixtures plus a ``sessions.json`` replay. Positions and fills start
        empty (the simulator produces its own fills as it runs).

        Args:
            path: The directory holding the JSON fixtures and ``sessions.json``.
            **overrides: Optional ``haircut_ppm`` / ``max_participation_ppm``
                keyword overrides forwarded to the constructor.

        Returns:
            A fully loaded paper exchange positioned at each session's step 0.
        """
        directory = Path(path)
        status, exchange_time = _load_exchange(directory)
        return cls(
            markets=_load_markets(directory),
            sessions=_load_sessions(directory),
            exchange_status=status,
            exchange_time=exchange_time,
            balances=_load_balances(directory),
            balance_semantics=_load_balance_semantics(directory),
            fee_models=_load_fee_models(directory),
            **overrides,
        )

    def _steps_for(self, ticker: str) -> tuple[_SessionStep, ...]:
        """Return the replay steps for ``ticker`` or raise ``UnknownMarketError``."""
        try:
            return self.sessions[ticker]
        except KeyError as exc:
            raise UnknownMarketError(ticker) from exc

    def _current_step(self, ticker: str) -> _SessionStep:
        """Return the step at ``ticker``'s current cursor (clamped to the last)."""
        steps = self._steps_for(ticker)
        index = min(self._cursor[ticker], len(steps) - 1)
        return steps[index]

    def list_markets(self) -> tuple[NormalizedMarket, ...]:
        """Return every market the session offers."""
        return tuple(self.markets.values())

    def get_market(self, ticker: str) -> NormalizedMarket:
        """Return the market for ``ticker`` or raise ``UnknownMarketError``."""
        try:
            return self.markets[ticker]
        except KeyError as exc:
            raise UnknownMarketError(ticker) from exc

    def get_order_book(self, ticker: str) -> OrderBookSnapshot:
        """Return the book at ``ticker``'s current replay cursor.

        Args:
            ticker: The market to look up.

        Returns:
            The order-book snapshot at the current step.

        Raises:
            UnknownMarketError: If ``ticker`` has no session.
        """
        return self._current_step(ticker).book

    def get_exchange_status(self) -> ExchangeStatus:
        """Return the fixture exchange status."""
        return self.exchange_status

    def get_exchange_time(self) -> datetime:
        """Return the fixture exchange time."""
        return self.exchange_time

    def get_balance_semantics(self) -> BalanceSemantics:
        """Return the fixture balance semantics."""
        return self.balance_semantics

    def get_balances(self) -> BalanceSnapshot:
        """Return the fixture balances (static; no debit modeling)."""
        return self.balances

    def get_positions(self) -> tuple[Position, ...]:
        """Return the account's positions (always empty in the paper sim)."""
        return ()

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Return the account's currently resting orders."""
        return tuple(self._resting)

    def get_fills(self, since: datetime) -> tuple[Fill, ...]:
        """Return simulated fills executed strictly after ``since``.

        Args:
            since: The exclusive lower bound; only fills with ``ts > since``
                are returned.

        Returns:
            The matching simulated fills, in emission order.
        """
        return tuple(fill for fill in self._fills if fill.ts > since)

    def get_fee_model(self, market_or_series: str) -> FeeModel:
        """Return the fee schedule for a ticker, falling back to ``default``.

        Args:
            market_or_series: The market ticker or series key to look up.

        Returns:
            The ticker-specific fee model when present, else the default one.
        """
        return self.fee_models.get(market_or_series, self.fee_models[_DEFAULT_FEE_KEY])

    def advance(self) -> bool:
        """Consume every session's current step, then step each cursor forward.

        Each ticker's current step is processed against its resting orders (a
        recorded trade-through fills, appending one :class:`Fill` per resting
        order that fills), and its cursor advances by one.

        Returns:
            True if any session still has an unconsumed step after advancing;
            False once every session's last step has been consumed.
        """
        any_next = False
        for ticker, steps in self.sessions.items():
            index = self._cursor[ticker]
            if index >= len(steps):
                continue
            self._process_resting_fills(ticker, steps[index])
            self._cursor[ticker] = index + 1
            any_next = any_next or index + 1 < len(steps)
        return any_next

    def _process_resting_fills(self, ticker: str, step: _SessionStep) -> None:
        """Fill this ticker's resting orders against ``step``'s trade prints."""
        survivors: list[OpenOrder] = []
        for order in self._resting:
            if order.ticker != ticker:
                survivors.append(order)
                continue
            survivor = self._resting_fill(order, step)
            if survivor is not None:
                survivors.append(survivor)
        self._resting = survivors

    def _resting_fill(self, order: OpenOrder, step: _SessionStep) -> OpenOrder | None:
        """Apply ``step``'s trade-through fill to one resting ``order``.

        Args:
            order: The resting order to test against the step.
            step: The replay step supplying the book depth and trade prints.

        Returns:
            The order with its remaining quantity reduced, or None if it filled
            completely and should be dropped.
        """
        limit, prints, depth = self._resting_fill_inputs(order, step)
        filled = resting_fill_quantity(
            limit,
            order.quantity,
            prints,
            depth,
            max_participation_ppm=self.max_participation_ppm,
        )
        if filled.value <= 0:
            return order
        self._emit_fill(order.ticker, order.side, order.price, filled, step.book)
        remaining = order.quantity.value - filled.value
        if remaining <= 0:
            return None
        return replace(order, quantity=ContractCentis(remaining))

    def _resting_fill_inputs(
        self, order: OpenOrder, step: _SessionStep
    ) -> tuple[PricePips, tuple[TradePrint, ...], ContractCentis]:
        """Project a resting order and step into buy-frame fill inputs.

        :func:`resting_fill_quantity` is written for a resting *buy* (a print
        strictly below the limit is a trade-through). A YES bid is already in
        that frame; a NO bid is complemented (``10_000 - price``) so the same
        buy-frame logic applies symmetrically.

        Args:
            order: The resting order to project.
            step: The replay step supplying the book and trade prints.

        Returns:
            The buy-frame limit, trade prints, and at-or-better depth.
        """
        if order.side == "yes":
            return self._yes_resting_inputs(order, step)
        return self._no_resting_inputs(order, step)

    def _yes_resting_inputs(
        self, order: OpenOrder, step: _SessionStep
    ) -> tuple[PricePips, tuple[TradePrint, ...], ContractCentis]:
        """Return buy-frame fill inputs for a resting YES bid (already in frame)."""
        depth = sum(
            level.quantity.value
            for level in step.book.yes_bids
            if level.price >= order.price
        )
        return order.price, step.trades, ContractCentis(depth)

    def _no_resting_inputs(
        self, order: OpenOrder, step: _SessionStep
    ) -> tuple[PricePips, tuple[TradePrint, ...], ContractCentis]:
        """Return buy-frame fill inputs for a resting NO bid (complemented)."""
        prints = tuple(
            replace(trade, price=PricePips(_COMPLEMENT_PIPS - trade.price.value))
            for trade in step.trades
        )
        depth = sum(
            level.quantity.value
            for level in step.book.yes_asks
            if _COMPLEMENT_PIPS - level.price.value >= order.price.value
        )
        return order.price, prints, ContractCentis(depth)

    def place_order(
        self, normalized_intent: object, approval_token: object
    ) -> PaperPlacement:
        """Place a :class:`PaperOrderIntent`, filling any crossing portion.

        The crossing portion fills immediately via the pessimistic taker walk
        (one :class:`Fill` per consumed level); any remainder rests at the
        order's own limit. The approval token is accepted and ignored -- paper
        trading has no real risk-kernel gate.

        Args:
            normalized_intent: The order intent; must be a :class:`PaperOrderIntent`.
            approval_token: Accepted and ignored.

        Returns:
            A :class:`PaperPlacement` receipt of the fills and any resting order.

        Raises:
            TypeError: If ``normalized_intent`` is not a :class:`PaperOrderIntent`.
            UnknownMarketError: If the intent's ticker has no session.
        """
        del approval_token
        if not isinstance(normalized_intent, PaperOrderIntent):
            raise TypeError(
                "normalized_intent must be a PaperOrderIntent, "
                f"got {type(normalized_intent).__name__}"
            )
        step = self._current_step(normalized_intent.ticker)
        result = walk_taker_fill(
            self._taker_levels(normalized_intent, step.book),
            normalized_intent.price,
            normalized_intent.quantity,
            self.get_fee_model(normalized_intent.ticker),
            haircut_ppm=self.haircut_ppm,
            max_participation_ppm=self.max_participation_ppm,
        )
        fills = self._emit_taker_fills(normalized_intent, result.consumed, step.book)
        remainder = normalized_intent.quantity.value - result.filled.value
        resting = self._rest_remainder(normalized_intent, remainder)
        return PaperPlacement(fills=fills, resting_order=resting)

    def _taker_levels(
        self, intent: PaperOrderIntent, book: OrderBookSnapshot
    ) -> tuple[OrderBookLevel, ...]:
        """Return the book side a crossing ``intent`` walks, best-first.

        A YES order crosses the recorded YES asks directly; a NO order crosses
        the complement of the YES bids (``10_000 - price``), which a best-first
        (descending) bid book turns into a best-first (ascending) NO-ask book.

        Args:
            intent: The order being placed.
            book: The current book snapshot.

        Returns:
            The eligible book side for the taker walk, best-first.
        """
        if intent.side == "yes":
            return book.yes_asks
        return tuple(
            OrderBookLevel(
                PricePips(_COMPLEMENT_PIPS - level.price.value), level.quantity
            )
            for level in book.yes_bids
        )

    def _emit_taker_fills(
        self,
        intent: PaperOrderIntent,
        consumed: Sequence[OrderBookLevel],
        book: OrderBookSnapshot,
    ) -> tuple[Fill, ...]:
        """Emit one :class:`Fill` per consumed level of a taker walk."""
        fills: list[Fill] = []
        for level in consumed:
            fills.append(
                self._emit_fill(
                    intent.ticker, intent.side, level.price, level.quantity, book
                )
            )
        return tuple(fills)

    def _rest_remainder(
        self, intent: PaperOrderIntent, remainder: int
    ) -> OpenOrder | None:
        """Rest an order for the unfilled ``remainder`` at the intent's limit."""
        if remainder <= 0:
            return None
        self._order_seq += 1
        order = OpenOrder(
            id=f"paper-order-{self._order_seq}",
            ticker=intent.ticker,
            side=intent.side,
            price=intent.price,
            quantity=ContractCentis(remainder),
        )
        self._resting.append(order)
        return order

    def _emit_fill(
        self,
        ticker: str,
        side: Literal["yes", "no"],
        price: PricePips,
        quantity: ContractCentis,
        book: OrderBookSnapshot,
    ) -> Fill:
        """Record and return one simulated fill timestamped at the book time."""
        self._fill_seq += 1
        fill = Fill(
            id=f"paper-fill-{self._fill_seq}",
            ticker=ticker,
            side=side,
            price=price,
            quantity=quantity,
            ts=book.fetched_at,
        )
        self._fills.append(fill)
        return fill

    def cancel_order(self, order_id: str) -> None:
        """Remove the resting order with ``order_id`` (a no-op if absent).

        Args:
            order_id: The identifier of the resting order to cancel.
        """
        self._resting = [order for order in self._resting if order.id != order_id]
