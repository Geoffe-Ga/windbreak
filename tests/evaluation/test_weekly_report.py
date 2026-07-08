"""Failing-first tests for `render_weekly_report` / `generate_weekly_report`
(issue #55, RED).

Neither symbol exists yet on `hedgekit.evaluation.report`, so every test below
imports them as the FIRST statement inside the test body (matching this
package's established RED convention; see `test_preregistration.py` /
`test_cohorts.py`) so each test collects and fails independently on its own
`ImportError: cannot import name 'render_weekly_report' from
'hedgekit.evaluation.report'` (or `'generate_weekly_report'`).

Pins issue #55's weekly-report extension of the #48 stub
(`hedgekit.reports.weekly`):

- `render_weekly_report(*, today, evaluation, costs) -> str` is pure markdown:
  it always preserves the stub's three original headings (`## Equity vs
  floor`, `## Positions`, `## Decisions`, each still `No data yet.` -- this
  issue does not wire that data), and adds two new sections: `## Evaluation`
  (the verbatim output of `evaluation.render_text()` when `evaluation` is not
  `None`, else `No data yet.`) and `## Cost meter` (the `str()` rendering of
  each of `CostMeter`'s three `MoneyMicros` fields when `costs` is not `None`,
  else `No data yet.`).
- `generate_weekly_report(output_dir, *, today, evaluation, costs) -> Path`
  delegates naming and ISO-week idempotence to
  `hedgekit.reports.weekly.maybe_write_weekly`, passing the rendered body
  through; calling it twice within the same ISO week returns the same
  already-written file untouched.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from hedgekit.evaluation.registry import Track
from hedgekit.evaluation.report import EvaluationReport, TrackReport
from hedgekit.numeric.types import MoneyMicros

if TYPE_CHECKING:
    from pathlib import Path


def _empty_evaluation_report() -> EvaluationReport:
    """Build a minimal, valid `EvaluationReport` with no metrics in any track.

    Returns:
        An `EvaluationReport` whose `render_text()` renders only the three
        bare `== <track> ==` section headers, with no metric lines and no
        `No data yet.` substring anywhere -- so this fixture cannot be
        confused with either side's own "no data" fallback text.
    """
    return EvaluationReport(
        tracks=(
            TrackReport(name=Track.FORECAST.value, metrics=()),
            TrackReport(name=Track.SELECTION.value, metrics=()),
            TrackReport(name=Track.EXECUTION.value, metrics=()),
        )
    )


def _sample_cost_meter() -> object:
    """Build a `CostMeter` with three distinct, non-zero `MoneyMicros` fields.

    Returns:
        A `CostMeter` whose three money fields each render to a distinct,
        greppable `str()` value.
    """
    from hedgekit.evaluation.costs import CostMeter

    return CostMeter(
        total_research_cost_micros=7_000_000,
        resolved_forecast_count=2,
        profitable_trade_count=1,
        trade_count=2,
        cost_per_resolved_forecast_micros=MoneyMicros(3_500_000),
        cost_per_profitable_trade_micros=MoneyMicros(7_000_000),
        cost_adjusted_expectancy_micros=MoneyMicros(-1_500_000),
    )


# ---------------------------------------------------------------------------
# 1. render_weekly_report: both sections populated.
# ---------------------------------------------------------------------------


def test_render_weekly_report_includes_stub_headings_and_both_new_sections() -> None:
    """Populated `evaluation`/`costs` render both new sections' real content.

    The three original stub headings (`Equity vs floor`, `Positions`,
    `Decisions`) still render `No data yet.` -- this issue does not wire that
    data -- so exactly 3 `No data yet.` occurrences remain (none contributed
    by the new sections, since both fixtures carry real content).
    """
    from hedgekit.evaluation.report import render_weekly_report

    evaluation = _empty_evaluation_report()
    costs = _sample_cost_meter()
    today = date(2024, 3, 4)

    body = render_weekly_report(today=today, evaluation=evaluation, costs=costs)

    assert "# Weekly report 2024-03-04" in body
    assert "## Equity vs floor" in body
    assert "## Positions" in body
    assert "## Decisions" in body
    assert "## Evaluation" in body
    assert "## Cost meter" in body

    assert evaluation.render_text() in body
    assert str(MoneyMicros(3_500_000)) in body
    assert str(MoneyMicros(7_000_000)) in body
    assert str(MoneyMicros(-1_500_000)) in body

    assert body.count("No data yet.") == 3


# ---------------------------------------------------------------------------
# 2. render_weekly_report: both None -> fallback text under both sections.
# ---------------------------------------------------------------------------


def test_render_weekly_report_shows_no_data_yet_for_both_none_arguments() -> None:
    """`evaluation=None, costs=None` fall back to `No data yet.` in both
    new sections, on top of the three the stub always carries -- five total.
    """
    from hedgekit.evaluation.report import render_weekly_report

    today = date(2024, 3, 4)

    body = render_weekly_report(today=today, evaluation=None, costs=None)

    assert "## Evaluation" in body
    assert "## Cost meter" in body
    assert body.count("No data yet.") == 5


def test_render_weekly_report_evaluation_and_costs_fall_back_independently() -> None:
    """`evaluation=None` with `costs` populated renders the cost section's
    real content alongside the evaluation section's fallback (and vice
    versa) -- the two sections are independent, not all-or-nothing.
    """
    from hedgekit.evaluation.report import render_weekly_report

    today = date(2024, 3, 4)
    costs = _sample_cost_meter()

    body_costs_only = render_weekly_report(today=today, evaluation=None, costs=costs)
    assert str(MoneyMicros(3_500_000)) in body_costs_only
    # 3 stub sections + the evaluation section's own fallback = 4.
    assert body_costs_only.count("No data yet.") == 4

    evaluation = _empty_evaluation_report()
    body_evaluation_only = render_weekly_report(
        today=today, evaluation=evaluation, costs=None
    )
    assert evaluation.render_text() in body_evaluation_only
    assert body_evaluation_only.count("No data yet.") == 4


# ---------------------------------------------------------------------------
# 3. generate_weekly_report: writes the dated file; ISO-week idempotence.
# ---------------------------------------------------------------------------


def test_generate_weekly_report_writes_dated_file_with_the_rendered_body(
    tmp_path: Path,
) -> None:
    """`generate_weekly_report` writes `weekly-YYYY-MM-DD.md` with the body
    `render_weekly_report` would have produced for the same arguments (this
    direct invocation is exactly what a scheduler hook would call).
    """
    from hedgekit.evaluation.report import (
        generate_weekly_report,
        render_weekly_report,
    )

    today = date(2024, 3, 4)
    evaluation = _empty_evaluation_report()
    costs = _sample_cost_meter()

    path = generate_weekly_report(
        tmp_path, today=today, evaluation=evaluation, costs=costs
    )

    assert path == tmp_path / "weekly-2024-03-04.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == render_weekly_report(
        today=today, evaluation=evaluation, costs=costs
    )


def test_generate_weekly_report_is_idempotent_within_the_same_iso_week(
    tmp_path: Path,
) -> None:
    """A second call within the same ISO calendar week returns the first
    file untouched -- routed through `hedgekit.reports.weekly.maybe_write_weekly`
    -- even though the second call's `today` and arguments differ.

    2024-03-04 is a Monday and 2024-03-05 a Tuesday of the same ISO week.
    """
    from hedgekit.evaluation.report import generate_weekly_report

    first = generate_weekly_report(
        tmp_path, today=date(2024, 3, 4), evaluation=None, costs=None
    )
    first_body = first.read_text(encoding="utf-8")

    second = generate_weekly_report(
        tmp_path,
        today=date(2024, 3, 5),
        evaluation=_empty_evaluation_report(),
        costs=_sample_cost_meter(),
    )

    assert second == first
    assert second.read_text(encoding="utf-8") == first_body


def test_generate_weekly_report_writes_a_new_file_the_following_iso_week(
    tmp_path: Path,
) -> None:
    """A call in a later ISO week writes a second, distinctly-named file."""
    from hedgekit.evaluation.report import generate_weekly_report

    first = generate_weekly_report(
        tmp_path, today=date(2024, 3, 4), evaluation=None, costs=None
    )
    second = generate_weekly_report(
        tmp_path, today=date(2024, 3, 11), evaluation=None, costs=None
    )

    assert first != second
    assert first.exists()
    assert second.exists()
