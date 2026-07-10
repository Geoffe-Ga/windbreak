"""Failing-first tests for `windbreak.evaluation.cohorts` (issue #53, RED).

`windbreak.evaluation.cohorts` does not exist yet, so every test below imports
its new symbols from that module as the FIRST statement inside the test body
(matching this package's established RED convention in
`test_temporal_integrity.py`) so each test collects independently and fails on
its own `ModuleNotFoundError: No module named 'windbreak.evaluation.cohorts'`.

Pins SPEC-EPIC_07 issue #53's selection-bias cohort taxonomy:

- `Cohort`: `ALL`, `TRADED`, `SKIPPED`, `ABOVE_THRESHOLD`, `ABSTAINED`,
  `EXCLUDED_BY_LIQUIDITY`, `EXCLUDED_BY_CATEGORY`.
- `assign_cohorts(forecast) -> frozenset[Cohort]`, with the totality invariant
  that `ALL` is always present and exactly one of `TRADED`/`SKIPPED` is
  present.
- `cohort_brier_table(inputs, *, window)` -- one `CohortBrier` row per
  `Cohort` (always seven), each carrying the cohort's mean Brier (in ppm) over
  its window-resolved, cohort-narrowed admitted inputs, or the `UNDEFINED`
  sentinel for an empty/unresolved cohort.
- `traded_vs_skipped_brier_delta(inputs, *, window) -> int` --
  `mean_brier(SKIPPED) - mean_brier(TRADED)`, in ppm; positive means TRADED
  outperformed, negative means SKIPPED outperformed. Raises `ValueError` when
  either cohort has no resolved records.
- `mean_brier_over(slices, inputs)` -- slice-accepting, mixing-guarded (see
  `test_windows.py` for the `MixedObservationWindowError` pin).

Every hand-computed expected value below is independently re-derived from the
shared `synthetic_known_answer.json` fixture in each test's own docstring, not
copied from any external plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from windbreak.evaluation import (
    EvaluationInputs,
    FixtureForecast,
    ObservationWindow,
    ResolutionOutcome,
    Track,
    run_evaluation,
)
from windbreak.numeric.types import ProbabilityPpm

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The epic-wide known-answer fixture shared by issues #49-#55.
SYNTHETIC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_known_answer.json"
)

#: The window every direct `cohorts` call below uses (matches `report.py`'s
#: documented choice for issue #53's cohort/abstention computation).
_WINDOW = ObservationWindow.LATEST_BEFORE_CLOSE


def _forecast(
    *,
    forecast_id: str,
    market_ticker: str,
    probability_ppm: int,
    baseline_pips: int,
    traded: bool = True,
    eligible: bool = True,
    abstention: str | None = None,
    created_sequence: int = 1,
    live: bool = False,
) -> FixtureForecast:
    """Build one `FixtureForecast` with the fields these cohort tests vary.

    Every record carries a non-`None` `created_sequence` because
    `cohort_brier_table` / `traded_vs_skipped_brier_delta` now resolve their
    window via `windows.resolve_window`, and the `LATEST_BEFORE_CLOSE` window
    requires a non-`None` `created_sequence` on every record (admitted records
    always carry one). The default is fine for the singleton-per-market records
    below -- per-market selection is an identity no-op when a market has exactly
    one record regardless of the sequence value; only the multi-forecast-
    per-market test varies it explicitly to make selection observable.

    Args:
        forecast_id: Stable identifier of the forecast record.
        market_ticker: Ticker of the market this forecast is about.
        probability_ppm: Forecast probability, in ppm.
        baseline_pips: The reference executable price, in pips.
        traded: Whether a live trade was actually taken.
        eligible: Whether the forecast passed live-eligibility gates.
        abstention: The abstention reason, or `None` if traded.
        created_sequence: The forecast's creation sequence on the ledger.
        live: Whether this forecast is on the LIVE track (vs PAPER); defaults
            to `False` so every pre-existing call site is unaffected.

    Returns:
        The constructed `FixtureForecast`.
    """
    return FixtureForecast(
        forecast_id=forecast_id,
        market_ticker=market_ticker,
        probability_ppm=ProbabilityPpm(probability_ppm),
        eligible_for_live=eligible,
        abstention_reason=abstention,
        traded=traded,
        baseline_executable_price_pips=baseline_pips,
        created_sequence=created_sequence,
        live=live,
    )


def _forecast_from_entry(entry: Mapping[str, Any]) -> FixtureForecast:
    """Build a `FixtureForecast` from one raw fixture forecast entry.

    Includes `created_sequence` (unlike `test_metrics.py`'s equivalent
    helper), because `cohort_brier_table` resolves its window via
    `windows.resolve_window`, and `LATEST_BEFORE_CLOSE` requires a non-`None`
    `created_sequence` on every record.

    Args:
        entry: The decoded forecast object from a fixture.

    Returns:
        The typed, validated forecast row.
    """
    return FixtureForecast(
        forecast_id=entry["forecast_id"],
        market_ticker=entry["market_ticker"],
        probability_ppm=ProbabilityPpm(entry["probability_ppm"]),
        eligible_for_live=entry["eligible_for_live"],
        abstention_reason=entry["abstention_reason"],
        traded=entry["traded"],
        baseline_executable_price_pips=entry["baseline_executable_price_pips"],
        correlation_group_id=entry.get("correlation_group_id"),
        created_sequence=entry.get("created_sequence"),
    )


def _resolutions_from_entries(
    entries: list[Mapping[str, Any]],
) -> dict[str, ResolutionOutcome]:
    """Build a ticker-keyed resolution mapping from raw resolution entries.

    Args:
        entries: The decoded `resolutions` list.

    Returns:
        A mapping from `market_ticker` to its `ResolutionOutcome`.
    """
    return {
        entry["market_ticker"]: ResolutionOutcome(entry["outcome"]) for entry in entries
    }


def _synthetic_inputs() -> EvaluationInputs:
    """Build `EvaluationInputs` directly from the synthetic known-answer fixture.

    Returns:
        The typed evaluation inputs for the 10-forecast synthetic fixture.
    """
    payload = json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))
    forecasts = tuple(_forecast_from_entry(entry) for entry in payload["forecasts"])
    resolutions = _resolutions_from_entries(payload["resolutions"])
    return EvaluationInputs(forecasts=forecasts, resolutions=resolutions)


# ---------------------------------------------------------------------------
# 1. assign_cohorts: hand-built records hitting all seven cohorts, plus the
#    totality invariant (ALL always present; exactly one of TRADED/SKIPPED).
# ---------------------------------------------------------------------------

#: r1: traded, large edge -> {ALL, TRADED, ABOVE_THRESHOLD}.
_R1_TRADED_ABOVE_THRESHOLD = _forecast(
    forecast_id="r1",
    market_ticker="R1",
    probability_ppm=900_000,
    baseline_pips=1_000,
    traded=True,
)
#: r2: untraded, excluded by liquidity -> {ALL, SKIPPED, EXCLUDED_BY_LIQUIDITY}.
#: NOT ABSTAINED: the reason is in the liquidity-exclusion set.
_R2_EXCLUDED_BY_LIQUIDITY = _forecast(
    forecast_id="r2",
    market_ticker="R2",
    probability_ppm=500_000,
    baseline_pips=5_000,
    traded=False,
    eligible=False,
    abstention="low_liquidity",
)
#: r3: untraded, excluded by category -> {ALL, SKIPPED, EXCLUDED_BY_CATEGORY}.
_R3_EXCLUDED_BY_CATEGORY = _forecast(
    forecast_id="r3",
    market_ticker="R3",
    probability_ppm=500_000,
    baseline_pips=5_000,
    traded=False,
    eligible=False,
    abstention="excluded_category",
)
#: r4: untraded, ineligible, an ordinary (non-exclusion) abstention reason ->
#: {ALL, SKIPPED, ABSTAINED}.
_R4_ABSTAINED = _forecast(
    forecast_id="r4",
    market_ticker="R4",
    probability_ppm=500_000,
    baseline_pips=5_000,
    traded=False,
    eligible=False,
    abstention="risk_kernel_veto",
)
#: r5: untraded, ELIGIBLE, no abstention reason at all -> {ALL, SKIPPED} only
#: -- the "no-reason/eligible/untraded" record: not ABSTAINED (no reason,
#: and eligible besides), not any EXCLUDED_BY_* (no reason to match).
_R5_SKIPPED_ONLY = _forecast(
    forecast_id="r5",
    market_ticker="R5",
    probability_ppm=500_000,
    baseline_pips=5_000,
    traded=False,
    eligible=True,
    abstention=None,
)

_ALL_SEVEN_RECORDS = (
    _R1_TRADED_ABOVE_THRESHOLD,
    _R2_EXCLUDED_BY_LIQUIDITY,
    _R3_EXCLUDED_BY_CATEGORY,
    _R4_ABSTAINED,
    _R5_SKIPPED_ONLY,
)


def test_assign_cohorts_hits_all_seven_cohorts_across_hand_built_records() -> None:
    """Each hand-built record's cohort membership is exactly as designed;
    across the five records, every one of the seven `Cohort` members is hit
    by at least one record.
    """
    from windbreak.evaluation.cohorts import Cohort, assign_cohorts

    assert assign_cohorts(_R1_TRADED_ABOVE_THRESHOLD) == frozenset(
        {Cohort.ALL, Cohort.TRADED, Cohort.ABOVE_THRESHOLD}
    )
    assert assign_cohorts(_R2_EXCLUDED_BY_LIQUIDITY) == frozenset(
        {Cohort.ALL, Cohort.SKIPPED, Cohort.EXCLUDED_BY_LIQUIDITY}
    )
    assert assign_cohorts(_R3_EXCLUDED_BY_CATEGORY) == frozenset(
        {Cohort.ALL, Cohort.SKIPPED, Cohort.EXCLUDED_BY_CATEGORY}
    )
    assert assign_cohorts(_R4_ABSTAINED) == frozenset(
        {Cohort.ALL, Cohort.SKIPPED, Cohort.ABSTAINED}
    )
    assert assign_cohorts(_R5_SKIPPED_ONLY) == frozenset({Cohort.ALL, Cohort.SKIPPED})

    hit_cohorts: set[Any] = set()
    for record in _ALL_SEVEN_RECORDS:
        hit_cohorts |= assign_cohorts(record)
    assert hit_cohorts == set(Cohort)


def test_assign_cohorts_totality_invariant_holds_for_every_record() -> None:
    """`ALL` is present in every record's cohort set, and exactly one of
    `TRADED`/`SKIPPED` is present -- never both, never neither.
    """
    from windbreak.evaluation.cohorts import Cohort, assign_cohorts

    for record in _ALL_SEVEN_RECORDS:
        cohorts = assign_cohorts(record)
        assert Cohort.ALL in cohorts
        assert len(cohorts & {Cohort.TRADED, Cohort.SKIPPED}) == 1


# ---------------------------------------------------------------------------
# 2. cohort_brier_table over the shared synthetic fixture: every value below
#    is re-derived independently (per-forecast Brier terms computed from
#    scratch, not copied from any plan) -- see each assertion's inline math.
# ---------------------------------------------------------------------------


def test_cohort_brier_table_matches_hand_computation_on_synthetic_fixture() -> None:
    """Hand-derivation over the 10 synthetic-fixture forecasts.

    Per-forecast Brier term `(p_ppm - outcome_ppm)^2`, in ppm^2 (o=YES is
    1_000_000, o=NO is 0):

        eval-fc-01 MKT-01 p=900_000 traded=T outcome=YES -> (100_000)^2 = 1e10
        eval-fc-02 MKT-02 p=100_000 traded=F outcome=NO  -> (100_000)^2 = 1e10
        eval-fc-03 MKT-03 p=700_000 traded=T outcome=YES -> (300_000)^2 = 9e10
        eval-fc-04 MKT-04 p=300_000 traded=F outcome=NO  -> (300_000)^2 = 9e10
        eval-fc-05 MKT-05 p=500_000 traded=T outcome=YES -> (500_000)^2 = 2.5e11
        eval-fc-06 MKT-06 p=500_000 traded=F outcome=NO  -> (500_000)^2 = 2.5e11
        eval-fc-07 MKT-07 p=800_000 traded=T outcome=YES -> (200_000)^2 = 4e10
        eval-fc-08 MKT-08 p=200_000 traded=F outcome=NO  -> (200_000)^2 = 4e10
        eval-fc-09 MKT-09 p=1_000_000 traded=T outcome=YES -> 0
        eval-fc-10 MKT-10 p=0 traded=F outcome=NO -> 0

    ALL: sum = 2*(1e10+9e10+2.5e11+4e10) = 7.8e11; n=10 -> mean = 78_000 ppm.
    TRADED = {01,03,05,07,09}: sum = 1e10+9e10+2.5e11+4e10+0 = 3.9e11; n=5 ->
        mean = 78_000 ppm (the SAME multiset of terms as SKIPPED below).
    SKIPPED = {02,04,06,08,10}: sum = 1e10+9e10+2.5e11+4e10+0 = 3.9e11; n=5 ->
        mean = 78_000 ppm.
    ABSTAINED: only records that are untraded, ineligible
        (`eligible_for_live=False`), carry a non-exclusion abstention reason.
        eval-fc-06 (stale_price, ineligible) and eval-fc-10 (market_closed,
        ineligible) qualify; eval-fc-02 is excluded (reason=low_liquidity is
        an exclusion reason) and eval-fc-04/eval-fc-08 are NOT ineligible
        (`eligible_for_live=True` in the fixture), so they fail the ABSTAINED
        predicate even though they carry an abstention reason.
        sum = 2.5e11 (fc-06) + 0 (fc-10) = 2.5e11; n=2 -> mean = 125_000 ppm.
    EXCLUDED_BY_LIQUIDITY = {eval-fc-02} (reason=low_liquidity): sum = 1e10;
        n=1 -> mean = 10_000 ppm.
    ABOVE_THRESHOLD: |p_ppm - baseline_pips*100| >= 30_000 ppm.
        fc-01: |900_000-880_000|=20_000 -> no.  fc-02: |100_000-150_000|=
        50_000 -> yes.  fc-03: |700_000-720_000|=20_000 -> no.
        fc-04: |300_000-320_000|=20_000 -> no.  fc-05: |500_000-550_000|=
        50_000 -> yes.  fc-06: |500_000-480_000|=20_000 -> no.
        fc-07: |800_000-790_000|=10_000 -> no.   fc-08: |200_000-220_000|=
        20_000 -> no.  fc-09: |1_000_000-990_000|=10_000 -> no.
        fc-10: |0-10_000|=10_000 -> no.
        So ABOVE_THRESHOLD = {fc-02, fc-05}: sum = 1e10 (fc-02) + 2.5e11
        (fc-05) = 2.6e11; n=2 -> mean = 130_000 ppm.
    EXCLUDED_BY_CATEGORY: no forecast in this fixture carries the
        `excluded_category` reason -> n=0, UNDEFINED.
    """
    from windbreak.evaluation.cohorts import UNDEFINED, Cohort, cohort_brier_table

    table = cohort_brier_table(_synthetic_inputs(), window=_WINDOW)

    assert len(table) == 7
    by_cohort = {row.cohort: row for row in table}
    assert set(by_cohort) == set(Cohort)

    assert by_cohort[Cohort.ALL].count == 10
    assert by_cohort[Cohort.ALL].brier_ppm == 78_000

    assert by_cohort[Cohort.TRADED].count == 5
    assert by_cohort[Cohort.TRADED].brier_ppm == 78_000

    assert by_cohort[Cohort.SKIPPED].count == 5
    assert by_cohort[Cohort.SKIPPED].brier_ppm == 78_000

    assert by_cohort[Cohort.ABSTAINED].count == 2
    assert by_cohort[Cohort.ABSTAINED].brier_ppm == 125_000

    assert by_cohort[Cohort.EXCLUDED_BY_LIQUIDITY].count == 1
    assert by_cohort[Cohort.EXCLUDED_BY_LIQUIDITY].brier_ppm == 10_000

    assert by_cohort[Cohort.ABOVE_THRESHOLD].count == 2
    assert by_cohort[Cohort.ABOVE_THRESHOLD].brier_ppm == 130_000

    assert by_cohort[Cohort.EXCLUDED_BY_CATEGORY].count == 0
    assert by_cohort[Cohort.EXCLUDED_BY_CATEGORY].brier_ppm is UNDEFINED

    for row in table:
        assert row.window is _WINDOW


# ---------------------------------------------------------------------------
# 2b. Window is genuinely load-bearing: ONE market with TWO resolved
#     forecasts at different created_sequence and different probabilities
#     yields DIFFERENT cohort Brier under FIRST_PER_MARKET vs
#     LATEST_BEFORE_CLOSE -- proving selection actually happens on the
#     cohort path, not a decorative label.
# ---------------------------------------------------------------------------

#: Market MULTI, outcome YES. Two traded snapshots on the SAME market:
#: an early low-probability miss (seq 1, p=200_000) and a late near-hit
#: (seq 5, p=900_000). Per-market window selection must pick exactly one.
_MULTI_EARLY = _forecast(
    forecast_id="multi-early",
    market_ticker="MULTI",
    probability_ppm=200_000,
    baseline_pips=5_000,
    traded=True,
    created_sequence=1,
)
_MULTI_LATE = _forecast(
    forecast_id="multi-late",
    market_ticker="MULTI",
    probability_ppm=900_000,
    baseline_pips=5_000,
    traded=True,
    created_sequence=5,
)
_MULTI_FORECAST_INPUTS = EvaluationInputs(
    forecasts=(_MULTI_EARLY, _MULTI_LATE),
    resolutions={"MULTI": ResolutionOutcome.YES},
)


def test_cohort_brier_table_window_selection_diverges_first_vs_latest() -> None:
    """One market, two resolved snapshots; the window genuinely selects.

    Market MULTI resolves YES (`outcome_ppm=1_000_000`); two traded snapshots:

        multi-early  seq=1  p=200_000 -> (200_000 - 1_000_000)^2 = 6.4e11
        multi-late   seq=5  p=900_000 -> (900_000 - 1_000_000)^2 = 1e10

    Both snapshots are traded, so both fall in the `ALL` and `TRADED` cohorts.
    Per-market window selection collapses the market to exactly one snapshot:

        FIRST_PER_MARKET   -> min created_sequence -> multi-early ->
            mean Brier over 1 record = 6.4e11 / (1 * 1_000_000) = 640_000 ppm.
        LATEST_BEFORE_CLOSE -> max created_sequence -> multi-late ->
            mean Brier over 1 record = 1e10 / (1 * 1_000_000) = 10_000 ppm.

    640_000 != 10_000, so the declared window is load-bearing: it changes which
    snapshot is scored, and thus the reported Brier.
    """
    from windbreak.evaluation.cohorts import Cohort, cohort_brier_table

    first = cohort_brier_table(
        _MULTI_FORECAST_INPUTS, window=ObservationWindow.FIRST_PER_MARKET
    )
    latest = cohort_brier_table(
        _MULTI_FORECAST_INPUTS, window=ObservationWindow.LATEST_BEFORE_CLOSE
    )

    first_all = {row.cohort: row for row in first}[Cohort.ALL]
    latest_all = {row.cohort: row for row in latest}[Cohort.ALL]

    assert first_all.count == 1
    assert latest_all.count == 1
    assert first_all.brier_ppm == 640_000
    assert latest_all.brier_ppm == 10_000
    assert first_all.brier_ppm != latest_all.brier_ppm

    first_traded = {row.cohort: row for row in first}[Cohort.TRADED]
    latest_traded = {row.cohort: row for row in latest}[Cohort.TRADED]
    assert first_traded.brier_ppm == 640_000
    assert latest_traded.brier_ppm == 10_000


# ---------------------------------------------------------------------------
# 3. Skipped-better-by-construction: delta is negative; the report render
#    over such inputs contains the SKIPPED-outperformed banner.
# ---------------------------------------------------------------------------

#: Two traded forecasts that both miss badly, two skipped forecasts that both
#: score well -- by construction, SKIPPED must show a lower (better) mean
#: Brier than TRADED.
_TRADED_MISS_1 = _forecast(
    forecast_id="traded-miss-1",
    market_ticker="T1",
    probability_ppm=900_000,
    baseline_pips=5_000,
    traded=True,
)
_TRADED_MISS_2 = _forecast(
    forecast_id="traded-miss-2",
    market_ticker="T2",
    probability_ppm=800_000,
    baseline_pips=5_000,
    traded=True,
)
_SKIPPED_HIT_1 = _forecast(
    forecast_id="skipped-hit-1",
    market_ticker="S1",
    probability_ppm=100_000,
    baseline_pips=5_000,
    traded=False,
)
_SKIPPED_HIT_2 = _forecast(
    forecast_id="skipped-hit-2",
    market_ticker="S2",
    probability_ppm=200_000,
    baseline_pips=5_000,
    traded=False,
)
_SKIPPED_BETTER_RESOLUTIONS = {
    "T1": ResolutionOutcome.NO,
    "T2": ResolutionOutcome.NO,
    "S1": ResolutionOutcome.NO,
    "S2": ResolutionOutcome.NO,
}
_SKIPPED_BETTER_INPUTS = EvaluationInputs(
    forecasts=(_TRADED_MISS_1, _TRADED_MISS_2, _SKIPPED_HIT_1, _SKIPPED_HIT_2),
    resolutions=_SKIPPED_BETTER_RESOLUTIONS,
)


def test_traded_vs_skipped_brier_delta_is_negative_when_skipped_outperforms() -> None:
    """Hand computation, all outcomes NO (`outcome_ppm=0`):

    TRADED: (900_000)^2 + (800_000)^2 = 8.1e11 + 6.4e11 = 1.45e12;
        n=2 -> mean = 725_000 ppm.
    SKIPPED: (100_000)^2 + (200_000)^2 = 1e10 + 4e10 = 5e10; n=2 ->
        mean = 25_000 ppm.
    delta = SKIPPED - TRADED = 25_000 - 725_000 = -700_000 (negative:
        SKIPPED outperformed TRADED).
    """
    from windbreak.evaluation.cohorts import traded_vs_skipped_brier_delta

    delta = traded_vs_skipped_brier_delta(_SKIPPED_BETTER_INPUTS, window=_WINDOW)

    assert delta == -700_000
    assert delta < 0


def test_report_render_text_contains_skipped_outperformed_banner() -> None:
    """A report built with the skipped-better-by-construction cohort table
    renders the literal `SKIPPED_OUTPERFORMED_BANNER` text.
    """
    from windbreak.evaluation.cohorts import cohort_brier_table
    from windbreak.evaluation.report import (
        SKIPPED_OUTPERFORMED_BANNER,
        EvaluationReport,
        TrackReport,
    )

    assert (
        SKIPPED_OUTPERFORMED_BANNER == "SKIPPED FORECASTS OUTPERFORMED TRADED FORECASTS"
    )

    table = cohort_brier_table(_SKIPPED_BETTER_INPUTS, window=_WINDOW)
    report = EvaluationReport(
        tracks=(
            TrackReport(name=Track.FORECAST.value, metrics=()),
            TrackReport(name=Track.SELECTION.value, metrics=()),
            TrackReport(name=Track.EXECUTION.value, metrics=()),
        ),
        cohorts=table,
    )

    text = report.render_text()

    assert SKIPPED_OUTPERFORMED_BANNER in text


# ---------------------------------------------------------------------------
# 4. traded_vs_skipped_brier_delta raises ValueError when either cohort has
#    no resolved records.
# ---------------------------------------------------------------------------


def test_traded_vs_skipped_brier_delta_raises_when_traded_cohort_is_empty() -> None:
    """Only SKIPPED records present -> TRADED has zero resolved records ->
    `ValueError`.
    """
    from windbreak.evaluation.cohorts import traded_vs_skipped_brier_delta

    only_skipped = EvaluationInputs(
        forecasts=(_SKIPPED_HIT_1, _SKIPPED_HIT_2),
        resolutions={"S1": ResolutionOutcome.NO, "S2": ResolutionOutcome.NO},
    )

    with pytest.raises(ValueError, match=r"(?i)resolved"):
        traded_vs_skipped_brier_delta(only_skipped, window=_WINDOW)


def test_traded_vs_skipped_brier_delta_raises_when_skipped_cohort_is_empty() -> None:
    """Only TRADED records present -> SKIPPED has zero resolved records ->
    `ValueError`.
    """
    from windbreak.evaluation.cohorts import traded_vs_skipped_brier_delta

    only_traded = EvaluationInputs(
        forecasts=(_TRADED_MISS_1, _TRADED_MISS_2),
        resolutions={"T1": ResolutionOutcome.NO, "T2": ResolutionOutcome.NO},
    )

    with pytest.raises(ValueError, match=r"(?i)resolved"):
        traded_vs_skipped_brier_delta(only_traded, window=_WINDOW)


# ---------------------------------------------------------------------------
# 5. Render: every cohort line labels [latest_before_close]; over the
#    synthetic fixture the delta renders 0 (both TRADED and SKIPPED are
#    78_000 ppm exactly, per the hand computation in test 2 above).
# ---------------------------------------------------------------------------


def test_run_evaluation_cohort_lines_are_labeled_latest_before_close() -> None:
    """`run_evaluation` over the synthetic fixture renders one line per
    `Cohort` (all seven), each labeled `[latest_before_close]`, and the
    `traded_vs_skipped_brier_delta` metric line renders exactly `0` (TRADED
    and SKIPPED both average 78_000 ppm on this fixture -- see test 2).
    """
    from windbreak.evaluation.cohorts import Cohort

    report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)
    text = report.render_text()

    for cohort in Cohort:
        assert f"cohort {cohort.value} [latest_before_close]" in text

    assert "traded_vs_skipped_brier_delta [latest_before_close] = 0" in text


# ---------------------------------------------------------------------------
# 6. run_evaluation degrades gracefully when the window has zero TRADED or
#    zero SKIPPED forecasts: the traded_vs_skipped_brier_delta metric renders
#    the UNDEFINED sentinel instead of crashing the entire three-track report.
#    (Regression for the unhandled ValueError; see PR #178 review.)
# ---------------------------------------------------------------------------


def _fixture_with_all_traded(tmp_path: Path, *, traded: bool) -> Path:
    """Write a copy of the synthetic fixture with every forecast's `traded` set.

    Forcing every forecast to the same `traded` value collapses one of the two
    mutually-exclusive `TRADED` / `SKIPPED` cohorts to zero resolved records --
    the exact input shape that made `traded_vs_skipped_brier_delta` raise before
    the graceful-degradation fix. Only the `traded` flag is rewritten; every
    resolution, snapshot, and temporal coordinate is left untouched so the
    fixture still admits all ten forecasts through the temporal gate.

    Args:
        tmp_path: The pytest-provided temporary directory to write into.
        traded: The `traded` value to stamp onto every forecast (`True` empties
            `SKIPPED`, `False` empties `TRADED`).

    Returns:
        The path to the written single-cohort fixture.
    """
    payload: dict[str, Any] = json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))
    for forecast in payload["forecasts"]:
        forecast["traded"] = traded
    destination = tmp_path / f"single_cohort_traded_{traded}.json"
    destination.write_text(json.dumps(payload), encoding="utf-8")
    return destination


def test_run_evaluation_renders_undefined_when_skipped_cohort_is_empty(
    tmp_path: Path,
) -> None:
    """Every forecast traded -> the `SKIPPED` cohort is empty in the window.

    `run_evaluation` must still assemble the full three-track report and render
    the `traded_vs_skipped_brier_delta` line as the `UNDEFINED` sentinel rather
    than propagating a `ValueError` out of report generation.
    """
    fixture_path = _fixture_with_all_traded(tmp_path, traded=True)

    report = run_evaluation(fixture_path=fixture_path)
    text = report.render_text()

    assert "traded_vs_skipped_brier_delta [latest_before_close] = UNDEFINED" in text


def test_run_evaluation_renders_undefined_when_traded_cohort_is_empty(
    tmp_path: Path,
) -> None:
    """Every forecast skipped -> the `TRADED` cohort is empty in the window.

    `run_evaluation` must still assemble the full three-track report and render
    the `traded_vs_skipped_brier_delta` line as the `UNDEFINED` sentinel rather
    than propagating a `ValueError` out of report generation.
    """
    fixture_path = _fixture_with_all_traded(tmp_path, traded=False)

    report = run_evaluation(fixture_path=fixture_path)
    text = report.render_text()

    assert "traded_vs_skipped_brier_delta [latest_before_close] = UNDEFINED" in text


def test_registry_adapter_propagates_non_empty_cohort_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graceful-degradation catch is scoped to `EmptyCohortError` alone.

    A `ValueError` that is *not* an `EmptyCohortError` (i.e. a genuinely invalid
    input, not the ordinary empty-cohort state) must propagate out of the
    registry adapter rather than being silently converted to the `UNDEFINED`
    sentinel -- otherwise a real bug would masquerade as an undefined metric.
    """
    from windbreak.evaluation import registry

    def _raise_generic(inputs: object, *, window: object) -> int:
        raise ValueError("not an empty-cohort error")

    monkeypatch.setattr(
        registry.cohorts, "traded_vs_skipped_brier_delta", _raise_generic
    )

    with pytest.raises(ValueError, match="not an empty-cohort error"):
        registry._compute_traded_vs_skipped_brier_delta(
            EvaluationInputs(forecasts=(), resolutions={})
        )


# ---------------------------------------------------------------------------
# 7. live_brier_degradation: a multi-forecast-per-market LIVE input scores the
#    re-forecast market ONCE (its LATEST_BEFORE_CLOSE record), proving the
#    per-market collapse independent of the SQL crosscheck (Gate 4 round-2
#    review fix, Fix 1). Mirrors `tests/evaluation/test_live_dual_path.py`'s
#    `test_live_brier_degradation_collapses_re_forecast_market_before_scoring`,
#    but exercises `cohorts.live_brier_degradation` directly (no SQL, no
#    registry adapter) so a regression here is unambiguously the Python
#    reference's own collapse, not a crosscheck artifact.
# ---------------------------------------------------------------------------


def test_live_brier_degradation_scores_a_re_forecast_market_once() -> None:
    """A LIVE market forecast TWICE contributes ONE observation -- its
    `LATEST_BEFORE_CLOSE` record -- to `live_brier_degradation`, not one
    observation per raw forecast.

    Hand computation, per-market collapse applied (the CORRECT answer):

    LIVE cohort, 2 markets after collapse:
    - `RF` is forecast TWICE: seq=1 p=200_000 and seq=2 p=900_000 (the
      LATEST). Only the seq=2 record survives `LATEST_BEFORE_CLOSE`.
      Outcome YES (1_000_000): term = (900_000-1_000_000)^2 =
      10_000_000_000.
    - `L2` (seq=3, p=500_000), outcome NO (0): term = (500_000-0)^2 =
      250_000_000_000.
    Collapsed LIVE sum = 260_000_000_000, n=2; mean =
    `ceil(260_000_000_000 / (2 * 1_000_000)) = 130_000` ppm exactly.

    PAPER cohort, 1 market: `P1` (p=700_000), outcome YES: term =
    (700_000-1_000_000)^2 = 90_000_000_000; n=1; mean =
    `ceil(90_000_000_000 / 1_000_000) = 90_000` ppm exactly.

    `live_brier_degradation` (collapsed, correct) = 130_000 - 90_000 =
    `40_000` ppm exactly.

    Today (RED): `live_brier_degradation` feeds `_resolved_track_forecasts`'s
    raw, uncollapsed per-track forecasts straight into `_rolling_window` and
    `mean_brier` with no `windows.resolve_window` collapse, so the seq=1
    `RF` record is counted as a THIRD, separate LIVE observation instead of
    being superseded by the seq=2 record: sum = 640_000_000_000 (seq=1,
    uncollapsed term) + 10_000_000_000 (seq=2) + 250_000_000_000 (`L2`) =
    900_000_000_000; n=3; mean =
    `ceil(900_000_000_000 / (3 * 1_000_000)) = 300_000`; degradation =
    300_000 - 90_000 = 210_000 != the pinned `40_000` below.
    """
    from windbreak.evaluation.cohorts import live_brier_degradation

    live_forecasts = (
        _forecast(
            forecast_id="rf-seq1",
            market_ticker="RF",
            probability_ppm=200_000,
            baseline_pips=2_000,
            created_sequence=1,
            live=True,
        ),
        _forecast(
            forecast_id="rf-seq2",
            market_ticker="RF",
            probability_ppm=900_000,
            baseline_pips=9_000,
            created_sequence=2,
            live=True,
        ),
        _forecast(
            forecast_id="l2-seq3",
            market_ticker="L2",
            probability_ppm=500_000,
            baseline_pips=5_000,
            created_sequence=3,
            live=True,
        ),
    )
    paper_forecasts = (
        _forecast(
            forecast_id="p1-seq4",
            market_ticker="P1",
            probability_ppm=700_000,
            baseline_pips=7_000,
            created_sequence=4,
            live=False,
        ),
    )
    inputs = EvaluationInputs(
        forecasts=live_forecasts + paper_forecasts,
        resolutions={
            "RF": ResolutionOutcome.YES,
            "L2": ResolutionOutcome.NO,
            "P1": ResolutionOutcome.YES,
        },
    )

    degradation = live_brier_degradation(inputs, window=_WINDOW, window_size=100)

    assert degradation == 40_000
