"""Tests for hedgekit.forecast.coherence (issue #25): SPEC S8.7 group coherence.

Pins `forecast_group`'s Hamilton largest-remainder normalization (exact
member targets, exact remainder-then-raw-then-lexicographic tie-breaks, an
optional residual "other" bucket, always-performed normalization regardless
of coherence), its inclusive tolerance-boundary coherence check, its
input-validation errors, and `GroupCoherenceResult`'s
`coherence_flag` / `eligible_for_live` mutual-exclusion invariant.
`hedgekit/forecast/coherence.py` does not exist yet, so importing
`hedgekit.forecast.coherence` fails collection with
`ModuleNotFoundError: No module named 'hedgekit.forecast.coherence'` -- the
expected Gate 1 RED state for issue #25.
"""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from hedgekit.forecast.coherence import (
    OTHER_BUCKET_KEY,
    GroupCoherenceResult,
    forecast_group,
)

_VALID_GROUP_COHERENCE_RESULT_KWARGS: dict[str, object] = {
    "outcome_key": "A",
    "raw_probability_ppm": 600_000,
    "normalized_probability_ppm": 400_000,
    "coherence_group_sum_ppm": 1_500_000,
    "coherence_flag": True,
    "eligible_for_live": False,
}


def _group_result(**overrides: object) -> GroupCoherenceResult:
    merged = {**_VALID_GROUP_COHERENCE_RESULT_KWARGS, **overrides}
    return GroupCoherenceResult(**merged)


# --- Pinned case 1: the issue's incoherent three-way example -----------------------


def test_forecast_group_issue_example_flags_and_normalizes_exactly() -> None:
    """The SPEC issue example: 60/60/30 raw sums to 1.5, is flagged
    incoherent, and its Hamilton-normalized members still sum to 1_000_000.
    """
    votes = {"A": 600_000, "B": 600_000, "C": 300_000}

    results = forecast_group(votes, tolerance_ppm=100_000)

    by_key = {result.outcome_key: result for result in results}
    assert [result.outcome_key for result in results] == ["A", "B", "C"]
    assert by_key["A"].normalized_probability_ppm == 400_000
    assert by_key["B"].normalized_probability_ppm == 400_000
    assert by_key["C"].normalized_probability_ppm == 200_000
    assert all(result.coherence_group_sum_ppm == 1_500_000 for result in results)
    assert all(result.coherence_flag is True for result in results)
    assert all(result.eligible_for_live is False for result in results)
    assert sum(result.normalized_probability_ppm for result in results) == 1_000_000


# --- Pinned case 2: in-tolerance exact Hamilton remainder allocation ---------------


def test_forecast_group_in_tolerance_hamilton_remainder_is_exact() -> None:
    """520_000/490_000 (raw sum 1_010_000) is within a 100_000 tolerance, so
    the group is coherent; the single deficit unit goes to B, whose
    remainder (520_000) exceeds A's (490_000) under `divmod(raw*T, raw_sum)`.
    """
    votes = {"A": 520_000, "B": 490_000}

    results = forecast_group(votes, tolerance_ppm=100_000)

    by_key = {result.outcome_key: result for result in results}
    assert by_key["A"].normalized_probability_ppm == 514_851
    assert by_key["B"].normalized_probability_ppm == 485_149
    assert all(result.coherence_flag is False for result in results)
    assert all(result.eligible_for_live is True for result in results)
    assert sum(result.normalized_probability_ppm for result in results) == 1_000_000


# --- Pinned case 3: tolerance boundary is inclusive --------------------------------


def test_forecast_group_tolerance_boundary_is_inclusive() -> None:
    """550_000/550_000 (raw sum 1_010_000... boundary exactly 100_000 over)
    sits exactly at the tolerance boundary and must NOT be flagged (`<=`).
    """
    votes = {"A": 550_000, "B": 550_000}

    results = forecast_group(votes, tolerance_ppm=100_000)

    by_key = {result.outcome_key: result for result in results}
    assert all(result.coherence_flag is False for result in results)
    assert all(result.eligible_for_live is True for result in results)
    assert by_key["A"].normalized_probability_ppm == 500_000
    assert by_key["B"].normalized_probability_ppm == 500_000


def test_forecast_group_under_sum_group_is_flagged() -> None:
    """A raw sum *below* target beyond tolerance is incoherent too (`abs`).

    The pinned over-sum cases only exercise ``raw_sum > target``; this asserts
    the symmetric under-sum side so a mutant dropping the ``abs()`` around the
    tolerance comparison cannot survive.
    """
    votes = {"A": 100_000, "B": 100_000}

    results = forecast_group(votes, tolerance_ppm=0)

    assert all(result.coherence_flag is True for result in results)
    assert all(result.eligible_for_live is False for result in results)
    assert all(result.coherence_group_sum_ppm == 200_000 for result in results)


# --- Pinned case 4: equal-remainder tie-break favors the smaller key ---------------


def test_forecast_group_tie_break_favors_smaller_key() -> None:
    """Three equal 333_333 votes (raw sum 999_999) all tie on remainder and
    raw value, so the single deficit unit goes to the lexicographically
    smallest key ("A"), regardless of input iteration order ("C", "B", "A").
    Output order must still mirror input order.
    """
    votes = {"C": 333_333, "B": 333_333, "A": 333_333}

    results = forecast_group(votes, tolerance_ppm=1_000)

    assert [result.outcome_key for result in results] == ["C", "B", "A"]
    by_key = {result.outcome_key: result for result in results}
    assert by_key["A"].normalized_probability_ppm == 333_334
    assert by_key["B"].normalized_probability_ppm == 333_333
    assert by_key["C"].normalized_probability_ppm == 333_333
    assert sum(result.normalized_probability_ppm for result in results) == 1_000_000


# --- Pinned case 5: determinism -----------------------------------------------------


def test_forecast_group_is_deterministic_for_identical_input() -> None:
    """Calling `forecast_group` twice with an equal mapping yields an
    identical result tuple.
    """
    votes = {"A": 600_000, "B": 600_000, "C": 300_000}

    first = forecast_group(votes, tolerance_ppm=100_000)
    second = forecast_group(dict(votes), tolerance_ppm=100_000)

    assert first == second


# --- Pinned case 6: residual "other" bucket -----------------------------------------


def test_forecast_group_other_bucket_grand_total_is_one_million() -> None:
    """A coherent group with a configured other-bucket: members normalize to
    exactly `1_000_000 - other_bucket_ppm`, the other bucket is appended last
    with `normalized == raw == other_bucket_ppm`, and the grand total is
    exactly 1_000_000.
    """
    votes = {"A": 300_000, "B": 300_000}

    results = forecast_group(votes, tolerance_ppm=100_000, other_bucket_ppm=400_000)

    assert [result.outcome_key for result in results] == ["A", "B", OTHER_BUCKET_KEY]
    by_key = {result.outcome_key: result for result in results}
    assert by_key["A"].normalized_probability_ppm == 300_000
    assert by_key["B"].normalized_probability_ppm == 300_000
    other = by_key[OTHER_BUCKET_KEY]
    assert other.raw_probability_ppm == 400_000
    assert other.normalized_probability_ppm == 400_000
    assert sum(result.normalized_probability_ppm for result in results) == 1_000_000
    assert all(result.coherence_flag is False for result in results)
    assert all(result.eligible_for_live is True for result in results)


def test_forecast_group_flagged_case_also_flags_the_other_bucket() -> None:
    """When the member group is incoherent, the other bucket is flagged too
    -- coherence is a property of the whole group, not just its members.
    """
    votes = {"A": 500_000, "B": 500_000}

    results = forecast_group(votes, tolerance_ppm=0, other_bucket_ppm=400_000)

    assert all(result.coherence_flag is True for result in results)
    assert all(result.eligible_for_live is False for result in results)
    by_key = {result.outcome_key: result for result in results}
    assert by_key["A"].normalized_probability_ppm == 300_000
    assert by_key["B"].normalized_probability_ppm == 300_000
    assert by_key[OTHER_BUCKET_KEY].normalized_probability_ppm == 400_000
    assert sum(result.normalized_probability_ppm for result in results) == 1_000_000


# --- Pinned case 7: input-validation errors -----------------------------------------


def test_forecast_group_empty_mapping_raises_value_error() -> None:
    """An empty `votes_ppm` mapping has nothing to normalize."""
    with pytest.raises(ValueError):
        forecast_group({}, tolerance_ppm=0)


def test_forecast_group_zero_raw_sum_raises_value_error() -> None:
    """An all-zero mapping has an undefined Hamilton target ratio."""
    with pytest.raises(ValueError):
        forecast_group({"A": 0, "B": 0}, tolerance_ppm=0)


def test_forecast_group_negative_tolerance_raises_value_error() -> None:
    """A negative tolerance is not a meaningful coherence threshold."""
    with pytest.raises(ValueError):
        forecast_group({"A": 500_000}, tolerance_ppm=-1)


@pytest.mark.parametrize("bad_other_bucket_ppm", [-1, 1_000_000])
def test_forecast_group_other_bucket_out_of_range_raises_value_error(
    bad_other_bucket_ppm: int,
) -> None:
    """`other_bucket_ppm` must sit in the half-open `[0, 1_000_000)` range --
    a full 1_000_000 would leave zero room for any member.
    """
    with pytest.raises(ValueError):
        forecast_group(
            {"A": 500_000}, tolerance_ppm=0, other_bucket_ppm=bad_other_bucket_ppm
        )


def test_forecast_group_reserved_key_collision_raises_value_error() -> None:
    """An input key equal to `OTHER_BUCKET_KEY` would silently collide with
    the residual bucket and must be rejected instead.
    """
    with pytest.raises(ValueError):
        forecast_group({OTHER_BUCKET_KEY: 500_000, "A": 500_000}, tolerance_ppm=0)


# --- Pinned case 8: GroupCoherenceResult validation ---------------------------------


def test_group_coherence_result_constructs_with_valid_fields() -> None:
    """A fully valid GroupCoherenceResult constructs and preserves its fields."""
    result = _group_result()

    assert result.outcome_key == "A"
    assert result.coherence_group_sum_ppm == 1_500_000


def test_group_coherence_result_flag_and_eligible_both_true_raises() -> None:
    """`coherence_flag=True` and `eligible_for_live=True` together are an
    invalid, self-contradicting combination.
    """
    with pytest.raises(ValueError):
        _group_result(coherence_flag=True, eligible_for_live=True)


def test_group_coherence_result_accepts_group_sum_over_one_million() -> None:
    """`coherence_group_sum_ppm` is an unbounded raw sum, not a ppm fraction,
    so values above 1_000_000 (the whole point of an incoherent group) are
    legal.
    """
    result = _group_result(coherence_group_sum_ppm=1_500_000)

    assert result.coherence_group_sum_ppm == 1_500_000


def test_group_coherence_result_negative_group_sum_raises_value_error() -> None:
    """A negative raw sum is nonsensical and must be rejected."""
    with pytest.raises(ValueError, match="coherence_group_sum_ppm"):
        _group_result(coherence_group_sum_ppm=-1)


def test_group_coherence_result_bool_group_sum_raises_type_error() -> None:
    """A stray `bool` must never masquerade as `coherence_group_sum_ppm`."""
    with pytest.raises(TypeError):
        _group_result(coherence_group_sum_ppm=True)


@pytest.mark.parametrize("field", ["raw_probability_ppm", "normalized_probability_ppm"])
@pytest.mark.parametrize("bad_value", [-1, 1_000_001])
def test_group_coherence_result_out_of_range_ppm_field_raises_value_error(
    field: str, bad_value: int
) -> None:
    """`raw_probability_ppm` / `normalized_probability_ppm` stay within
    `[0, 1_000_000]`.
    """
    with pytest.raises(ValueError, match=field):
        _group_result(**{field: bad_value})


def test_group_coherence_result_empty_outcome_key_raises_value_error() -> None:
    """`outcome_key` must be non-empty."""
    with pytest.raises(ValueError, match="outcome_key"):
        _group_result(outcome_key="")


def test_group_coherence_result_is_frozen() -> None:
    """Mutating any field of a constructed GroupCoherenceResult raises."""
    result = _group_result()

    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        result.outcome_key = "other"  # type: ignore[misc]


# --- Reserved-key constant -----------------------------------------------------------


def test_other_bucket_key_is_the_reserved_sentinel_string() -> None:
    """`OTHER_BUCKET_KEY` is the pinned, human-unlikely sentinel string."""
    assert OTHER_BUCKET_KEY == "__other__"


# --- Hypothesis properties -----------------------------------------------------------

#: Key alphabet excludes `_` so no generated key can ever collide with
#: `OTHER_BUCKET_KEY` ("__other__"), removing the need for a filtering pass.
_KEY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_votes_strategy = st.dictionaries(
    keys=st.text(alphabet=_KEY_ALPHABET, min_size=1, max_size=6),
    values=st.integers(min_value=0, max_value=1_000_000),
    min_size=1,
    max_size=8,
)


_other_bucket_strategy = st.one_of(
    st.none(), st.integers(min_value=0, max_value=999_999)
)


@given(votes=_votes_strategy, other_bucket_ppm=_other_bucket_strategy)
def test_forecast_group_normalization_properties(
    votes: dict[str, int], other_bucket_ppm: int | None
) -> None:
    """For any non-all-zero mapping of 1-8 outcomes, with or without an other
    bucket: member normalized values sum to exactly `1_000_000 -
    (other_bucket_ppm or 0)`, the grand total is exactly 1_000_000, rank
    order is preserved (a strictly larger raw value never normalizes lower),
    every output stays in the ppm domain, and `coherence_flag` and
    `eligible_for_live` are never both true.
    """
    assume(sum(votes.values()) > 0)

    results = forecast_group(votes, tolerance_ppm=0, other_bucket_ppm=other_bucket_ppm)

    member_target = 1_000_000 - (other_bucket_ppm or 0)
    member_results = [
        result for result in results if result.outcome_key != OTHER_BUCKET_KEY
    ]
    assert (
        sum(result.normalized_probability_ppm for result in member_results)
        == member_target
    )
    assert sum(result.normalized_probability_ppm for result in results) == 1_000_000

    normalized_by_key = {
        result.outcome_key: result.normalized_probability_ppm for result in results
    }
    for key_a, raw_a in votes.items():
        for key_b, raw_b in votes.items():
            if raw_a > raw_b:
                assert normalized_by_key[key_a] >= normalized_by_key[key_b]

    for result in results:
        assert 0 <= result.raw_probability_ppm <= 1_000_000
        assert 0 <= result.normalized_probability_ppm <= 1_000_000
        assert (not result.coherence_flag) or (not result.eligible_for_live)
