"""SPEC S8.7 group coherence normalization (Hamilton largest-remainder).

Normalizes a group of mutually-exclusive outcome probabilities so their member
total is exactly ``1_000_000 - other_bucket_ppm`` ppm, using integer-only
Hamilton (largest-remainder) apportionment with a fully deterministic
remainder-then-raw-then-lexicographic tie-break. Normalization is *always*
performed; an incoherent group -- one whose raw member sum strays outside the
inclusive tolerance band around the member target -- is additionally flagged
and barred from live eligibility, on every result including the residual
bucket. Every quantity is a bare parts-per-million integer (SPEC S6.3), so no
float ever enters the probability path guarded by ``scripts/lint_no_floats.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from windbreak.forecast.records import _require_non_empty, _require_ppm

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The reserved outcome key for the residual "other" probability bucket, chosen
#: to be human-unlikely so a real outcome key never silently collides with it.
OTHER_BUCKET_KEY = "__other__"

#: One full probability (1.0) in ppm: the grand-total apportionment target.
_PPM_SCALE = 1_000_000


def _require_non_negative_int(value: object, field_name: str) -> None:
    """Guard that a value is a true, non-negative integer (never a ``bool``).

    Args:
        value: The candidate value.
        field_name: The owning field's name, surfaced in the error message.

    Raises:
        TypeError: If ``value`` is a ``bool`` or is not an ``int``.
        ValueError: If ``value`` is negative. The message names ``field_name``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{field_name} must be a non-bool int, got {type(value).__name__}"
        )
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative, got {value}")


def _validate_other_bucket(other_bucket_ppm: int | None) -> None:
    """Guard the optional residual-bucket ppm against its half-open range.

    Args:
        other_bucket_ppm: The residual bucket size in ppm, or ``None``.

    Raises:
        ValueError: If not ``None`` and not a non-bool int within the half-open
            range ``[0, 1_000_000)`` (a full 1_000_000 would leave no room for
            any member outcome).
    """
    if other_bucket_ppm is None:
        return
    valid = (
        not isinstance(other_bucket_ppm, bool)
        and isinstance(other_bucket_ppm, int)
        and 0 <= other_bucket_ppm < _PPM_SCALE
    )
    if not valid:
        raise ValueError(
            f"other_bucket_ppm must be a non-bool int in [0, {_PPM_SCALE}), "
            f"got {other_bucket_ppm!r}"
        )


@dataclass(frozen=True, slots=True)
class GroupCoherenceResult:
    """One outcome's normalized probability and coherence verdict (SPEC S8.7).

    Attributes:
        outcome_key: The outcome's identifier within its group.
        raw_probability_ppm: The outcome's pre-normalization probability, ppm.
        normalized_probability_ppm: The Hamilton-normalized probability, ppm.
        coherence_group_sum_ppm: The raw member sum of the whole group, ppm;
            unbounded above (an incoherent group sums past 1_000_000).
        coherence_flag: Whether the group was flagged incoherent.
        eligible_for_live: Whether the outcome may back a live order.
    """

    outcome_key: str
    raw_probability_ppm: int
    normalized_probability_ppm: int
    coherence_group_sum_ppm: int
    coherence_flag: bool
    eligible_for_live: bool

    def __post_init__(self) -> None:
        """Validate the ppm ranges, identifier, group sum, and live invariant.

        Raises:
            TypeError: If any guarded integer is a ``bool`` or non-``int``.
            ValueError: If a ppm field is out of range, ``outcome_key`` is
                empty, ``coherence_group_sum_ppm`` is negative, or the record
                is both flagged incoherent and live-eligible.
        """
        _require_ppm(self.raw_probability_ppm, "raw_probability_ppm")
        _require_ppm(self.normalized_probability_ppm, "normalized_probability_ppm")
        _require_non_empty(self.outcome_key, "outcome_key")
        _require_non_negative_int(
            self.coherence_group_sum_ppm, "coherence_group_sum_ppm"
        )
        if self.coherence_flag and self.eligible_for_live:
            raise ValueError(
                "eligible_for_live must be False when coherence_flag is True"
            )


def _validate_group_inputs(
    votes_ppm: Mapping[str, int],
    tolerance_ppm: int,
    other_bucket_ppm: int | None,
) -> int:
    """Validate a group's inputs and return its raw member sum.

    Args:
        votes_ppm: The raw per-outcome probabilities, in ppm.
        tolerance_ppm: The inclusive coherence tolerance band, in ppm.
        other_bucket_ppm: The optional residual bucket size, in ppm.

    Returns:
        The raw member sum (strictly positive), in ppm.

    Raises:
        TypeError: If ``tolerance_ppm`` or a member value is a ``bool`` or
            non-``int``.
        ValueError: If the mapping is empty, ``tolerance_ppm`` is negative,
            ``other_bucket_ppm`` is out of range, an input key collides with
            the reserved other-bucket key, a member value is out of ppm range,
            or the members sum to zero.
    """
    if not votes_ppm:
        raise ValueError("votes_ppm must be a non-empty mapping")
    _require_non_negative_int(tolerance_ppm, "tolerance_ppm")
    _validate_other_bucket(other_bucket_ppm)
    if OTHER_BUCKET_KEY in votes_ppm:
        raise ValueError(
            f"{OTHER_BUCKET_KEY!r} is reserved for the residual bucket "
            "and cannot be an input outcome key"
        )
    for key, raw in votes_ppm.items():
        _require_ppm(raw, f"votes_ppm[{key!r}]")
    raw_sum = sum(votes_ppm.values())
    if raw_sum <= 0:
        raise ValueError("votes_ppm must sum to a positive value")
    return raw_sum


def _hamilton_apportion(votes_ppm: Mapping[str, int], target: int) -> dict[str, int]:
    """Apportion ``target`` ppm across outcomes by largest remainder.

    Each outcome receives ``base_i = (raw_i * target) // raw_sum`` ppm; the
    ``target - sum(base_i)`` leftover units are awarded one apiece to the
    outcomes ranked by larger remainder, then larger raw value, then
    lexicographically smaller key -- a fully deterministic tie-break. The
    returned shares sum to exactly ``target``.

    Args:
        votes_ppm: The raw per-outcome probabilities, in ppm.
        target: The exact ppm total to apportion (strictly positive).

    Returns:
        A mapping from each outcome key to its normalized ppm share.
    """
    raw_sum = sum(votes_ppm.values())
    bases: dict[str, int] = {}
    remainders: dict[str, int] = {}
    for key, raw in votes_ppm.items():
        base, remainder = divmod(raw * target, raw_sum)
        bases[key] = base
        remainders[key] = remainder
    deficit = target - sum(bases.values())
    ranked = sorted(votes_ppm, key=lambda key: (-remainders[key], -votes_ppm[key], key))
    for key in ranked[:deficit]:
        bases[key] += 1
    return bases


def _coherence_result(
    outcome_key: str, raw_ppm: int, normalized_ppm: int, raw_sum: int, *, coherent: bool
) -> GroupCoherenceResult:
    """Build one :class:`GroupCoherenceResult` from its resolved parts.

    Args:
        outcome_key: The outcome's identifier (or the residual-bucket key).
        raw_ppm: The outcome's raw probability, in ppm.
        normalized_ppm: The outcome's normalized probability, in ppm.
        raw_sum: The group's raw member sum, in ppm.
        coherent: Whether the whole group is coherent; drives both the flag
            (its negation) and live eligibility.

    Returns:
        The assembled, validated coherence result.
    """
    return GroupCoherenceResult(
        outcome_key=outcome_key,
        raw_probability_ppm=raw_ppm,
        normalized_probability_ppm=normalized_ppm,
        coherence_group_sum_ppm=raw_sum,
        coherence_flag=not coherent,
        eligible_for_live=coherent,
    )


def forecast_group(
    votes_ppm: Mapping[str, int],
    *,
    tolerance_ppm: int,
    other_bucket_ppm: int | None = None,
) -> tuple[GroupCoherenceResult, ...]:
    """Normalize and coherence-check a group of mutually-exclusive outcomes.

    Members are Hamilton-normalized so they sum to exactly
    ``1_000_000 - (other_bucket_ppm or 0)``; when ``other_bucket_ppm`` is given,
    a residual bucket keyed :data:`OTHER_BUCKET_KEY` is appended last so the
    grand total is exactly 1_000_000. The group is coherent when its raw member
    sum sits within ``tolerance_ppm`` of the member target (inclusive); an
    incoherent group is flagged and barred from live eligibility, on every
    result including the residual bucket.

    Args:
        votes_ppm: The raw per-outcome probabilities, in ppm, in the order
            results should be returned.
        tolerance_ppm: The inclusive coherence tolerance band, in ppm.
        other_bucket_ppm: The optional residual bucket size, in ppm; ``None``
            omits the bucket.

    Returns:
        One :class:`GroupCoherenceResult` per input outcome in input order,
        followed by the residual-bucket result when ``other_bucket_ppm`` is set.

    Raises:
        TypeError: If ``tolerance_ppm`` or a member value is a ``bool`` or
            non-``int``.
        ValueError: On any input-validation failure (see
            :func:`_validate_group_inputs`).
    """
    raw_sum = _validate_group_inputs(votes_ppm, tolerance_ppm, other_bucket_ppm)
    target = _PPM_SCALE - (other_bucket_ppm or 0)
    normalized = _hamilton_apportion(votes_ppm, target)
    coherent = abs(raw_sum - target) <= tolerance_ppm
    results = [
        _coherence_result(
            key, votes_ppm[key], normalized[key], raw_sum, coherent=coherent
        )
        for key in votes_ppm
    ]
    if other_bucket_ppm is not None:
        results.append(
            _coherence_result(
                OTHER_BUCKET_KEY,
                other_bucket_ppm,
                other_bucket_ppm,
                raw_sum,
                coherent=coherent,
            )
        )
    return tuple(results)
