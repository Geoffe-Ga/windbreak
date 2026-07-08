"""Pure, pytest-free JSON-to-domain loader for selector test fixtures (#43).

`load_inputs` parses one recorded "bundle" JSON file (see
`tests/selector/fixtures/bundle_a.json` / `bundle_b.json`) into a real
`hedgekit.selector.SelectorInputs` -- built from the actual, post-init
validated `hedgekit.forecast.records.ForecastRecord` and
`hedgekit.connector.models.OrderBookSnapshot` domain types, with every
arithmetic-bearing value wrapped in its `hedgekit.numeric` scaled-integer unit
type at load time (mirroring `hedgekit.connector.fake.FakeExchange`'s
`_market_from_dict` / `_book_from_dict` fixture-loading convention). No
float ever appears in a bundle file or in the objects built from it.

This module is deliberately free of any `pytest` import: `test_determinism_
golden.py`'s fresh-interpreter check imports it from a bare `python -c`
subprocess with no test runner present, so it must be importable and usable
on its own.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from hedgekit.connector.models import OrderBookLevel, OrderBookSnapshot
from hedgekit.forecast.records import Citation, ForecastRecord, ModelVote
from hedgekit.numeric import ContractCentis, PricePips
from hedgekit.selector import SelectorInputs
from hedgekit.selector.types import (
    FeeModelRef,
    PositionReadModelRef,
    RiskConfigRef,
    SlippageModelRef,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a UTC-normalized datetime.

    Args:
        value: An ISO-8601 string, e.g. ``"2024-12-10T12:00:00.000000Z"``.

    Returns:
        The timezone-aware datetime, normalized to UTC.
    """
    return datetime.fromisoformat(value).astimezone(UTC)


def _parse_optional_dt(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 timestamp, preserving ``None``.

    Args:
        value: An ISO-8601 string, or ``None``.

    Returns:
        The parsed UTC datetime, or ``None`` when ``value`` is ``None``.
    """
    return None if value is None else _parse_dt(value)


def _model_vote_from_dict(data: Mapping[str, object]) -> ModelVote:
    """Build a :class:`ModelVote` from one raw ``model_votes`` entry.

    Args:
        data: One raw ``model_votes`` list entry from a bundle JSON file.

    Returns:
        The constructed, post-init-validated :class:`ModelVote`.
    """
    return ModelVote(
        provider=data["provider"],
        model_version=data["model_version"],
        declared_training_cutoff=data["declared_training_cutoff"],
        probability_ppm=data["probability_ppm"],
        response_fingerprint=data["response_fingerprint"],
    )


def _citation_from_dict(data: Mapping[str, object]) -> Citation:
    """Build a :class:`Citation` from one raw ``citations`` entry.

    Args:
        data: One raw ``citations`` list entry from a bundle JSON file.

    Returns:
        The constructed :class:`Citation`, with its ``publication_date``
        parsed from an ISO-8601 string (or preserved as ``None``).
    """
    return Citation(
        url=data["url"],
        content_hash=data["content_hash"],
        quoted_text=data["quoted_text"],
        publication_date=_parse_optional_dt(data["publication_date"]),
        source_type=data["source_type"],
    )


def _forecast_record_from_dict(data: Mapping[str, object]) -> ForecastRecord:
    """Build a :class:`ForecastRecord` from a bundle's ``forecast`` object.

    Args:
        data: The raw ``forecast`` mapping from a bundle JSON file.

    Returns:
        The constructed, post-init-validated :class:`ForecastRecord`.
    """
    return ForecastRecord(
        forecast_id=data["forecast_id"],
        market_ticker=data["market_ticker"],
        normalized_question_hash=data["normalized_question_hash"],
        probability_ppm=data["probability_ppm"],
        ci_low_ppm=data["ci_low_ppm"],
        ci_high_ppm=data["ci_high_ppm"],
        model_votes=tuple(_model_vote_from_dict(vote) for vote in data["model_votes"]),
        vote_dispersion_ppm=data["vote_dispersion_ppm"],
        rationale_markdown=data["rationale_markdown"],
        citations=tuple(
            _citation_from_dict(citation) for citation in data["citations"]
        ),
        source_quality_notes=tuple(data["source_quality_notes"]),
        research_cost_micros=data["research_cost_micros"],
        triage_stage=data["triage_stage"],
        created_at=_parse_dt(data["created_at"]),
        forecast_horizon_hours=data["forecast_horizon_hours"],
        market_price_baseline_pips=data["market_price_baseline_pips"],
        baseline_quote_snapshot_id=data["baseline_quote_snapshot_id"],
        coherence_group_sum_ppm=data["coherence_group_sum_ppm"],
        coherence_flag=data["coherence_flag"],
        abstention_reason=data["abstention_reason"],
        eligible_for_live=data["eligible_for_live"],
    )


def _order_book_level_from_dict(data: Mapping[str, object]) -> OrderBookLevel:
    """Build a unit-wrapped :class:`OrderBookLevel` from one raw level entry.

    Args:
        data: One raw ``yes_bids``/``yes_asks`` list entry.

    Returns:
        The constructed :class:`OrderBookLevel`, with ``price`` and
        ``quantity`` wrapped in their scaled-integer unit types.
    """
    return OrderBookLevel(
        price=PricePips(data["price"]),
        quantity=ContractCentis(data["quantity"]),
    )


def _order_book_from_dict(data: Mapping[str, object]) -> OrderBookSnapshot:
    """Build an :class:`OrderBookSnapshot` from a bundle's ``order_book`` object.

    Args:
        data: The raw ``order_book`` mapping from a bundle JSON file.

    Returns:
        The constructed :class:`OrderBookSnapshot`.
    """
    return OrderBookSnapshot(
        ticker=data["ticker"],
        yes_bids=tuple(
            _order_book_level_from_dict(level) for level in data["yes_bids"]
        ),
        yes_asks=tuple(
            _order_book_level_from_dict(level) for level in data["yes_asks"]
        ),
        fetched_at=_parse_dt(data["fetched_at"]),
    )


def load_inputs(path: str | Path) -> SelectorInputs:
    """Parse one recorded bundle JSON file into a `SelectorInputs`.

    Args:
        path: Path to a bundle JSON file (e.g.
            ``tests/selector/fixtures/bundle_a.json``).

    Returns:
        A fully constructed `SelectorInputs`, with its `forecast` and
        `order_book` built from the real, post-init-validated domain types
        and every placeholder ref wrapped in its `hedgekit.selector.types`
        dataclass.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return SelectorInputs(
        forecast=_forecast_record_from_dict(raw["forecast"]),
        calibration_map_version=raw["calibration_map_version"],
        order_book=_order_book_from_dict(raw["order_book"]),
        fee_model=FeeModelRef(raw["fee_model_id"]),
        slippage_model=SlippageModelRef(raw["slippage_model_id"]),
        positions=PositionReadModelRef(raw["positions_snapshot_id"]),
        risk_config=RiskConfigRef(raw["risk_config_hash"]),
        correlation_tags=tuple(raw["correlation_tags"]),
    )
