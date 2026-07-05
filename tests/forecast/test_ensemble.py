"""Tests for hedgekit.forecast.ensemble (issue #25): SPEC S8.6 vote aggregation.

Pins `aggregate_votes`'s integer median (exclusive-median floor-on-ties
rounding), its exclusive-median (Moore-McCabe) IQR dispersion (Q1 rounds
down, Q3 rounds up -- a risk-widening convention), its min/max confidence
bounds, its unpinned-provenance guard, and `VoteAggregate`'s immutability.
`hedgekit/forecast/ensemble.py` does not exist yet, so importing
`hedgekit.forecast.ensemble` fails collection with
`ModuleNotFoundError: No module named 'hedgekit.forecast.ensemble'` -- the
expected Gate 1 RED state for issue #25.
"""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hedgekit.forecast.ensemble import VoteAggregate, aggregate_votes
from hedgekit.forecast.records import ModelVote

#: Ppm domain bound shared by every probability field under test.
_MAX_PPM = 1_000_000


def mk_vote(probability_ppm: int, **overrides: object) -> ModelVote:
    """Build a `ModelVote` with pinned, valid provenance strings.

    `ModelVote` itself does not reject empty provenance strings (that guard
    lives in `aggregate_votes`), so `**overrides` lets the unpinned-version
    guard tests construct a deliberately malformed vote.

    Args:
        probability_ppm: The vote's probability estimate, in ppm.
        **overrides: Field overrides, applied after the pinned defaults.

    Returns:
        A `ModelVote` with `probability_ppm` set and every other field
        defaulted to a fixed, valid provenance string unless overridden.
    """
    fields: dict[str, object] = {
        "provider": "openai",
        "model_version": "gpt-5-forecast",
        "declared_training_cutoff": "2024-06-01",
        "probability_ppm": probability_ppm,
        "response_fingerprint": "sha256:deadbeef",
    }
    fields.update(overrides)
    return ModelVote(**fields)


# --- Integer median: odd count ----------------------------------------------------


def test_median_odd_three_votes_is_the_middle_element() -> None:
    """Odd-count median is the sorted middle element; IQR is max-min for n=3."""
    votes = (mk_vote(520_000), mk_vote(410_000), mk_vote(450_000))

    result = aggregate_votes(votes)

    assert result.probability_ppm == 450_000
    assert result.probability_ppm in (410_000, 450_000, 520_000)
    assert result.ci_low_ppm == 410_000
    assert result.ci_high_ppm == 520_000
    assert result.vote_dispersion_ppm == 110_000


# --- Integer median: even count floors, never ceils --------------------------------


def test_median_even_two_votes_floors_the_middle_mean() -> None:
    """Even-count median floors the mean of the two middle votes (n=2 case).

    The two middle values (440_001, 450_000) sum to an odd 890_001, so the
    true mean is 445_000.5: the pinned direction is FLOOR (445_000), never
    CEIL (445_001).
    """
    votes = (mk_vote(440_001), mk_vote(450_000))

    result = aggregate_votes(votes)

    assert result.probability_ppm == 445_000
    assert result.ci_low_ppm == 440_001
    assert result.ci_high_ppm == 450_000
    assert result.vote_dispersion_ppm == 9_999


# --- Exclusive-median (Moore-McCabe) IQR: n=1 edge case ----------------------------


def test_iqr_single_vote_has_zero_dispersion() -> None:
    """A single vote has no spread: dispersion is 0, ci bounds equal the vote."""
    votes = (mk_vote(500_000),)

    result = aggregate_votes(votes)

    assert result.probability_ppm == 500_000
    assert result.ci_low_ppm == 500_000
    assert result.ci_high_ppm == 500_000
    assert result.vote_dispersion_ppm == 0


# --- Exclusive-median IQR: n=4 pins both rounding directions -----------------------


def test_iqr_n4_pins_floor_lower_ceil_upper_rounding_directions() -> None:
    """n=4 IQR: Q1 floors an odd-sum lower pair, Q3 ceils an odd-sum upper pair.

    Sorted votes [100_001, 200_000, 400_000, 500_001]: lower half
    [100_001, 200_000] sums to an odd 300_001 (true mean 150_000.5, Q1 floors
    to 150_000); upper half [400_000, 500_001] sums to an odd 900_001 (true
    mean 450_000.5, Q3 ceils to 450_001). Choosing odd sums on both halves
    makes each rounding direction independently observable.
    """
    votes = (mk_vote(500_001), mk_vote(100_001), mk_vote(400_000), mk_vote(200_000))

    result = aggregate_votes(votes)

    assert result.ci_low_ppm == 100_001
    assert result.ci_high_ppm == 500_001
    assert result.probability_ppm == 300_000
    assert result.vote_dispersion_ppm == 300_001


# --- Exclusive-median IQR: n=5 excludes the median from both halves ----------------


def test_iqr_n5_excludes_median_from_both_halves() -> None:
    """n=5 IQR: the middle element is excluded from both the lower and upper
    halves (Moore-McCabe exclusive-median convention), each an even sub-pair
    whose odd sum again pins the floor/ceil rounding directions.
    """
    votes = (
        mk_vote(300_000),
        mk_vote(500_001),
        mk_vote(100_001),
        mk_vote(400_000),
        mk_vote(200_000),
    )

    result = aggregate_votes(votes)

    assert result.probability_ppm == 300_000
    assert result.ci_low_ppm == 100_001
    assert result.ci_high_ppm == 500_001
    assert result.vote_dispersion_ppm == 300_001


# --- Permutation invariance (explicit pinned case) ---------------------------------


def test_aggregate_votes_is_permutation_invariant_for_a_pinned_case() -> None:
    """Reordering an identical set of votes never changes the aggregate."""
    ascending = (mk_vote(410_000), mk_vote(450_000), mk_vote(520_000))
    shuffled = (mk_vote(520_000), mk_vote(410_000), mk_vote(450_000))

    assert aggregate_votes(ascending) == aggregate_votes(shuffled)


# --- Empty input guard --------------------------------------------------------------


def test_aggregate_votes_empty_sequence_raises_value_error() -> None:
    """Aggregating zero votes is meaningless and must fail loudly."""
    with pytest.raises(ValueError, match="votes"):
        aggregate_votes(())


# --- Unpinned-version guard ----------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["provider", "model_version", "declared_training_cutoff"]
)
def test_aggregate_votes_rejects_unpinned_vote(field: str) -> None:
    """A vote with an empty provider/model_version/training_cutoff is an
    unpinned-version error, not silently averaged in. `ModelVote` itself does
    not reject the empty string, so the guard must live in `aggregate_votes`.
    """
    bad_vote = mk_vote(500_000, **{field: ""})
    votes = (bad_vote, mk_vote(400_000), mk_vote(600_000))

    with pytest.raises(ValueError, match=field):
        aggregate_votes(votes)


# --- VoteAggregate immutability ------------------------------------------------------


def test_vote_aggregate_is_frozen() -> None:
    """Mutating any field of a constructed VoteAggregate raises."""
    result = aggregate_votes((mk_vote(500_000),))

    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        result.probability_ppm = 0  # type: ignore[misc]


# --- Hypothesis properties -----------------------------------------------------------


@given(st.lists(st.integers(min_value=0, max_value=_MAX_PPM), min_size=1, max_size=7))
def test_aggregate_votes_satisfies_range_and_bound_invariants(
    values: list[int],
) -> None:
    """For any 1-7 ppm votes: the median sits within the ci bounds, the ci
    bounds are exactly min/max, dispersion is bounded by the raw spread, an
    odd-count median is one of the input votes, and every output stays a
    ppm-domain integer.
    """
    votes = tuple(mk_vote(value) for value in values)

    result = aggregate_votes(votes)

    assert isinstance(result.probability_ppm, int)
    assert 0 <= result.probability_ppm <= _MAX_PPM
    assert result.ci_low_ppm == min(values)
    assert result.ci_high_ppm == max(values)
    assert result.ci_low_ppm <= result.probability_ppm <= result.ci_high_ppm
    assert 0 <= result.vote_dispersion_ppm <= result.ci_high_ppm - result.ci_low_ppm
    if len(values) % 2 == 1:
        assert result.probability_ppm in values


@given(st.lists(st.integers(min_value=0, max_value=_MAX_PPM), min_size=1, max_size=7))
def test_aggregate_votes_is_permutation_invariant(values: list[int]) -> None:
    """Reversing the same votes never changes the aggregate result."""
    votes = tuple(mk_vote(value) for value in values)
    reversed_votes = tuple(mk_vote(value) for value in reversed(values))

    assert aggregate_votes(votes) == aggregate_votes(reversed_votes)


def test_vote_aggregate_field_names_match_the_contract() -> None:
    """`VoteAggregate` exposes exactly the four spec-mandated ppm fields."""
    field_names = {field.name for field in dataclasses.fields(VoteAggregate)}

    assert field_names == {
        "probability_ppm",
        "vote_dispersion_ppm",
        "ci_low_ppm",
        "ci_high_ppm",
    }
