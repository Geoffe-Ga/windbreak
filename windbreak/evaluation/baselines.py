"""Reference-baseline computation for the evaluation harness (SPEC-EPIC_07, #50).

Every forecast is scored not in isolation but against a handful of dumb
*baselines* -- the comparison points a real forecast must beat to demonstrate
skill. This module derives, for each forecast, five such baselines from its own
referenced quote snapshot and the market's history:

    * ``executable_price_ppm`` (PRIMARY): the snapshot's ``yes_ask_pips`` -- the
      price you could actually buy at -- converted to ppm.
    * ``midpoint_ppm``: the bid/ask midpoint, converted to ppm.
    * ``uniform_ppm``: the constant :data:`UNIFORM_BASELINE_PPM` (a flat 50%).
    * ``base_rate_ppm``: the market's base rate where one is known, else ``None``.
    * ``previous_forecast_ppm``: the same market's last-seen forecast probability
      in forecast order, or ``None`` for the first forecast on a market.

All conversions are *exact integer multiplications* -- there is deliberately no
division and therefore no :class:`windbreak.numeric.rounding.RoundingDirection`
anywhere in this module. A pip is 1e-4 payout-dollars and a ppm is 1e-6
probability, so one pip is exactly ``100`` ppm; the bid/ask midpoint halving is
kept exact by computing in ppm space as ``(bid + ask) * 50`` rather than ever
dividing a pip value by two. Both factors are exact, so no remainder can ever
arise and no rounding decision is required.

This is a leaf module: it imports only :mod:`windbreak.numeric.types` and is
imported by nothing except the package ``__init__``, preserving the one-way
intra-package import graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from windbreak.numeric.types import PricePips, ProbabilityPpm

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any, Final

#: Exact ppm-per-pip factor: 1 pip (1e-4 $) equals 100 ppm (1e-6). Because it is
#: exact, no division and no ``RoundingDirection`` is ever needed here.
_PPM_PER_PIP: Final = 100
#: Applied to the bid+ask pip *sum* so the midpoint's halving stays exact in ppm
#: space: ``(bid + ask) / 2`` pips converted at 100 ppm/pip equals
#: ``(bid + ask) * 50`` ppm, with no rounding.
_MIDPOINT_PPM_PER_PIP_SUM: Final = 50
#: Inclusive minimum yes-price in pips: prices are bounded below by zero.
_MIN_PRICE_PIPS: Final = 0
#: Inclusive maximum yes-price in pips: a binary "yes" cannot exceed $1.00 full
#: payout (10_000 pips).
_MAX_PRICE_PIPS: Final = 10_000

#: The uniform (max-entropy) baseline: a flat 50%, i.e. 500_000 ppm.
UNIFORM_BASELINE_PPM: Final = ProbabilityPpm(500_000)

#: JSON key holding the ordered list of forecast rows.
_FORECASTS_KEY = "forecasts"
#: JSON key holding the list of quote-snapshot entries.
_QUOTE_SNAPSHOTS_KEY = "quote_snapshots"
#: JSON key holding the list of per-market base-rate entries.
_BASE_RATES_KEY = "base_rates"
#: JSON field naming a forecast's stable identifier.
_FORECAST_ID_FIELD = "forecast_id"
#: JSON field naming the market a forecast or base rate belongs to.
_MARKET_TICKER_FIELD = "market_ticker"
#: JSON field carrying a forecast's probability, in ppm.
_PROBABILITY_FIELD = "probability_ppm"
#: JSON field naming the quote snapshot a forecast's baselines read.
_BASELINE_SNAPSHOT_FIELD = "baseline_quote_snapshot_id"
#: JSON field naming a quote snapshot's stable identifier.
_SNAPSHOT_ID_FIELD = "snapshot_id"
#: JSON field carrying a snapshot's yes-bid price, in pips.
_YES_BID_FIELD = "yes_bid_pips"
#: JSON field carrying a snapshot's yes-ask price, in pips.
_YES_ASK_FIELD = "yes_ask_pips"
#: JSON field carrying a market's base rate, in ppm.
_BASE_RATE_FIELD = "base_rate_ppm"


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    """A single yes-side bid/ask quote observed for a market.

    Attributes:
        snapshot_id: Stable identifier the referencing forecast points at.
        yes_bid_pips: The best yes-side bid price, in pips.
        yes_ask_pips: The best yes-side ask (executable buy) price, in pips.
    """

    snapshot_id: str
    yes_bid_pips: PricePips
    yes_ask_pips: PricePips

    def __post_init__(self) -> None:
        """Validate the quote is well-formed and within the payout bounds.

        Raises:
            ValueError: If the bid is negative, the bid exceeds the ask (a
                crossed quote), or the ask exceeds full payout; the message
                names the offending ``bid`` or ``ask`` side.
        """
        bid = self.yes_bid_pips.value
        ask = self.yes_ask_pips.value
        if bid < _MIN_PRICE_PIPS:
            raise ValueError(f"yes_bid_pips must be >= {_MIN_PRICE_PIPS}, got {bid}")
        if bid > ask:
            raise ValueError(
                f"yes_bid_pips {bid} must not exceed yes_ask_pips {ask} "
                "(a crossed quote is not a valid market state)"
            )
        if ask > _MAX_PRICE_PIPS:
            raise ValueError(
                f"yes_ask_pips {ask} must not exceed {_MAX_PRICE_PIPS} pips "
                "($1.00 full payout)"
            )


@dataclass(frozen=True, slots=True)
class BaselineForecast:
    """The forecast fields the baseline computation reads.

    Attributes:
        forecast_id: Stable identifier of the forecast record.
        market_ticker: Ticker of the market this forecast is about.
        probability_ppm: The forecast probability, in ppm.
        baseline_quote_snapshot_id: Identifier of the quote snapshot this
            forecast's price baselines read.
    """

    forecast_id: str
    market_ticker: str
    probability_ppm: ProbabilityPpm
    baseline_quote_snapshot_id: str


@dataclass(frozen=True, slots=True)
class BaselineInputs:
    """The immutable inputs the baseline computation folds over.

    Attributes:
        forecasts: The forecasts to compute baselines for, in order.
        quote_snapshots: Every available quote snapshot, keyed by ``snapshot_id``.
        base_rates: Known per-market base rates, keyed by ``market_ticker``.
    """

    forecasts: tuple[BaselineForecast, ...]
    quote_snapshots: Mapping[str, QuoteSnapshot]
    base_rates: Mapping[str, ProbabilityPpm]


@dataclass(frozen=True, slots=True)
class BaselineSet:
    """The five reference baselines computed for one forecast.

    Attributes:
        forecast_id: The forecast these baselines belong to.
        executable_price_ppm: The primary baseline -- the snapshot's executable
            ask price in ppm.
        midpoint_ppm: The bid/ask midpoint in ppm.
        uniform_ppm: The constant uniform (50%) baseline.
        base_rate_ppm: The market's base rate in ppm, or ``None`` if unknown.
        previous_forecast_ppm: The same market's prior forecast probability, or
            ``None`` for the first forecast on the market.
    """

    forecast_id: str
    executable_price_ppm: ProbabilityPpm
    midpoint_ppm: ProbabilityPpm
    uniform_ppm: ProbabilityPpm
    base_rate_ppm: ProbabilityPpm | None
    previous_forecast_ppm: ProbabilityPpm | None


def _resolve_snapshot(
    forecast: BaselineForecast, snapshots: Mapping[str, QuoteSnapshot]
) -> QuoteSnapshot:
    """Look up the quote snapshot a forecast's baselines read.

    Args:
        forecast: The forecast whose referenced snapshot is needed.
        snapshots: Every available snapshot, keyed by ``snapshot_id``.

    Returns:
        The snapshot named by the forecast's ``baseline_quote_snapshot_id``.

    Raises:
        ValueError: If no snapshot matches; the message names both the
            ``baseline_quote_snapshot_id`` field and the offending
            ``forecast_id``.
    """
    snapshot = snapshots.get(forecast.baseline_quote_snapshot_id)
    if snapshot is None:
        raise ValueError(
            "baseline_quote_snapshot_id "
            f"{forecast.baseline_quote_snapshot_id!r} referenced by forecast "
            f"{forecast.forecast_id!r} has no matching quote snapshot"
        )
    return snapshot


def _primary_ppm(snapshot: QuoteSnapshot) -> ProbabilityPpm:
    """Convert a snapshot's executable ask price to a ppm baseline.

    Args:
        snapshot: The forecast's own quote snapshot.

    Returns:
        The executable ask price in ppm, exact via ``ask * 100``.
    """
    return ProbabilityPpm(snapshot.yes_ask_pips.value * _PPM_PER_PIP)


def _midpoint_ppm(snapshot: QuoteSnapshot) -> ProbabilityPpm:
    """Convert a snapshot's bid/ask midpoint to a ppm baseline.

    Args:
        snapshot: The forecast's own quote snapshot.

    Returns:
        The midpoint in ppm, kept exact via ``(bid + ask) * 50``.
    """
    pip_sum = snapshot.yes_bid_pips.value + snapshot.yes_ask_pips.value
    return ProbabilityPpm(pip_sum * _MIDPOINT_PPM_PER_PIP_SUM)


def compute_baselines(inputs: BaselineInputs) -> tuple[BaselineSet, ...]:
    """Compute the five reference baselines for every forecast, in order.

    A single pass tracks each market's last-seen forecast probability so the
    ``previous_forecast_ppm`` baseline is the prior forecast on the *same*
    market -- ``None`` for the first, never a zero-filled ``ProbabilityPpm(0)``
    -- even when forecasts on different markets are interleaved.

    Args:
        inputs: The forecasts, snapshots, and base rates to compute over.

    Returns:
        One :class:`BaselineSet` per forecast, in the input order.

    Raises:
        ValueError: If a forecast references a snapshot that does not exist; the
            message names the field and the offending forecast.
    """
    baselines: list[BaselineSet] = []
    last_probability: dict[str, ProbabilityPpm] = {}
    for forecast in inputs.forecasts:
        snapshot = _resolve_snapshot(forecast, inputs.quote_snapshots)
        baselines.append(
            BaselineSet(
                forecast_id=forecast.forecast_id,
                executable_price_ppm=_primary_ppm(snapshot),
                midpoint_ppm=_midpoint_ppm(snapshot),
                uniform_ppm=UNIFORM_BASELINE_PPM,
                base_rate_ppm=inputs.base_rates.get(forecast.market_ticker),
                previous_forecast_ppm=last_probability.get(forecast.market_ticker),
            )
        )
        last_probability[forecast.market_ticker] = forecast.probability_ppm
    return tuple(baselines)


def _quote_snapshot_from_entry(entry: Mapping[str, Any]) -> QuoteSnapshot:
    """Build a :class:`QuoteSnapshot` from one raw fixture snapshot entry.

    Args:
        entry: The decoded snapshot object from the fixture.

    Returns:
        The typed, validated quote snapshot.

    Raises:
        TypeError: If a pips field carries a ``bool`` masquerading as an int
            (rejected by :class:`~windbreak.numeric.types.PricePips`).
        ValueError: If the resulting quote is crossed or out of bounds.
    """
    return QuoteSnapshot(
        snapshot_id=entry[_SNAPSHOT_ID_FIELD],
        yes_bid_pips=PricePips(entry[_YES_BID_FIELD]),
        yes_ask_pips=PricePips(entry[_YES_ASK_FIELD]),
    )


def _snapshots_by_id(fixture: Mapping[str, Any]) -> dict[str, QuoteSnapshot]:
    """Index the fixture's ``quote_snapshots`` block by ``snapshot_id``.

    Args:
        fixture: The decoded fixture payload.

    Returns:
        A mapping from each ``snapshot_id`` to its :class:`QuoteSnapshot`.

    Raises:
        ValueError: If two entries share a ``snapshot_id``; the message names
            the ``snapshot_id`` field.
    """
    snapshots: dict[str, QuoteSnapshot] = {}
    for entry in fixture[_QUOTE_SNAPSHOTS_KEY]:
        snapshot = _quote_snapshot_from_entry(entry)
        if snapshot.snapshot_id in snapshots:
            raise ValueError(
                f"duplicate snapshot_id in quote_snapshots: {snapshot.snapshot_id!r}"
            )
        snapshots[snapshot.snapshot_id] = snapshot
    return snapshots


def _forecast_from_entry(entry: Mapping[str, Any]) -> BaselineForecast:
    """Build a :class:`BaselineForecast` from one raw fixture forecast entry.

    Args:
        entry: The decoded forecast object from the fixture.

    Returns:
        The typed forecast row.

    Raises:
        TypeError: If ``probability_ppm`` carries a ``bool`` masquerading as an
            int (rejected by :class:`~windbreak.numeric.types.ProbabilityPpm`).
    """
    return BaselineForecast(
        forecast_id=entry[_FORECAST_ID_FIELD],
        market_ticker=entry[_MARKET_TICKER_FIELD],
        probability_ppm=ProbabilityPpm(entry[_PROBABILITY_FIELD]),
        baseline_quote_snapshot_id=entry[_BASELINE_SNAPSHOT_FIELD],
    )


def _forecasts_in_order(fixture: Mapping[str, Any]) -> tuple[BaselineForecast, ...]:
    """Build the fixture's ``forecasts`` block in order, rejecting duplicates.

    Args:
        fixture: The decoded fixture payload.

    Returns:
        The forecasts in fixture order.

    Raises:
        ValueError: If two entries share a ``forecast_id``; the message names
            the ``forecast_id`` field.
    """
    forecasts: list[BaselineForecast] = []
    seen: set[str] = set()
    for entry in fixture[_FORECASTS_KEY]:
        forecast = _forecast_from_entry(entry)
        if forecast.forecast_id in seen:
            raise ValueError(
                f"duplicate forecast_id in forecasts: {forecast.forecast_id!r}"
            )
        seen.add(forecast.forecast_id)
        forecasts.append(forecast)
    return tuple(forecasts)


def _base_rates_by_ticker(fixture: Mapping[str, Any]) -> dict[str, ProbabilityPpm]:
    """Index the fixture's ``base_rates`` block by ``market_ticker``.

    Args:
        fixture: The decoded fixture payload.

    Returns:
        A mapping from each ``market_ticker`` to its base rate in ppm.
    """
    base_rates: dict[str, ProbabilityPpm] = {}
    for entry in fixture[_BASE_RATES_KEY]:
        ticker = entry[_MARKET_TICKER_FIELD]
        base_rates[ticker] = ProbabilityPpm(entry[_BASE_RATE_FIELD])
    return base_rates


def baseline_inputs_from_fixture(fixture: Mapping[str, Any]) -> BaselineInputs:
    """Build typed :class:`BaselineInputs` from a fixture payload.

    Reads the fixture's ``forecasts`` (via the ``baseline_quote_snapshot_id``
    key), ``quote_snapshots``, and ``base_rates`` blocks.

    Args:
        fixture: The decoded fixture payload.

    Returns:
        The typed baseline inputs.

    Raises:
        ValueError: If a ``snapshot_id`` or ``forecast_id`` is duplicated, or a
            quote snapshot is malformed.
        TypeError: If a numeric field carries a ``bool`` masquerading as an int.
    """
    return BaselineInputs(
        forecasts=_forecasts_in_order(fixture),
        quote_snapshots=_snapshots_by_id(fixture),
        base_rates=_base_rates_by_ticker(fixture),
    )
