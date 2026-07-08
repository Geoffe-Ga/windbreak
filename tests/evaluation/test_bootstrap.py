"""Failing-first tests for `windbreak.evaluation.bootstrap` (issue #51, RED;
SPEC-EPIC_07 S13.5, S21 glossary "clustered bootstrap").

`windbreak.evaluation.bootstrap` does not exist yet, so every test below fails
collection/execution with `ModuleNotFoundError: No module named
'windbreak.evaluation.bootstrap'` -- the expected Gate 1 RED state for issue
#51.

Pins the clustered-bootstrap confidence interval for the headline Brier skill
metric:

- `brier_skill_ci(inputs, *, confidence_ppm, seed, replicates=..., window=...)`
  clusters resampling by `FixtureForecast.correlation_group_id` (a market with
  no group id is its own singleton cluster), never by raw market ticker --
  so 30 perfectly-correlated markets in 3 real event groups resample as 3
  independent clusters, not 30.
- There is deliberately no separate "naive"/unclustered bootstrap function:
  the "unclustered reference" in this suite is the *same* public
  `brier_skill_ci` called on inputs with every `correlation_group_id`
  stripped (each market becomes its own singleton cluster).
- The percentile-index arithmetic for the two-sided confidence interval is
  pinned as a standalone unit test of `_percentile_indices`.
- Identical inputs and seed produce a byte-identical `ClusteredCiResult`
  (SPEC S3.5 determinism).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from windbreak.evaluation import (
    EvaluationInputs,
    FixtureForecast,
    ObservationWindow,
    ResolutionOutcome,
)
from windbreak.numeric.types import ProbabilityPpm

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The synthetic known-answer fixture shared by issues #49-#55: 10 forecasts,
#: no `correlation_group_id` field at all, so every market is its own
#: singleton cluster.
SYNTHETIC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_known_answer.json"
)

#: The new fixture with known correlation structure: 3 event groups (EVT-A,
#: EVT-B, EVT-C) of 10 perfectly-correlated markets each; see its own
#: "description" key for the full hand-derivation this suite pins against.
CLUSTERED_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "clustered_fixture.json"
)

_WINDOW = ObservationWindow.LATEST_BEFORE_CLOSE

#: Hand-derived support set for the clustered fixture's bootstrap replicates
#: (see `clustered_fixture.json`'s own "description" for the full derivation
#: of each of the 10 possible 3-draw-with-replacement multisets of
#: {EVT-A, EVT-B, EVT-C}). Because a replicate value's exact internal
#: rounding (floor vs. ceil) is not pinned by this issue's design, both the
#: floor- and ceil-rounded candidate is included for every multiset -- any
#: single-seed bootstrap's order-statistic bound must land on one of these
#: candidates. The `seed=42` example additionally pins its bounds to the exact
#: support extremes (see `test_clustered_ci_respects_correlation_groups`).
_CLUSTERED_REPLICATE_SUPPORT_SET_PPM = frozenset(
    {
        -1_250_000,  # BBB (exact: -5/4)
        -294_118,
        -294_117,  # ABB (-5/17)
        -30_304,
        -30_303,  # BBC (-1/33)
        227_272,
        227_273,  # AAB (5/22)
        236_842,
        236_843,  # ABC (9/38) -- also the full-sample point estimate
        240_740,
        240_741,  # BCC (13/54)
        441_860,
        441_861,  # AAC (19/43)
        389_830,
        389_831,  # ACC (23/59)
        360_000,  # CCC (exact: 9/25)
        555_555,
        555_556,  # AAA (5/9)
    }
)

#: The exact hand-derived full-sample point estimate on the clustered
#: fixture: forecast_sum = 10*(0.04+0.09+0.16) = 2.9, baseline_sum =
#: 10*(0.09+0.04+0.25) = 3.8; skill = 1 - 29/38 = 9/38 -> floor(9/38 *
#: 1_000_000) = 236_842 ppm exactly (see `clustered_fixture.json`).
_EXPECTED_CLUSTERED_POINT_ESTIMATE_PPM = 236_842


def _load_json(path: Path) -> dict[str, Any]:
    """Load and JSON-decode a fixture file.

    Args:
        path: The fixture file path.

    Returns:
        The decoded payload.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _forecast_from_entry(entry: Mapping[str, Any]) -> FixtureForecast:
    """Build a `FixtureForecast` from one raw fixture forecast entry,
    including the new `correlation_group_id` field via `.get(...)` so a
    fixture with no such key (the synthetic fixture) defaults every forecast
    to an ungrouped (`None`) singleton cluster.

    Duplicated locally rather than imported cross-test-module, matching
    `test_baselines.py`'s established import-isolation convention.

    Args:
        entry: The decoded forecast object from a fixture.

    Returns:
        The typed forecast row.
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


def _inputs_from_payload(payload: Mapping[str, Any]) -> EvaluationInputs:
    """Build typed `EvaluationInputs` directly from a decoded fixture payload.

    Args:
        payload: The decoded fixture payload.

    Returns:
        The typed evaluation inputs, including each forecast's
        `correlation_group_id` where present.
    """
    forecasts = tuple(_forecast_from_entry(entry) for entry in payload["forecasts"])
    resolutions = _resolutions_from_entries(payload["resolutions"])
    return EvaluationInputs(forecasts=forecasts, resolutions=resolutions)


def _synthetic_inputs() -> EvaluationInputs:
    """Build `EvaluationInputs` from the shared synthetic known-answer
    fixture (no `correlation_group_id` anywhere -- 10 singleton clusters).

    Returns:
        The typed evaluation inputs.
    """
    return _inputs_from_payload(_load_json(SYNTHETIC_FIXTURE))


def clustered_fixture() -> EvaluationInputs:
    """Build `EvaluationInputs` from the known-correlation clustered fixture.

    Named to match the issue's own verbatim acceptance-test example
    (`brier_skill_ci(clustered_fixture(), ...)`).

    Returns:
        The typed evaluation inputs: 30 forecasts across 3
        `correlation_group_id` clusters (EVT-A, EVT-B, EVT-C).
    """
    return _inputs_from_payload(_load_json(CLUSTERED_FIXTURE))


def _stripped_of_correlation_groups(inputs: EvaluationInputs) -> EvaluationInputs:
    """Return a copy of `inputs` with every `correlation_group_id` cleared.

    Per this issue's design, there is no separate "naive"/unclustered
    bootstrap function; the unclustered reference is produced by calling the
    same public `brier_skill_ci` on inputs whose group ids have been erased,
    so each market becomes its own singleton cluster.

    Args:
        inputs: The evaluation inputs to strip.

    Returns:
        A new `EvaluationInputs` with `correlation_group_id=None` on every
        forecast, otherwise identical.
    """
    stripped = tuple(
        replace(forecast, correlation_group_id=None) for forecast in inputs.forecasts
    )
    return EvaluationInputs(forecasts=stripped, resolutions=inputs.resolutions)


def unclustered_width_reference() -> int:
    """Compute the clustered fixture's CI width with group ids stripped.

    Calls the same public `brier_skill_ci` API used throughout this suite,
    on the clustered fixture's 30 forecasts each treated as its own
    singleton cluster (`effective_n == 30`), for direct comparison against
    the real 3-cluster CI width.

    Returns:
        The `ci_width` of the stripped-input bootstrap result, using the
        same confidence level and seed as the primary clustered-fixture test.
    """
    from windbreak.evaluation.bootstrap import brier_skill_ci

    stripped = _stripped_of_correlation_groups(clustered_fixture())
    result = brier_skill_ci(stripped, confidence_ppm=950_000, seed=42, window=_WINDOW)
    return result.ci_width


# ---------------------------------------------------------------------------
# 1. Module constants.
# ---------------------------------------------------------------------------


def test_bootstrap_replicates_constant_is_one_thousand() -> None:
    """`BOOTSTRAP_REPLICATES` is `1_000`, the documented default replicate
    count (matches the issue's own worked percentile-index example).
    """
    from windbreak.evaluation.bootstrap import BOOTSTRAP_REPLICATES

    assert BOOTSTRAP_REPLICATES == 1_000


# ---------------------------------------------------------------------------
# 2. Percentile-index arithmetic, pinned directly.
# ---------------------------------------------------------------------------


def test_percentile_indices_for_one_thousand_replicates_at_ninety_five_percent() -> (
    None
):
    """For `replicates=1_000`, `confidence_ppm=950_000`:
    `alpha_half_ppm = (1_000_000 - 950_000) // 2 = 25_000`;
    `lo_idx = divide(1_000 * 25_000, 1_000_000, UNDERSTATE_EQUITY)
            = divide(25_000_000, 1_000_000, floor) = 25`;
    `hi_idx = 1_000 - 1 - 25 = 974`.
    """
    from windbreak.evaluation.bootstrap import _percentile_indices

    lo_idx, hi_idx = _percentile_indices(replicates=1_000, confidence_ppm=950_000)

    assert lo_idx == 25
    assert hi_idx == 974


# ---------------------------------------------------------------------------
# 3. ClusteredCiResult shape and singleton degradation.
# ---------------------------------------------------------------------------


def test_clustered_ci_result_is_a_frozen_dataclass_with_the_documented_fields() -> None:
    """`ClusteredCiResult` carries exactly the documented fields and cannot
    be mutated after construction.
    """
    from windbreak.evaluation.bootstrap import ClusteredCiResult

    result = ClusteredCiResult(
        point_estimate_ppm=100_000,
        ci_low_ppm=50_000,
        ci_high_ppm=150_000,
        ci_width=100_000,
        effective_n=3,
        replicates=1_000,
        seed=42,
        confidence_ppm=950_000,
        window=_WINDOW,
    )

    assert result.ci_width == result.ci_high_ppm - result.ci_low_ppm
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ci_low_ppm = 0  # type: ignore[misc]


def test_brier_skill_ci_degrades_to_one_singleton_cluster_per_market() -> None:
    """With no `correlation_group_id` anywhere (the synthetic fixture),
    every one of the 10 resolved markets is its own singleton cluster, so
    `effective_n == 10`.
    """
    from windbreak.evaluation.bootstrap import brier_skill_ci

    result = brier_skill_ci(
        _synthetic_inputs(), confidence_ppm=950_000, seed=1, window=_WINDOW
    )

    assert result.effective_n == 10


# ---------------------------------------------------------------------------
# 4. Determinism.
# ---------------------------------------------------------------------------


def test_brier_skill_ci_is_deterministic_for_identical_inputs_and_seed() -> None:
    """Two calls with identical inputs and seed produce a byte-identical
    `ClusteredCiResult` (SPEC S3.5).
    """
    from windbreak.evaluation.bootstrap import brier_skill_ci

    inputs = clustered_fixture()

    first = brier_skill_ci(inputs, confidence_ppm=950_000, seed=42, window=_WINDOW)
    second = brier_skill_ci(inputs, confidence_ppm=950_000, seed=42, window=_WINDOW)

    assert first == second


# ---------------------------------------------------------------------------
# 5. The issue's own verbatim acceptance scenario.
# ---------------------------------------------------------------------------


def test_clustered_ci_respects_correlation_groups() -> None:
    """The issue's own acceptance example: 3 independent clusters, not 30
    independent markets, and a clustered CI strictly wider than the
    unclustered (group-id-stripped) reference computed via the same public
    API.
    """
    from windbreak.evaluation.bootstrap import brier_skill_ci

    result = brier_skill_ci(
        clustered_fixture(), confidence_ppm=950_000, seed=42, window=_WINDOW
    )

    assert result.effective_n == 3
    assert result.point_estimate_ppm == _EXPECTED_CLUSTERED_POINT_ESTIMATE_PPM
    assert result.ci_low_ppm in _CLUSTERED_REPLICATE_SUPPORT_SET_PPM
    assert result.ci_high_ppm in _CLUSTERED_REPLICATE_SUPPORT_SET_PPM
    assert result.ci_low_ppm <= result.ci_high_ppm
    # The seed=42 order statistics are deterministic (SplitMix64, SPEC S3.5):
    # the 2.5%/97.5% bounds land on the support extremes -- BBB's exact minimum
    # (-5/4 -> floor -1_250_000) and AAA's ceil-rounded maximum (5/9 -> ceil
    # 555_556) -- so the 95% CI width is exactly their span, 1_805_556 ppm.
    assert result.ci_low_ppm == -1_250_000
    assert result.ci_high_ppm == 555_556
    assert result.ci_width == 1_805_556
    assert result.ci_width > unclustered_width_reference()


def test_every_replicate_bound_is_within_the_clustered_support_set() -> None:
    """Both CI edges of the clustered-fixture result land in the hand-
    derived 10-multiset support set, for a second, independent seed -- not
    just the issue's own pinned `seed=42` example.
    """
    from windbreak.evaluation.bootstrap import brier_skill_ci

    result = brier_skill_ci(
        clustered_fixture(), confidence_ppm=950_000, seed=1_234, window=_WINDOW
    )

    assert result.ci_low_ppm in _CLUSTERED_REPLICATE_SUPPORT_SET_PPM
    assert result.ci_high_ppm in _CLUSTERED_REPLICATE_SUPPORT_SET_PPM


# ---------------------------------------------------------------------------
# 6. Error paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence_ppm", [0, 1_000_000, -1, 1_000_001])
def test_brier_skill_ci_rejects_confidence_ppm_outside_open_unit_interval(
    confidence_ppm: int,
) -> None:
    """`confidence_ppm` outside the open interval `(0, 1_000_000)` raises
    `ValueError`.
    """
    from windbreak.evaluation.bootstrap import brier_skill_ci

    with pytest.raises(ValueError, match="confidence_ppm"):
        brier_skill_ci(
            _synthetic_inputs(), confidence_ppm=confidence_ppm, seed=1, window=_WINDOW
        )


def test_brier_skill_ci_rejects_bool_as_confidence_ppm() -> None:
    """A `bool` masquerading as `confidence_ppm` raises `ValueError` (per
    this issue's own spec: "confidence_ppm outside (0,1e6) or bool ->
    ValueError").
    """
    from windbreak.evaluation.bootstrap import brier_skill_ci

    with pytest.raises(ValueError, match="confidence_ppm"):
        brier_skill_ci(
            _synthetic_inputs(),
            confidence_ppm=True,  # type: ignore[arg-type]
            seed=1,
            window=_WINDOW,
        )
