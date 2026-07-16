"""Tests for `windbreak.reports.providers` (issue #195, RED).

`windbreak/reports/providers.py` does not exist yet, so every import below
fails collection with `ModuleNotFoundError: No module named
'windbreak.reports.providers'` -- the expected Gate 1 RED state for issue
#195.

`render_provider_lines` is a pure renderer (mirrors
`windbreak.evaluation.report._render_cost_meter`'s own pure, no-I/O shape)
producing the issue's own worked-example line, verbatim:

    provider=futuresearch resolved=212 brier_skill_ppm=+14200
    cost_per_forecast=n/a abstain_rate=9% canary=OK

Per-provider `cost_per_forecast` is PERMANENTLY `"n/a"` (issue #281, not this
issue: per-provider cost is not derivable from the ledger). The two FLEET
cost figures (`cost_per_forecast_micros`/`cost_per_resolved_micros`) ARE
derivable in aggregate and render as their own lines.
"""

from __future__ import annotations


def _row(
    *,
    provider: str = "futuresearch",
    resolved: int = 212,
    brier_skill_ppm: int = 14_200,
    abstain_rate_ppm: int = 90_000,
    canary_status: str = "OK",
) -> object:
    """Build one `ProviderReportRow`, deferring the import to call time."""
    from windbreak.reports.providers import ProviderReportRow

    return ProviderReportRow(
        provider=provider,
        resolved=resolved,
        brier_skill_ppm=brier_skill_ppm,
        abstain_rate_ppm=abstain_rate_ppm,
        canary_status=canary_status,
    )


def _fleet(
    *,
    cost_per_forecast_micros: int | None = None,
    cost_per_resolved_micros: int | None = None,
) -> object:
    """Build one `FleetCostSummary`, deferring the import to call time."""
    from windbreak.reports.providers import FleetCostSummary

    return FleetCostSummary(
        cost_per_forecast_micros=cost_per_forecast_micros,
        cost_per_resolved_micros=cost_per_resolved_micros,
    )


def test_render_provider_lines_matches_the_issues_worked_example_verbatim() -> None:
    """The issue's own worked-example line renders byte-for-byte."""
    from windbreak.reports.providers import render_provider_lines

    text = render_provider_lines((_row(),), fleet=_fleet())

    assert (
        "provider=futuresearch resolved=212 brier_skill_ppm=+14200 "
        "cost_per_forecast=n/a abstain_rate=9% canary=OK"
    ) in text


def test_render_provider_lines_negative_skill_renders_the_minus_sign_verbatim() -> None:
    """A negative Brier skill renders its exact `-` sign, never suppressed,
    rounded to zero, or rendered as `n/a`.
    """
    from windbreak.reports.providers import render_provider_lines

    text = render_provider_lines(
        (_row(provider="anthropic", brier_skill_ppm=-2_100),), fleet=_fleet()
    )

    assert "brier_skill_ppm=-2100" in text
    assert "brier_skill_ppm=+-2100" not in text


def test_render_provider_lines_zero_and_positive_skill_render_explicit_plus_sign() -> (
    None
):
    """A skill of exactly zero (or any positive value) renders an explicit
    `+` sign -- only a strictly negative skill omits it (the sign is never
    ambiguous between "not yet measured" and "at or above baseline").
    """
    from windbreak.reports.providers import render_provider_lines

    text = render_provider_lines((_row(brier_skill_ppm=0),), fleet=_fleet())

    assert "brier_skill_ppm=+0" in text


def test_render_provider_lines_renders_multiple_providers_each_on_their_own_line() -> (
    None
):
    """Two providers render as two distinct, fully-populated lines."""
    from windbreak.reports.providers import render_provider_lines

    rows = (
        _row(provider="futuresearch", brier_skill_ppm=14_200),
        _row(provider="anthropic", brier_skill_ppm=-2_100),
    )

    text = render_provider_lines(rows, fleet=_fleet())

    assert "provider=futuresearch" in text
    assert "provider=anthropic" in text
    assert text.count("provider=") == 2


def test_render_provider_lines_abstain_rate_renders_as_a_whole_percent() -> None:
    """`abstain_rate_ppm` renders as a whole-number percent: 0 ppm -> 0%,
    1_000_000 ppm (100%) -> 100%.
    """
    from windbreak.reports.providers import render_provider_lines

    zero_text = render_provider_lines((_row(abstain_rate_ppm=0),), fleet=_fleet())
    full_text = render_provider_lines(
        (_row(abstain_rate_ppm=1_000_000),), fleet=_fleet()
    )

    assert "abstain_rate=0%" in zero_text
    assert "abstain_rate=100%" in full_text


def test_render_provider_lines_canary_status_renders_verbatim() -> None:
    """A drifting provider's `canary_status` renders verbatim, not just `OK`."""
    from windbreak.reports.providers import render_provider_lines

    text = render_provider_lines((_row(canary_status="ANSWER_DRIFT"),), fleet=_fleet())

    assert "canary=ANSWER_DRIFT" in text


def test_render_provider_lines_per_provider_cost_is_permanently_n_a() -> None:
    """The per-provider `cost_per_forecast` field is always `n/a` -- issue
    #281 (per-provider cost attribution), not this issue.
    """
    from windbreak.reports.providers import render_provider_lines

    text = render_provider_lines((_row(),), fleet=_fleet())

    assert "cost_per_forecast=n/a" in text


def test_render_provider_lines_includes_fleet_cost_lines_when_present() -> None:
    """The fleet-wide cost-per-forecast and cost-per-resolved figures ARE
    derivable in aggregate and render as their own lines.
    """
    from windbreak.reports.providers import render_provider_lines

    text = render_provider_lines(
        (),
        fleet=_fleet(
            cost_per_forecast_micros=125_000, cost_per_resolved_micros=250_000
        ),
    )

    assert "fleet_cost_per_forecast_micros=125000" in text
    assert "fleet_cost_per_resolved_micros=250000" in text


def test_render_provider_lines_fleet_costs_render_n_a_when_none() -> None:
    """Unset (`None`) fleet costs render `n/a`, never `None` or a crash --
    e.g. before any forecast has resolved."""
    from windbreak.reports.providers import render_provider_lines

    text = render_provider_lines((), fleet=_fleet())

    assert "fleet_cost_per_forecast_micros=n/a" in text
    assert "fleet_cost_per_resolved_micros=n/a" in text


def test_render_provider_lines_with_no_providers_still_renders_fleet_lines() -> None:
    """Zero provider rows still renders the fleet summary lines (never
    crashes on an empty rows tuple); the caller (the weekly report / dashboard
    wiring) is responsible for the "No data yet." empty-state fallback."""
    from windbreak.reports.providers import render_provider_lines

    text = render_provider_lines((), fleet=_fleet())

    assert "provider=" not in text
    assert "fleet_cost_per_forecast_micros=n/a" in text
