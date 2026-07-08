"""Failing-first tests for the evaluation baselines module (issue #50, RED).

`hedgekit.evaluation.baselines` does not exist yet, so every import below
fails collection with `ModuleNotFoundError: No module named
'hedgekit.evaluation.baselines'` -- the expected Gate 1 RED state for issue
#50.

Pins the reference-baseline computation (SPEC-EPIC_07, #50): for every
forecast, `compute_baselines` derives five comparison points --

- `executable_price_ppm` (PRIMARY): the forecast's own quote snapshot's
  `yes_ask_pips`, converted to ppm by an *exact* `* 100` (1 pip == 100 ppm,
  never rounded).
- `midpoint_ppm`: `(yes_bid_pips + yes_ask_pips) * 50`, computed directly in
  ppm space so the halving is always exact.
- `uniform_ppm`: the constant `UNIFORM_BASELINE_PPM` (500_000, i.e. 50%).
- `base_rate_ppm`: the market's base rate where available, else `None`.
- `previous_forecast_ppm`: the same market's last-seen `probability_ppm` in
  forecast order, or `None` for the first forecast on a market -- never a
  zero-filled `ProbabilityPpm(0)`.

A baseline always reads its own forecast's referenced snapshot, never a
later one that happens to exist for the same market.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hedgekit.numeric.types import PricePips, ProbabilityPpm

#: The epic-wide known-answer fixture shared by issues #49-#55; see its own
#: "description" key for the hand-computed baseline arithmetic this suite
#: pins against.
SYNTHETIC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_known_answer.json"
)

#: Per-forecast (in fixture order) hand-computed primary baseline ppm values:
#: yes_ask_pips * 100 for each forecast's own quote snapshot.
_EXPECTED_PRIMARY_PPM = [
    880000,
    150000,
    720000,
    320000,
    550000,
    480000,
    790000,
    220000,
    990000,
    10000,
]

#: Per-forecast (in fixture order) hand-computed midpoint baseline ppm
#: values: (yes_bid_pips + yes_ask_pips) * 50 for each forecast's own quote
#: snapshot.
_EXPECTED_MIDPOINT_PPM = [
    870000,
    140000,
    710000,
    310000,
    540000,
    470000,
    780000,
    210000,
    980000,
    5000,
]

#: Base rates pinned into the fixture's `base_rates` block; present only for
#: MKT-01..MKT-05.
_EXPECTED_BASE_RATE_PPM_BY_TICKER = {
    "MKT-01": 600000,
    "MKT-02": 200000,
    "MKT-03": 650000,
    "MKT-04": 400000,
    "MKT-05": 500000,
}

#: A minimal valid `forecasts` entry for the loader-error tests below, which
#: exercise `baseline_inputs_from_fixture` directly rather than the full
#: synthetic fixture.
_MINIMAL_BASELINE_FORECAST_ENTRY: dict[str, Any] = {
    "forecast_id": "fc-1",
    "market_ticker": "MKT-A",
    "probability_ppm": 500000,
    "baseline_quote_snapshot_id": "qs-1",
}

#: A minimal valid `quote_snapshots` entry paired with the forecast above.
_MINIMAL_SNAPSHOT_ENTRY: dict[str, Any] = {
    "snapshot_id": "qs-1",
    "yes_bid_pips": 100,
    "yes_ask_pips": 200,
}


def _load_fixture() -> dict[str, Any]:
    """Load and JSON-decode the shared synthetic known-answer fixture.

    Returns:
        The decoded fixture payload.
    """
    return json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))


def _assert_no_float_leaves(value: object, *, path: str = "$") -> None:
    """Recursively assert that no leaf in a decoded JSON structure is a float.

    Duplicated locally from `test_skeleton.py` (rather than imported cross
    test-module) to keep this suite import-isolated; both copies must agree
    that SPEC S6.1 bans float leaves anywhere in a fixture.

    Args:
        value: A JSON-decoded value (dict, list, or scalar) to inspect.
        path: A breadcrumb path used to make failures locatable.

    Raises:
        AssertionError: If any leaf in the structure is a `float`.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_no_float_leaves(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_float_leaves(child, path=f"{path}[{index}]")
    else:
        assert not isinstance(value, float), f"float leaf found at {path}: {value!r}"


# ---------------------------------------------------------------------------
# 1. Full fixture integration: every hand-computed value, for every forecast.
# ---------------------------------------------------------------------------


def test_compute_baselines_matches_every_hand_computed_value_over_the_fixture() -> None:
    """`compute_baselines` over the full synthetic fixture reproduces, for
    every one of the 10 forecasts and in fixture order: the primary
    (executable-price) ppm, the midpoint ppm, the constant uniform ppm, the
    base rate (present for MKT-01..05, `None` for MKT-06..10), and a `None`
    previous-forecast baseline (every forecast here is the first, and only,
    forecast on its market).
    """
    from hedgekit.evaluation.baselines import (
        UNIFORM_BASELINE_PPM,
        baseline_inputs_from_fixture,
        compute_baselines,
    )

    fixture = _load_fixture()
    inputs = baseline_inputs_from_fixture(fixture)

    baselines = compute_baselines(inputs)

    assert len(baselines) == 10
    assert ProbabilityPpm(500_000) == UNIFORM_BASELINE_PPM

    forecast_tickers = [forecast["market_ticker"] for forecast in fixture["forecasts"]]
    forecast_ids = [forecast["forecast_id"] for forecast in fixture["forecasts"]]

    for index, baseline in enumerate(baselines):
        ticker = forecast_tickers[index]
        expected_base_rate = _EXPECTED_BASE_RATE_PPM_BY_TICKER.get(ticker)

        assert baseline.forecast_id == forecast_ids[index]
        assert baseline.executable_price_ppm == ProbabilityPpm(
            _EXPECTED_PRIMARY_PPM[index]
        )
        assert baseline.midpoint_ppm == ProbabilityPpm(_EXPECTED_MIDPOINT_PPM[index])
        assert baseline.uniform_ppm == UNIFORM_BASELINE_PPM
        if expected_base_rate is None:
            assert baseline.base_rate_ppm is None
        else:
            assert baseline.base_rate_ppm == ProbabilityPpm(expected_base_rate)
        # Every fixture forecast is the only forecast on its market: there is
        # never a previous forecast to compare against.
        assert baseline.previous_forecast_ppm is None


def test_compute_baselines_preserves_forecast_order() -> None:
    """`compute_baselines` returns one `BaselineSet` per forecast, in the
    same order the forecasts were given in.
    """
    from hedgekit.evaluation.baselines import (
        baseline_inputs_from_fixture,
        compute_baselines,
    )

    fixture = _load_fixture()
    inputs = baseline_inputs_from_fixture(fixture)

    baselines = compute_baselines(inputs)

    expected_ids = [forecast["forecast_id"] for forecast in fixture["forecasts"]]
    assert [baseline.forecast_id for baseline in baselines] == expected_ids


# ---------------------------------------------------------------------------
# 2. Primary baseline reads its own snapshot, never a later one.
# ---------------------------------------------------------------------------


def test_primary_baseline_reads_own_snapshot_never_a_later_one() -> None:
    """Two forecasts on the same market point at two different snapshots
    (`qs-01`, the one the real fixture forecast uses, and the decoy
    `qs-01-late`); each forecast's primary baseline reads only its own
    referenced snapshot -- the existence of a later snapshot for the same
    market never leaks into an earlier forecast's baseline.
    """
    from hedgekit.evaluation.baselines import (
        BaselineForecast,
        BaselineInputs,
        QuoteSnapshot,
        compute_baselines,
    )

    early_snapshot = QuoteSnapshot(
        snapshot_id="qs-01",
        yes_bid_pips=PricePips(8600),
        yes_ask_pips=PricePips(8800),
    )
    late_snapshot = QuoteSnapshot(
        snapshot_id="qs-01-late",
        yes_bid_pips=PricePips(9300),
        yes_ask_pips=PricePips(9500),
    )
    forecast_early = BaselineForecast(
        forecast_id="fc-early",
        market_ticker="MKT-01",
        probability_ppm=ProbabilityPpm(900_000),
        baseline_quote_snapshot_id="qs-01",
    )
    forecast_late = BaselineForecast(
        forecast_id="fc-late",
        market_ticker="MKT-01",
        probability_ppm=ProbabilityPpm(900_000),
        baseline_quote_snapshot_id="qs-01-late",
    )
    inputs = BaselineInputs(
        forecasts=(forecast_early, forecast_late),
        quote_snapshots={"qs-01": early_snapshot, "qs-01-late": late_snapshot},
        base_rates={},
    )

    baselines = compute_baselines(inputs)

    assert baselines[0].executable_price_ppm == ProbabilityPpm(880_000)
    assert baselines[1].executable_price_ppm == ProbabilityPpm(950_000)


# ---------------------------------------------------------------------------
# 3. Previous-forecast baseline: omitted, not zero-filled; per-ticker order.
# ---------------------------------------------------------------------------


def test_previous_forecast_baseline_omitted_not_zero_filled_and_per_ticker() -> None:
    """The first forecast on each market gets `previous_forecast_ppm=None`
    (never `ProbabilityPpm(0)`); a later forecast on the same market gets
    the prior forecast's own `probability_ppm`, tracked per market ticker
    even when forecasts on different markets are interleaved.
    """
    from hedgekit.evaluation.baselines import (
        BaselineForecast,
        BaselineInputs,
        QuoteSnapshot,
        compute_baselines,
    )

    snapshot = QuoteSnapshot(
        snapshot_id="qs-shared", yes_bid_pips=PricePips(0), yes_ask_pips=PricePips(100)
    )
    forecast_a1 = BaselineForecast(
        forecast_id="fc-a1",
        market_ticker="MKT-X",
        probability_ppm=ProbabilityPpm(300_000),
        baseline_quote_snapshot_id="qs-shared",
    )
    forecast_b1 = BaselineForecast(
        forecast_id="fc-b1",
        market_ticker="MKT-Y",
        probability_ppm=ProbabilityPpm(400_000),
        baseline_quote_snapshot_id="qs-shared",
    )
    forecast_a2 = BaselineForecast(
        forecast_id="fc-a2",
        market_ticker="MKT-X",
        probability_ppm=ProbabilityPpm(350_000),
        baseline_quote_snapshot_id="qs-shared",
    )
    inputs = BaselineInputs(
        # Interleaved: MKT-X, MKT-Y, MKT-X -- the tracker must key the
        # "previous forecast" by market_ticker, not by stream position.
        forecasts=(forecast_a1, forecast_b1, forecast_a2),
        quote_snapshots={"qs-shared": snapshot},
        base_rates={},
    )

    baselines = compute_baselines(inputs)
    by_id = {baseline.forecast_id: baseline for baseline in baselines}

    assert by_id["fc-a1"].previous_forecast_ppm is None
    assert by_id["fc-b1"].previous_forecast_ppm is None
    assert by_id["fc-a2"].previous_forecast_ppm == ProbabilityPpm(300_000)
    # The omitted case is genuinely absent, not a disguised zero.
    assert by_id["fc-a1"].previous_forecast_ppm != ProbabilityPpm(0)


# ---------------------------------------------------------------------------
# 4. Exactness pin: integer arithmetic only, no rounding, anywhere.
# ---------------------------------------------------------------------------


def test_midpoint_and_primary_baselines_use_exact_integer_arithmetic() -> None:
    """A tiny 1/2-pip snapshot proves the conversions are exact integer
    multiplications with no rounding: midpoint `(1 + 2) * 50 == 150` ppm,
    primary `2 * 100 == 200` ppm.
    """
    from hedgekit.evaluation.baselines import (
        BaselineForecast,
        BaselineInputs,
        QuoteSnapshot,
        compute_baselines,
    )

    snapshot = QuoteSnapshot(
        snapshot_id="qs-tiny", yes_bid_pips=PricePips(1), yes_ask_pips=PricePips(2)
    )
    forecast = BaselineForecast(
        forecast_id="fc-tiny",
        market_ticker="MKT-TINY",
        probability_ppm=ProbabilityPpm(500_000),
        baseline_quote_snapshot_id="qs-tiny",
    )
    inputs = BaselineInputs(
        forecasts=(forecast,),
        quote_snapshots={"qs-tiny": snapshot},
        base_rates={},
    )

    baselines = compute_baselines(inputs)

    assert baselines[0].midpoint_ppm == ProbabilityPpm(150)
    assert baselines[0].executable_price_ppm == ProbabilityPpm(200)


def test_synthetic_fixture_extension_has_no_float_leaf_anywhere() -> None:
    """The new `quote_snapshots`, `base_rates`, and `settlement_events`
    blocks obey the same "no float leaves" rule as the rest of the fixture
    (SPEC S6.1's integer-only money/probability path).
    """
    fixture = _load_fixture()

    _assert_no_float_leaves(fixture)


# ---------------------------------------------------------------------------
# 5. QuoteSnapshot construction invariants.
# ---------------------------------------------------------------------------


def test_quote_snapshot_rejects_bid_above_ask() -> None:
    """`yes_bid_pips > yes_ask_pips` raises `ValueError` -- a crossed quote
    is not a valid market state.
    """
    from hedgekit.evaluation.baselines import QuoteSnapshot

    with pytest.raises(ValueError, match="bid"):
        QuoteSnapshot(
            snapshot_id="qs-crossed",
            yes_bid_pips=PricePips(500),
            yes_ask_pips=PricePips(400),
        )


def test_quote_snapshot_rejects_ask_above_ten_thousand_pips() -> None:
    """`yes_ask_pips > 10_000` (i.e. above $1.00) raises `ValueError` -- a
    binary "yes" price can never exceed full payout.
    """
    from hedgekit.evaluation.baselines import QuoteSnapshot

    with pytest.raises(ValueError, match="ask"):
        QuoteSnapshot(
            snapshot_id="qs-over",
            yes_bid_pips=PricePips(9_000),
            yes_ask_pips=PricePips(10_001),
        )


def test_quote_snapshot_rejects_negative_bid() -> None:
    """A negative `yes_bid_pips` raises `ValueError` -- prices are bounded
    below by zero.
    """
    from hedgekit.evaluation.baselines import QuoteSnapshot

    with pytest.raises(ValueError, match="bid"):
        QuoteSnapshot(
            snapshot_id="qs-negative",
            yes_bid_pips=PricePips(-1),
            yes_ask_pips=PricePips(100),
        )


def test_quote_snapshot_rejects_bool_as_int_pips() -> None:
    """A `bool` masquerading as a pips value raises `TypeError`, inherited
    from `hedgekit.numeric.types._IntUnit`'s "no bool-as-int" guard -- the
    guard fires while constructing the `PricePips` passed into
    `QuoteSnapshot`, before `QuoteSnapshot.__post_init__` ever runs.
    """
    from hedgekit.evaluation.baselines import QuoteSnapshot

    with pytest.raises(TypeError):
        QuoteSnapshot(
            snapshot_id="qs-bool",
            yes_bid_pips=PricePips(True),
            yes_ask_pips=PricePips(100),
        )


# ---------------------------------------------------------------------------
# 6. compute_baselines / loader error paths, each naming the offending field.
# ---------------------------------------------------------------------------


def test_compute_baselines_rejects_unknown_snapshot_id_naming_field_and_forecast() -> (
    None
):
    """A `BaselineForecast` whose `baseline_quote_snapshot_id` has no entry
    in `quote_snapshots` raises `ValueError` naming both the
    `baseline_quote_snapshot_id` field and the offending `forecast_id`.
    """
    from hedgekit.evaluation.baselines import (
        BaselineForecast,
        BaselineInputs,
        compute_baselines,
    )

    forecast = BaselineForecast(
        forecast_id="fc-orphan",
        market_ticker="MKT-X",
        probability_ppm=ProbabilityPpm(500_000),
        baseline_quote_snapshot_id="qs-does-not-exist",
    )
    inputs = BaselineInputs(forecasts=(forecast,), quote_snapshots={}, base_rates={})

    with pytest.raises(ValueError) as exc_info:
        compute_baselines(inputs)

    message = str(exc_info.value)
    assert "baseline_quote_snapshot_id" in message
    assert "fc-orphan" in message


def test_baseline_inputs_from_fixture_rejects_duplicate_snapshot_id() -> None:
    """Two `quote_snapshots` entries sharing a `snapshot_id` raise
    `ValueError` naming `snapshot_id`.
    """
    from hedgekit.evaluation.baselines import baseline_inputs_from_fixture

    fixture = {
        "forecasts": [_MINIMAL_BASELINE_FORECAST_ENTRY],
        "quote_snapshots": [_MINIMAL_SNAPSHOT_ENTRY, _MINIMAL_SNAPSHOT_ENTRY],
        "base_rates": [],
    }

    with pytest.raises(ValueError, match="snapshot_id"):
        baseline_inputs_from_fixture(fixture)


def test_baseline_inputs_from_fixture_rejects_duplicate_forecast_id() -> None:
    """Two `forecasts` entries sharing a `forecast_id` raise `ValueError`
    naming `forecast_id`.
    """
    from hedgekit.evaluation.baselines import baseline_inputs_from_fixture

    duplicate_forecast = {
        **_MINIMAL_BASELINE_FORECAST_ENTRY,
        "market_ticker": "MKT-B",
    }
    fixture = {
        "forecasts": [_MINIMAL_BASELINE_FORECAST_ENTRY, duplicate_forecast],
        "quote_snapshots": [_MINIMAL_SNAPSHOT_ENTRY],
        "base_rates": [],
    }

    with pytest.raises(ValueError, match="forecast_id"):
        baseline_inputs_from_fixture(fixture)
