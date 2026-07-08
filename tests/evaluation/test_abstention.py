"""Failing-first tests for `windbreak.evaluation.abstention` (issue #53, RED).

`windbreak.evaluation.abstention` does not exist yet, so every test below
imports its new symbols from that module as the FIRST statement inside the
test body (matching this package's established RED convention in
`test_temporal_integrity.py`) so each test collects independently and fails on
its own `ModuleNotFoundError: No module named 'windbreak.evaluation.abstention'`.

Pins SPEC-EPIC_07 issue #53's counterfactual abstention-wisdom scoring:

- For each admitted, RESOLVED, non-traded record with `abstention_reason` set
  (traded and unresolved records never enter this scoring):
    * implied direction is `LONG_YES` when `probability_ppm > baseline_ppm`,
      `LONG_NO` when `<`, and no implied trade (counterfactual PnL exactly 0,
      verdict WISE) when equal.
    * `LONG_YES` PnL, in pips: `(PAYOUT_PIPS if outcome YES else 0) -
      baseline_pips`.
    * `LONG_NO` PnL, in pips: `(PAYOUT_PIPS if outcome NO else 0) -
      (PAYOUT_PIPS - baseline_pips)`.
    * Verdict is `WISE` iff PnL `<= 0`, `UNWISE` iff PnL `> 0`.
- `score_abstentions(inputs) -> tuple[AbstentionScore, ...]`.
- `summarize_abstentions(scores_or_inputs) -> AbstentionSummary`, whose
  `forgone_pnl_pips` is the sum of strictly POSITIVE counterfactual PnLs only.

`PAYOUT_PIPS` (10_000) and `BASELINE_PPM_PER_PIP` (100) are re-derived below
directly from `windbreak.evaluation.metrics`, per this issue's own instruction
not to assume their values.
"""

from __future__ import annotations

from pathlib import Path

from windbreak.evaluation import EvaluationInputs, FixtureForecast, ResolutionOutcome
from windbreak.evaluation.metrics import BASELINE_PPM_PER_PIP, PAYOUT_PIPS
from windbreak.numeric.types import ProbabilityPpm

#: The epic-wide known-answer fixture shared by issues #49-#55.
SYNTHETIC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_known_answer.json"
)


def test_metrics_module_payout_and_ppm_per_pip_constants_are_as_expected() -> None:
    """Re-derive `windbreak.evaluation.metrics`'s own constants rather than
    assume them: `PAYOUT_PIPS` is the full binary payout (10_000 == $1.00 at
    1e-4 pips) and `BASELINE_PPM_PER_PIP` converts a pip-scaled price into ppm
    probability (`baseline_ppm = baseline_pips * BASELINE_PPM_PER_PIP`). Every
    hand computation in this file is pinned against these exact values.
    """
    assert PAYOUT_PIPS == 10_000
    assert BASELINE_PPM_PER_PIP == 100


def _forecast(
    *,
    forecast_id: str,
    market_ticker: str,
    probability_ppm: int,
    baseline_pips: int,
    traded: bool = False,
    eligible: bool = False,
    abstention: str | None = "some_reason",
) -> FixtureForecast:
    """Build one `FixtureForecast` with the fields these abstention tests vary.

    Args:
        forecast_id: Stable identifier of the forecast record.
        market_ticker: Ticker of the market this forecast is about.
        probability_ppm: Forecast probability, in ppm.
        baseline_pips: The reference executable (ask) price, in pips.
        traded: Whether a live trade was actually taken.
        eligible: Whether the forecast passed live-eligibility gates.
        abstention: The abstention reason, or `None`.

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
    )


# ---------------------------------------------------------------------------
# 1. The four directional wise/unwise scenarios, hand-derived against the
#    re-derived PAYOUT_PIPS=10_000 constant.
# ---------------------------------------------------------------------------


def test_wise_long_yes_scores_negative_pnl_and_wise_verdict() -> None:
    """p=800_000 > baseline_ppm=300_000 (ask 3_000 pips) -> implied LONG_YES.
    Outcome NO -> PnL = (0) - 3_000 = -3_000 (<=0) -> WISE.
    """
    from windbreak.evaluation.abstention import AbstentionVerdict, score_abstentions

    forecast = _forecast(
        forecast_id="wise-long-yes",
        market_ticker="MKT-WLY",
        probability_ppm=800_000,
        baseline_pips=3_000,
    )
    inputs = EvaluationInputs(
        forecasts=(forecast,), resolutions={"MKT-WLY": ResolutionOutcome.NO}
    )

    scores = score_abstentions(inputs)

    assert len(scores) == 1
    assert scores[0].counterfactual_pnl_pips == -3_000
    assert scores[0].verdict is AbstentionVerdict.WISE


def test_unwise_long_yes_scores_positive_pnl_and_unwise_verdict() -> None:
    """p=800_000 > baseline_ppm=300_000 (ask 3_000 pips) -> implied LONG_YES.
    Outcome YES -> PnL = (10_000) - 3_000 = 7_000 (>0) -> UNWISE.
    """
    from windbreak.evaluation.abstention import AbstentionVerdict, score_abstentions

    forecast = _forecast(
        forecast_id="unwise-long-yes",
        market_ticker="MKT-ULY",
        probability_ppm=800_000,
        baseline_pips=3_000,
    )
    inputs = EvaluationInputs(
        forecasts=(forecast,), resolutions={"MKT-ULY": ResolutionOutcome.YES}
    )

    scores = score_abstentions(inputs)

    assert len(scores) == 1
    assert scores[0].counterfactual_pnl_pips == 7_000
    assert scores[0].verdict is AbstentionVerdict.UNWISE


def test_wise_long_no_scores_negative_pnl_and_wise_verdict() -> None:
    """p=100_000 < baseline_ppm=900_000 (ask 9_000 pips) -> implied LONG_NO.
    Outcome YES -> PnL = (0) - (10_000-9_000) = -1_000 (<=0) -> WISE.
    """
    from windbreak.evaluation.abstention import AbstentionVerdict, score_abstentions

    forecast = _forecast(
        forecast_id="wise-long-no",
        market_ticker="MKT-WLN",
        probability_ppm=100_000,
        baseline_pips=9_000,
    )
    inputs = EvaluationInputs(
        forecasts=(forecast,), resolutions={"MKT-WLN": ResolutionOutcome.YES}
    )

    scores = score_abstentions(inputs)

    assert len(scores) == 1
    assert scores[0].counterfactual_pnl_pips == -1_000
    assert scores[0].verdict is AbstentionVerdict.WISE


def test_unwise_long_no_scores_positive_pnl_and_unwise_verdict() -> None:
    """p=100_000 < baseline_ppm=900_000 (ask 9_000 pips) -> implied LONG_NO.
    Outcome NO -> PnL = (10_000) - (10_000-9_000) = 9_000 (>0) -> UNWISE.
    """
    from windbreak.evaluation.abstention import AbstentionVerdict, score_abstentions

    forecast = _forecast(
        forecast_id="unwise-long-no",
        market_ticker="MKT-ULN",
        probability_ppm=100_000,
        baseline_pips=9_000,
    )
    inputs = EvaluationInputs(
        forecasts=(forecast,), resolutions={"MKT-ULN": ResolutionOutcome.NO}
    )

    scores = score_abstentions(inputs)

    assert len(scores) == 1
    assert scores[0].counterfactual_pnl_pips == 9_000
    assert scores[0].verdict is AbstentionVerdict.UNWISE


def test_zero_edge_forecast_has_no_implied_trade_pnl_zero_wise() -> None:
    """p_ppm == baseline_ppm (300_000 == 3_000*100) -> no implied direction,
    counterfactual PnL is exactly 0, verdict WISE (0 <= 0).
    """
    from windbreak.evaluation.abstention import AbstentionVerdict, score_abstentions

    forecast = _forecast(
        forecast_id="zero-edge",
        market_ticker="MKT-ZERO",
        probability_ppm=300_000,
        baseline_pips=3_000,
    )
    inputs = EvaluationInputs(
        forecasts=(forecast,), resolutions={"MKT-ZERO": ResolutionOutcome.YES}
    )

    scores = score_abstentions(inputs)

    assert len(scores) == 1
    assert scores[0].counterfactual_pnl_pips == 0
    assert scores[0].verdict is AbstentionVerdict.WISE


# ---------------------------------------------------------------------------
# 2. Traded and unresolved records never enter score_abstentions.
# ---------------------------------------------------------------------------


def test_traded_and_unresolved_records_are_excluded_from_scoring() -> None:
    """A traded record (even with an abstention_reason-like field unset) and
    an unresolved record (its ticker absent from `resolutions`) never appear
    in `score_abstentions`'s output, even alongside a legitimately scoreable
    abstained record.
    """
    from windbreak.evaluation.abstention import score_abstentions

    traded_record = _forecast(
        forecast_id="traded-should-be-excluded",
        market_ticker="MKT-TRADED",
        probability_ppm=500_000,
        baseline_pips=5_000,
        traded=True,
        eligible=True,
        abstention=None,
    )
    unresolved_record = _forecast(
        forecast_id="unresolved-should-be-excluded",
        market_ticker="MKT-UNRESOLVED",
        probability_ppm=500_000,
        baseline_pips=5_000,
        traded=False,
        eligible=False,
        abstention="some_reason",
    )
    scoreable_record = _forecast(
        forecast_id="scoreable",
        market_ticker="MKT-SCOREABLE",
        probability_ppm=800_000,
        baseline_pips=3_000,
        traded=False,
        eligible=False,
        abstention="some_reason",
    )
    inputs = EvaluationInputs(
        forecasts=(traded_record, unresolved_record, scoreable_record),
        resolutions={
            "MKT-TRADED": ResolutionOutcome.YES,
            "MKT-SCOREABLE": ResolutionOutcome.NO,
            # MKT-UNRESOLVED deliberately absent.
        },
    )

    scores = score_abstentions(inputs)

    ids = {score.forecast_id for score in scores}
    assert ids == {"scoreable"}


def test_traded_record_with_abstention_reason_field_still_set_is_excluded() -> None:
    """A record that is TRADED still carries no scoring row even if
    (pathologically) it also carries a non-`None` `abstention_reason` --
    `traded` alone is sufficient to exclude a record from abstention scoring.
    """
    from windbreak.evaluation.abstention import score_abstentions

    pathological = _forecast(
        forecast_id="traded-with-reason",
        market_ticker="MKT-PATH",
        probability_ppm=500_000,
        baseline_pips=5_000,
        traded=True,
        eligible=True,
        abstention="stale_price",
    )
    inputs = EvaluationInputs(
        forecasts=(pathological,), resolutions={"MKT-PATH": ResolutionOutcome.YES}
    )

    scores = score_abstentions(inputs)

    assert scores == ()


def test_untraded_record_with_no_abstention_reason_is_excluded() -> None:
    """A record that is untraded but carries `abstention_reason=None` (no
    reason recorded) is excluded -- there is nothing to counterfactually
    score without a recorded reason.
    """
    from windbreak.evaluation.abstention import score_abstentions

    no_reason = _forecast(
        forecast_id="no-reason",
        market_ticker="MKT-NOREASON",
        probability_ppm=500_000,
        baseline_pips=5_000,
        traded=False,
        eligible=True,
        abstention=None,
    )
    inputs = EvaluationInputs(
        forecasts=(no_reason,), resolutions={"MKT-NOREASON": ResolutionOutcome.YES}
    )

    scores = score_abstentions(inputs)

    assert scores == ()


# ---------------------------------------------------------------------------
# 3. AbstentionScore field shape.
# ---------------------------------------------------------------------------


def test_abstention_score_carries_the_documented_fields() -> None:
    """`AbstentionScore` exposes `forecast_id`, `market_ticker`,
    `abstention_reason`, `counterfactual_pnl_pips`, and `verdict`.
    """
    from windbreak.evaluation.abstention import score_abstentions

    forecast = _forecast(
        forecast_id="fields-fc",
        market_ticker="MKT-FIELDS",
        probability_ppm=800_000,
        baseline_pips=3_000,
        abstention="stale_price",
    )
    inputs = EvaluationInputs(
        forecasts=(forecast,), resolutions={"MKT-FIELDS": ResolutionOutcome.NO}
    )

    (score,) = score_abstentions(inputs)

    assert score.forecast_id == "fields-fc"
    assert score.market_ticker == "MKT-FIELDS"
    assert score.abstention_reason == "stale_price"
    assert score.counterfactual_pnl_pips == -3_000
    assert score.verdict is not None


# ---------------------------------------------------------------------------
# 4. summarize_abstentions: counts and forgone_pnl_pips (sum of POSITIVE
#    counterfactual PnLs only).
# ---------------------------------------------------------------------------

#: Five abstained-and-resolved records: three UNWISE (positive PnL) and two
#: WISE (one negative, one exactly zero), reusing the four directional
#: scenarios above plus the zero-edge case.
_SUMMARY_FORECASTS = (
    _forecast(
        forecast_id="sum-wise-long-yes",
        market_ticker="SUM-WLY",
        probability_ppm=800_000,
        baseline_pips=3_000,
    ),
    _forecast(
        forecast_id="sum-unwise-long-yes",
        market_ticker="SUM-ULY",
        probability_ppm=800_000,
        baseline_pips=3_000,
    ),
    _forecast(
        forecast_id="sum-wise-long-no",
        market_ticker="SUM-WLN",
        probability_ppm=100_000,
        baseline_pips=9_000,
    ),
    _forecast(
        forecast_id="sum-unwise-long-no",
        market_ticker="SUM-ULN",
        probability_ppm=100_000,
        baseline_pips=9_000,
    ),
    _forecast(
        forecast_id="sum-zero-edge",
        market_ticker="SUM-ZERO",
        probability_ppm=300_000,
        baseline_pips=3_000,
    ),
)
_SUMMARY_RESOLUTIONS = {
    "SUM-WLY": ResolutionOutcome.NO,  # wise long-yes: PnL -3_000
    "SUM-ULY": ResolutionOutcome.YES,  # unwise long-yes: PnL +7_000
    "SUM-WLN": ResolutionOutcome.YES,  # wise long-no: PnL -1_000
    "SUM-ULN": ResolutionOutcome.NO,  # unwise long-no: PnL +9_000
    "SUM-ZERO": ResolutionOutcome.YES,  # zero-edge: PnL 0
}
_SUMMARY_INPUTS = EvaluationInputs(
    forecasts=_SUMMARY_FORECASTS, resolutions=_SUMMARY_RESOLUTIONS
)


def test_summarize_abstentions_counts_and_forgone_pnl_match_hand_computation() -> None:
    """Hand computation over the five constructed records:

    wise:   sum-wise-long-yes (-3_000), sum-wise-long-no (-1_000),
            sum-zero-edge (0)         -> 3 WISE
    unwise: sum-unwise-long-yes (+7_000), sum-unwise-long-no (+9_000)
                                        -> 2 UNWISE
    total = 5.
    forgone_pnl_pips = sum of POSITIVE PnLs only = 7_000 + 9_000 = 16_000
    (the two negative PnLs and the zero PnL are NOT included).
    """
    from windbreak.evaluation.abstention import summarize_abstentions

    summary = summarize_abstentions(_SUMMARY_INPUTS)

    assert summary.total == 5
    assert summary.wise_count == 3
    assert summary.unwise_count == 2
    assert summary.forgone_pnl_pips == 16_000


def test_summarize_abstentions_accepts_precomputed_scores_too() -> None:
    """`summarize_abstentions` also accepts an already-computed
    `tuple[AbstentionScore, ...]` (not just raw `EvaluationInputs`), and
    produces an identical summary either way.
    """
    from windbreak.evaluation.abstention import score_abstentions, summarize_abstentions

    scores = score_abstentions(_SUMMARY_INPUTS)

    from_inputs = summarize_abstentions(_SUMMARY_INPUTS)
    from_scores = summarize_abstentions(scores)

    assert from_inputs == from_scores


def test_summarize_abstentions_on_synthetic_fixture_matches_hand_computation() -> None:
    """Over the shared synthetic known-answer fixture, the abstained-and-
    resolved records are eval-fc-02, eval-fc-04, eval-fc-06, eval-fc-08,
    eval-fc-10 (every untraded record carries a non-`None`
    `abstention_reason` in this fixture). Hand-derived direction and PnL for
    each (baseline_ppm = baseline_executable_price_pips * 100):

        eval-fc-02 MKT-02 p=100_000 baseline=1_500 (ppm 150_000) -> p<baseline
            -> LONG_NO. Outcome NO -> PnL = 10_000 - (10_000-1_500) = 1_500
            (UNWISE).
        eval-fc-04 MKT-04 p=300_000 baseline=3_200 (ppm 320_000) -> p<baseline
            -> LONG_NO. Outcome NO -> PnL = 10_000 - (10_000-3_200) = 3_200
            (UNWISE).
        eval-fc-06 MKT-06 p=500_000 baseline=4_800 (ppm 480_000) -> p>baseline
            -> LONG_YES. Outcome NO -> PnL = 0 - 4_800 = -4_800 (WISE).
        eval-fc-08 MKT-08 p=200_000 baseline=2_200 (ppm 220_000) -> p<baseline
            -> LONG_NO. Outcome NO -> PnL = 10_000 - (10_000-2_200) = 2_200
            (UNWISE).
        eval-fc-10 MKT-10 p=0 baseline=100 (ppm 10_000) -> p<baseline ->
            LONG_NO. Outcome NO -> PnL = 10_000 - (10_000-100) = 100
            (UNWISE).

    wise=1 (fc-06), unwise=4 (fc-02, fc-04, fc-08, fc-10), total=5.
    forgone_pnl_pips = 1_500 + 3_200 + 2_200 + 100 = 7_000.
    """
    import json

    from windbreak.evaluation.abstention import summarize_abstentions

    payload = json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))
    forecasts = tuple(
        FixtureForecast(
            forecast_id=entry["forecast_id"],
            market_ticker=entry["market_ticker"],
            probability_ppm=ProbabilityPpm(entry["probability_ppm"]),
            eligible_for_live=entry["eligible_for_live"],
            abstention_reason=entry["abstention_reason"],
            traded=entry["traded"],
            baseline_executable_price_pips=entry["baseline_executable_price_pips"],
        )
        for entry in payload["forecasts"]
    )
    resolutions = {
        entry["market_ticker"]: ResolutionOutcome(entry["outcome"])
        for entry in payload["resolutions"]
    }
    inputs = EvaluationInputs(forecasts=forecasts, resolutions=resolutions)

    summary = summarize_abstentions(inputs)

    assert summary.total == 5
    assert summary.wise_count == 1
    assert summary.unwise_count == 4
    assert summary.forgone_pnl_pips == 7_000


def test_run_evaluation_renders_abstentions_line_with_synthetic_fixture_values() -> (
    None
):
    """`run_evaluation`'s report renders the abstentions line, labeled
    `[latest_before_close]`, with the exact hand-derived summary from the
    synthetic fixture (see the test above): `wise=1 unwise=4
    forgone_pnl_pips=7_000`.
    """
    from windbreak.evaluation import run_evaluation

    report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)
    text = report.render_text()

    assert (
        "abstentions [latest_before_close] wise=1 unwise=4 forgone_pnl_pips=7000"
        in text
    )
