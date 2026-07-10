"""Failing-first tests for `windbreak.evaluation.execution_quality` (issue #58, RED).

`windbreak.evaluation.execution_quality` does not exist yet, so every test below
imports its new symbols (`ExecutionQualityRecord`, `compare_fill_to_model`,
`live_slippage_ratio`, `ExecutionQualityRecorded`) as the FIRST statement inside
the test body -- matching this package's established RED convention in
`test_dual_path.py` / `test_preregistration.py` -- so each test collects and
fails independently on its own
`ModuleNotFoundError: No module named 'windbreak.evaluation.execution_quality'`.
Symbols from already-existing modules (`windbreak.connector.models`,
`windbreak.connector.fees`, `windbreak.connector.fills`, `windbreak.numeric`,
`windbreak.evaluation.cohorts`) are imported at module scope.

Pins issue #58's live-vs-paper slippage series (SPEC §17.4):

- `ExecutionQualityRecord` is a frozen dataclass whose `slippage_micros` is
  always `actual_cost_micros - modeled_cost_micros` (derived, not
  caller-supplied), and whose integer fields bool-guard exactly like
  `FixtureForecast.__post_init__`.
- `compare_fill_to_model` re-derives the paper-model cost for a real fill by
  calling `windbreak.connector.fills.walk_taker_fill` over the *same recorded
  book* the live fill executed against, so `modeled_cost_micros` is
  independently reproducible from `book_cost + fee + haircut`.
- `live_slippage_ratio(records)` is
  `ceil(sum(actual_cost_micros) * 1_000_000 / sum(modeled_cost_micros))`,
  cost-side `OVERSTATE_COST` (ceiling) throughout, over an empty record set it
  returns the existing `cohorts.UNDEFINED` sentinel (empty-but-valid, never an
  error) -- mirroring `traded_vs_skipped_brier_delta`'s empty-cohort handling.
- PR #199 review fix: a non-empty record set whose modeled-cost sum is
  exactly `0` must raise a documented `ValueError` (mirroring
  `calibration_slope`/`calibration_intercept`'s zero-variance guard), not the
  bare `ZeroDivisionError` `divide()` currently raises unhandled.

ASSUMPTION this file pins (the architecture plan gives `compare_fill_to_model`'s
signature as `compare_fill_to_model(fill, book_levels, fee_model, *,
haircut_ppm, max_participation_ppm)` but does not name the concrete shape of
`fill`): a live fill observation carries at minimum a `limit` (`PricePips`) and
`requested` (`ContractCentis`) -- the two fields `walk_taker_fill` itself
requires -- plus an `actual_cost_micros` (the real observed cost) and enough
identity fields (`fill_id`, `market_ticker`, `side`, `model_version`,
`created_sequence`) to build the resulting `ExecutionQualityRecord`. This test
file's local `_ObservedFill` dataclass names that minimal shape; if the real
`fill` parameter type differs, this is a design point to reconcile with the
architect, not silently patched to match whichever lands first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from windbreak.connector.fees import FeeModel
from windbreak.connector.fills import walk_taker_fill
from windbreak.connector.models import OrderBookLevel
from windbreak.evaluation import cohorts
from windbreak.numeric import ContractCentis, MoneyMicros, PricePips

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Fixture directory for this suite's known-answer slippage-ratio cases.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "execution_quality"

#: A fixed, obviously-synthetic paper fill-model version pinning every fixture
#: record's `model_version` field to one identity across this suite.
_PFM_VERSION = "pfm-known-answer-v1"

#: A zero-fee schedule so `compare_fill_to_model`'s reference `total_cost` in
#: this suite's per-fill test reduces to the exact book cost alone (no fee, no
#: haircut), keeping the hand-derivation in
#: `test_compare_fill_to_model_matches_hand_walked_reference` free of the fee
#: model's own quadratic-bound arithmetic.
_ZERO_FEE_MODEL = FeeModel(
    schedule_id="zero-fee-test-schedule",
    maker_fee_ppm=0,
    taker_fee_ppm=0,
    settlement_fee_ppm=0,
)


@dataclass(frozen=True, slots=True)
class _ObservedFill:
    """Minimal live-fill observation shape this suite feeds `compare_fill_to_model`.

    See the module docstring's ASSUMPTION for why this local shape exists
    rather than importing a concrete type from the (not-yet-written)
    `execution_quality` module.
    """

    fill_id: str
    market_ticker: str
    side: str
    limit: PricePips
    requested: ContractCentis
    actual_cost_micros: int
    model_version: str
    created_sequence: int


def _load_fixture(name: str) -> dict[str, Any]:
    """Load and JSON-decode one known-answer execution-quality fixture.

    Args:
        name: The fixture file's stem (without ``.json``).

    Returns:
        The decoded fixture payload.
    """
    path = _FIXTURES_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _record_from_entry(entry: Mapping[str, Any], record_cls: type) -> object:
    """Build one `ExecutionQualityRecord` from a raw fixture fill entry.

    Args:
        entry: One decoded ``fills`` list entry.
        record_cls: The `ExecutionQualityRecord` class (passed in so this
            helper stays import-free of the not-yet-existing module at
            collection time).

    Returns:
        The constructed record.
    """
    return record_cls(
        fill_id=entry["fill_id"],
        market_ticker=entry["market_ticker"],
        side=entry["side"],
        filled_centis=entry["filled_centis"],
        actual_cost_micros=entry["actual_cost_micros"],
        modeled_cost_micros=entry["modeled_cost_micros"],
        model_version=entry["model_version"],
        created_sequence=entry["created_sequence"],
    )


# ---------------------------------------------------------------------------
# 1. ExecutionQualityRecord: derived slippage_micros, bool-guarded int fields.
# ---------------------------------------------------------------------------


def test_execution_quality_record_slippage_micros_is_actual_minus_modeled() -> None:
    """`slippage_micros` is always `actual_cost_micros - modeled_cost_micros`."""
    from windbreak.evaluation.execution_quality import ExecutionQualityRecord

    record = ExecutionQualityRecord(
        fill_id="F-1",
        market_ticker="MKT-A",
        side="YES",
        filled_centis=500,
        actual_cost_micros=2_600_000,
        modeled_cost_micros=2_500_000,
        model_version=_PFM_VERSION,
        created_sequence=1,
    )

    assert record.slippage_micros == 100_000


def test_execution_quality_record_negative_slippage_is_preserved() -> None:
    """A fill that filled cheaper than modeled carries a negative slippage."""
    from windbreak.evaluation.execution_quality import ExecutionQualityRecord

    record = ExecutionQualityRecord(
        fill_id="F-1",
        market_ticker="MKT-A",
        side="YES",
        filled_centis=500,
        actual_cost_micros=2_400_000,
        modeled_cost_micros=2_500_000,
        model_version=_PFM_VERSION,
        created_sequence=1,
    )

    assert record.slippage_micros == -100_000


@pytest.mark.parametrize(
    "field_name",
    ["filled_centis", "actual_cost_micros", "modeled_cost_micros", "created_sequence"],
)
def test_execution_quality_record_bool_guards_int_fields(field_name: str) -> None:
    """Passing `True`/`False` for an int field raises `TypeError`, mirroring
    `FixtureForecast.__post_init__`'s bool-vs-int guard.
    """
    from windbreak.evaluation.execution_quality import ExecutionQualityRecord

    kwargs: dict[str, object] = {
        "fill_id": "F-1",
        "market_ticker": "MKT-A",
        "side": "YES",
        "filled_centis": 500,
        "actual_cost_micros": 100_000,
        "modeled_cost_micros": 90_000,
        "model_version": _PFM_VERSION,
        "created_sequence": 1,
    }
    kwargs[field_name] = True

    with pytest.raises(TypeError, match=field_name):
        ExecutionQualityRecord(**kwargs)


# ---------------------------------------------------------------------------
# 2. compare_fill_to_model: reproduces walk_taker_fill over the same book.
# ---------------------------------------------------------------------------


def test_compare_fill_to_model_matches_hand_walked_reference() -> None:
    """`compare_fill_to_model`'s `modeled_cost_micros` equals a hand-walked
    `walk_taker_fill` over the identical book, fee model, and haircut/
    participation knobs; `slippage_micros` follows the actual-minus-modeled
    convention.

    Book: one level at 5_000 pips, 1_000 centis deep. Order: limit 5_000,
    requested 500 centis, zero-fee schedule, `haircut_ppm=0`,
    `max_participation_ppm=1_000_000` (no cap). The exact
    `money_from_price_and_count` product is `5_000 * 500 == 2_500_000` micros
    with zero fee and zero haircut, so `total_cost.value == 2_500_000` exactly
    -- no rounding ambiguity anywhere in this reference computation.
    """
    from windbreak.evaluation.execution_quality import compare_fill_to_model

    book_levels = (OrderBookLevel(PricePips(5_000), ContractCentis(1_000)),)
    reference = walk_taker_fill(
        book_levels,
        PricePips(5_000),
        ContractCentis(500),
        _ZERO_FEE_MODEL,
        haircut_ppm=0,
        max_participation_ppm=1_000_000,
    )
    assert reference.total_cost == MoneyMicros(2_500_000)

    fill = _ObservedFill(
        fill_id="F-live-1",
        market_ticker="MKT-EXEC-LIVE",
        side="YES",
        limit=PricePips(5_000),
        requested=ContractCentis(500),
        actual_cost_micros=2_600_000,
        model_version=_PFM_VERSION,
        created_sequence=42,
    )

    record = compare_fill_to_model(
        fill,
        book_levels,
        _ZERO_FEE_MODEL,
        haircut_ppm=0,
        max_participation_ppm=1_000_000,
    )

    assert record.modeled_cost_micros == reference.total_cost.value == 2_500_000
    assert record.actual_cost_micros == 2_600_000
    assert record.slippage_micros == 100_000


def test_compare_fill_to_model_negative_slippage_sign_convention() -> None:
    """A live fill cheaper than the modeled reference yields negative slippage."""
    from windbreak.evaluation.execution_quality import compare_fill_to_model

    book_levels = (OrderBookLevel(PricePips(5_000), ContractCentis(1_000)),)
    fill = _ObservedFill(
        fill_id="F-live-2",
        market_ticker="MKT-EXEC-LIVE",
        side="YES",
        limit=PricePips(5_000),
        requested=ContractCentis(500),
        actual_cost_micros=2_400_000,
        model_version=_PFM_VERSION,
        created_sequence=43,
    )

    record = compare_fill_to_model(
        fill,
        book_levels,
        _ZERO_FEE_MODEL,
        haircut_ppm=0,
        max_participation_ppm=1_000_000,
    )

    assert record.modeled_cost_micros == 2_500_000
    assert record.slippage_micros == -100_000


# ---------------------------------------------------------------------------
# 3. live_slippage_ratio: known-answer fixtures, empty-set sentinel, rolling
#    window truncation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "expected_ppm"),
    [
        ("known_answer_basic", 1_153_847),
        ("known_answer_negative_slippage", 833_334),
    ],
)
def test_live_slippage_ratio_known_answer_fixtures(
    fixture_name: str, expected_ppm: int
) -> None:
    """`live_slippage_ratio` matches the fixture's hand-computed pinned integer.

    Both fixtures assert their own ``expected.live_slippage_ratio_ppm`` in their
    docstring-equivalent ``description`` key, verified independently here so a
    change to either the fixture or the pinned constant is caught.
    """
    from windbreak.evaluation.execution_quality import (
        ExecutionQualityRecord,
        live_slippage_ratio,
    )

    payload = _load_fixture(fixture_name)
    assert payload["expected"]["live_slippage_ratio_ppm"] == expected_ppm
    records = tuple(
        _record_from_entry(entry, ExecutionQualityRecord) for entry in payload["fills"]
    )

    ratio = live_slippage_ratio(records)

    assert ratio == expected_ppm


def test_live_slippage_ratio_empty_records_is_undefined() -> None:
    """An empty record set is empty-but-valid: the `cohorts.UNDEFINED` sentinel,
    never an exception -- mirroring `traded_vs_skipped_brier_delta`.
    """
    from windbreak.evaluation.execution_quality import live_slippage_ratio

    ratio = live_slippage_ratio(())

    assert ratio is cohorts.UNDEFINED


def test_live_slippage_ratio_zero_modeled_cost_sum_raises_value_error() -> None:
    """A non-empty record set whose modeled-cost sum is exactly `0` must raise a
    clear, documented `ValueError` -- mirroring `calibration_slope` /
    `calibration_intercept`'s zero-variance guard (`test_metrics.py`) -- rather
    than letting `divide`'s bare `ZeroDivisionError` escape uncaught.

    Today `live_slippage_ratio` has no zero-modeled-cost guard at all: it
    passes `modeled_sum == 0` straight into `divide(...)`, which raises
    `ZeroDivisionError` (not `ValueError`), so this currently fails -- the
    unhandled `ZeroDivisionError` propagates instead of the expected
    `ValueError`.
    """
    from windbreak.evaluation.execution_quality import (
        ExecutionQualityRecord,
        live_slippage_ratio,
    )

    records = (
        ExecutionQualityRecord(
            fill_id="F-zero-model",
            market_ticker="MKT-ZERO",
            side="YES",
            filled_centis=100,
            actual_cost_micros=1_000_000,
            modeled_cost_micros=0,
            model_version=_PFM_VERSION,
            created_sequence=1,
        ),
    )

    with pytest.raises(ValueError, match=r"(?i)modeled"):
        live_slippage_ratio(records)


def test_live_slippage_ratio_rolling_window_truncation_is_observable() -> None:
    """Only the most-recent `live_rolling_window_size` (100) records feed the
    ratio; including 5 much-older, much-worse records changes the answer.

    105 fills are built here in Python (rather than hand-transcribed as a
    105-entry JSON fixture -- impractical to hand-author and verify, the same
    rationale `test_dual_path.py`'s module docstring documents for the OLS
    sums) with two constant per-fill cost pairs, so both the windowed and the
    full-set sums are exact hand arithmetic, not merely "many similar rows":

    - 5 "old" fills (`created_sequence` 1-5): `actual=1_000_000`,
      `modeled=100_000` each.
    - 100 "recent" fills (`created_sequence` 6-105): `actual=110_000`,
      `modeled=100_000` each.

    Windowed (last 100 by `created_sequence` desc -- the 100 "recent" fills
    only): `sum(actual) = 100 * 110_000 = 11_000_000`,
    `sum(modeled) = 100 * 100_000 = 10_000_000`;
    `ratio = ceil(11_000_000 * 1_000_000 / 10_000_000) = 1_100_000` exactly (no
    remainder) -- comfortably under the confirmed
    `live_slippage_ratio_limit_ppm` default of `1_500_000`.

    Full, untruncated set (all 105): `sum(actual) = 5_000_000 + 11_000_000 =
    16_000_000`, `sum(modeled) = 500_000 + 10_000_000 = 10_500_000`;
    `ratio = ceil(16_000_000_000_000 / 10_500_000) = 1_523_810` -- *over* the
    threshold. The two answers disagree in exactly the direction that matters:
    without truncation the monitor would wrongly signal a breach.
    """
    from windbreak.evaluation.execution_quality import (
        ExecutionQualityRecord,
        live_slippage_ratio,
    )

    old_fills = tuple(
        ExecutionQualityRecord(
            fill_id=f"F-old-{sequence}",
            market_ticker="MKT-OLD",
            side="YES",
            filled_centis=100,
            actual_cost_micros=1_000_000,
            modeled_cost_micros=100_000,
            model_version=_PFM_VERSION,
            created_sequence=sequence,
        )
        for sequence in range(1, 6)
    )
    recent_fills = tuple(
        ExecutionQualityRecord(
            fill_id=f"F-recent-{sequence}",
            market_ticker="MKT-RECENT",
            side="YES",
            filled_centis=100,
            actual_cost_micros=110_000,
            modeled_cost_micros=100_000,
            model_version=_PFM_VERSION,
            created_sequence=sequence,
        )
        for sequence in range(6, 106)
    )
    all_fills = old_fills + recent_fills
    assert len(all_fills) == 105

    windowed = tuple(
        sorted(all_fills, key=lambda record: record.created_sequence, reverse=True)
    )[:100]
    assert len(windowed) == 100
    assert all(record.market_ticker == "MKT-RECENT" for record in windowed)

    windowed_ratio = live_slippage_ratio(windowed)
    full_ratio = live_slippage_ratio(all_fills)

    assert windowed_ratio == 1_100_000
    assert full_ratio == 1_523_810
    assert windowed_ratio != full_ratio


# ---------------------------------------------------------------------------
# 4. ExecutionQualityRecorded ledger event: payload shape.
# ---------------------------------------------------------------------------


def test_execution_quality_recorded_event_carries_the_full_record() -> None:
    """`ExecutionQualityRecorded`'s payload names every `ExecutionQualityRecord`
    field, so a reader can reconstruct the exact record from the ledger alone.
    """
    from windbreak.evaluation.execution_quality import (
        ExecutionQualityRecord,
        ExecutionQualityRecorded,
    )

    record = ExecutionQualityRecord(
        fill_id="F-1",
        market_ticker="MKT-A",
        side="YES",
        filled_centis=500,
        actual_cost_micros=2_600_000,
        modeled_cost_micros=2_500_000,
        model_version=_PFM_VERSION,
        created_sequence=7,
    )

    event = ExecutionQualityRecorded(component="evaluation", record=record)

    assert event.event_type == "ExecutionQualityRecorded"
    assert event.payload["fill_id"] == "F-1"
    assert event.payload["market_ticker"] == "MKT-A"
    assert event.payload["actual_cost_micros"] == 2_600_000
    assert event.payload["modeled_cost_micros"] == 2_500_000
    assert event.payload["slippage_micros"] == 100_000
    assert event.payload["model_version"] == _PFM_VERSION
    assert event.payload["created_sequence"] == 7
