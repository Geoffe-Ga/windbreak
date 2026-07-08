"""Clustered bootstrap confidence interval for Brier skill (#51; SPEC S13.5).

The headline forecast-skill metric is a point estimate; this module puts a
confidence interval around it by resampling *clusters* of correlated markets,
not individual markets. Thirty perfectly-correlated markets in three event
groups carry only three independent observations, and resampling their groups
(rather than the thirty markets) keeps the interval honestly wide instead of
falsely narrow.

Clustering keys on :attr:`FixtureForecast.correlation_group_id`; a market with
no group id is its own singleton cluster, so ungrouped inputs degrade cleanly to
one cluster per market. There is deliberately no separate "naive" bootstrap: the
unclustered reference is just this same public :func:`brier_skill_ci` called on
inputs whose group ids have been stripped.

Randomness comes from a from-scratch :class:`SplitMix64` generator, chosen over
the standard-library :mod:`random` for two reasons: ``random`` draws trip bandit
rule ``B311`` (which fails Gate 2 with no sanctioned silencer), and a pinned
integer generator guarantees byte-identical output across platforms and Python
builds (SPEC S3.5 determinism). Every replicate statistic is an exact
:class:`fractions.Fraction`; only the final confidence-interval edges are reduced
to ppm ``int``s, widening outward (floor the low edge, ceil the high edge) so the
reported interval never overstates its own precision.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

import hedgekit.evaluation.metrics as metrics
from hedgekit.evaluation.windows import ObservationWindow
from hedgekit.numeric.rounding import RoundingDirection, divide

if TYPE_CHECKING:
    from hedgekit.evaluation.metrics import ForecastTerms
    from hedgekit.evaluation.registry import EvaluationInputs

#: The documented default number of bootstrap replicates.
BOOTSTRAP_REPLICATES = 1_000

#: One whole probability in ppm; also the ppm-scaling factor for skill values.
_PPM_SCALE = 1_000_000

#: SplitMix64's golden-ratio increment (``floor(2**64 / phi)`` made odd); the
#: additive step that walks the generator's internal state.
_GOLDEN_GAMMA = 0x9E3779B97F4A7C15
#: First avalanche multiplier of the SplitMix64 output mix.
_MIX_MULTIPLIER_1 = 0xBF58476D1CE4E5B9
#: Second avalanche multiplier of the SplitMix64 output mix.
_MIX_MULTIPLIER_2 = 0x94D049BB133111EB
#: 64-bit wraparound mask; every SplitMix64 step is taken modulo ``2**64``.
_MASK_64 = (1 << 64) - 1
#: The size of the unsigned 64-bit output range, ``2**64``.
_UINT64_RANGE = 1 << 64


class SplitMix64:
    """A deterministic, pure-integer SplitMix64 pseudo-random generator.

    SplitMix64 (Steele, Lea & Flood, 2014; the algorithm behind the JDK's
    ``SplittableRandom``) advances a 64-bit state by a fixed odd increment and
    avalanches it through two multiply-xorshift rounds. It is used here instead
    of :mod:`random` because ``random`` draws trip bandit ``B311`` and because a
    pinned integer generator is byte-identical across platforms (SPEC S3.5).

    Attributes:
        _state: The current 64-bit generator state.
    """

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        """Seed the generator.

        Args:
            seed: The seed; reduced modulo ``2**64`` to the initial state.
        """
        self._state = seed & _MASK_64

    def next_uint64(self) -> int:
        """Advance the state and return the next 64-bit output.

        Returns:
            A pseudo-random integer in ``[0, 2**64)``.
        """
        self._state = (self._state + _GOLDEN_GAMMA) & _MASK_64
        mixed = self._state
        mixed = ((mixed ^ (mixed >> 30)) * _MIX_MULTIPLIER_1) & _MASK_64
        mixed = ((mixed ^ (mixed >> 27)) * _MIX_MULTIPLIER_2) & _MASK_64
        return mixed ^ (mixed >> 31)

    def below(self, bound: int) -> int:
        """Return an unbiased draw in ``[0, bound)`` by rejection sampling.

        The top partial block of the 64-bit range is rejected so every residue
        class is equally likely -- eliminating the modulo bias a bare
        ``next_uint64() % bound`` would introduce.

        Args:
            bound: The exclusive upper bound; must be positive.

        Returns:
            A uniformly-distributed integer in ``[0, bound)``.
        """
        unbiased_limit = _UINT64_RANGE - (_UINT64_RANGE % bound)
        draw = self.next_uint64()
        while draw >= unbiased_limit:
            draw = self.next_uint64()
        return draw % bound


@dataclass(frozen=True, slots=True)
class ClusteredCiResult:
    """A clustered-bootstrap confidence interval for a Brier-skill estimate.

    Attributes:
        point_estimate_ppm: The full-sample Brier skill, in ppm.
        ci_low_ppm: The lower confidence bound, in ppm (floored outward).
        ci_high_ppm: The upper confidence bound, in ppm (ceiled outward).
        ci_width: ``ci_high_ppm - ci_low_ppm``, in ppm.
        effective_n: The number of independent clusters resampled.
        replicates: The number of bootstrap replicates drawn.
        seed: The seed the generator was initialised with.
        confidence_ppm: The two-sided confidence level, in ppm.
        window: The observation window the metric was scored over.
    """

    point_estimate_ppm: int
    ci_low_ppm: int
    ci_high_ppm: int
    ci_width: int
    effective_n: int
    replicates: int
    seed: int
    confidence_ppm: int
    window: ObservationWindow


def validate_confidence_ppm(confidence_ppm: int) -> None:
    """Validate that ``confidence_ppm`` is a real ppm in the open unit interval.

    Args:
        confidence_ppm: The candidate confidence level, in ppm.

    Raises:
        ValueError: If ``confidence_ppm`` is a ``bool`` (an ``int`` subclass that
            must not masquerade as a level) or falls outside the open interval
            ``(0, 1_000_000)``; the message names ``confidence_ppm``.
    """
    if isinstance(confidence_ppm, bool):
        raise ValueError("confidence_ppm must be a non-bool int, got bool")
    if not 0 < confidence_ppm < _PPM_SCALE:
        raise ValueError(
            f"confidence_ppm must be within (0, {_PPM_SCALE}), got {confidence_ppm}"
        )


def _percentile_indices(replicates: int, confidence_ppm: int) -> tuple[int, int]:
    """Return the lower/upper order-statistic indices for a two-sided interval.

    Half the residual mass sits in each tail: ``alpha_half = (1e6 - conf) / 2``.
    The lower index floors ``replicates * alpha_half`` and the upper index is its
    mirror ``replicates - 1 - lower``.

    Args:
        replicates: The number of sorted replicates.
        confidence_ppm: The two-sided confidence level, in ppm.

    Returns:
        The ``(lower_index, upper_index)`` pair into the sorted replicates.
    """
    alpha_half_ppm = (_PPM_SCALE - confidence_ppm) // 2
    lower_index = divide(
        replicates * alpha_half_ppm,
        _PPM_SCALE,
        rounding=RoundingDirection.UNDERSTATE_EQUITY,
    )
    upper_index = replicates - 1 - lower_index
    return lower_index, upper_index


@dataclass(frozen=True, slots=True)
class _ClusterTerms:
    """The pooled Brier-term sums of one correlation cluster.

    Attributes:
        forecast_sum: Sum of the cluster's forecast Brier terms, in ppm^2.
        baseline_sum: Sum of the cluster's baseline Brier terms, in ppm^2.
    """

    forecast_sum: int
    baseline_sum: int


def _cluster_key(term: ForecastTerms) -> tuple[str, str]:
    """Return the cluster key for one forecast's terms.

    A correlation group id keys a shared cluster; an ungrouped market keys a
    singleton cluster. The two are tagged distinctly so a group id can never
    collide with a market ticker.

    Args:
        term: The forecast's Brier terms and cluster identity.

    Returns:
        A ``("group", id)`` or ``("market", ticker)`` cluster key.
    """
    if term.correlation_group_id is not None:
        return ("group", term.correlation_group_id)
    return ("market", term.market_ticker)


def _cluster_terms(terms: tuple[ForecastTerms, ...]) -> tuple[_ClusterTerms, ...]:
    """Pool per-forecast terms into per-cluster Brier-term sums, in cluster order.

    Args:
        terms: The per-forecast Brier terms and cluster identities.

    Returns:
        One :class:`_ClusterTerms` per distinct cluster, in first-appearance
        order (a deterministic, seed-independent ordering).
    """
    order: list[tuple[str, str]] = []
    forecast_sums: dict[tuple[str, str], int] = {}
    baseline_sums: dict[tuple[str, str], int] = {}
    for term in terms:
        key = _cluster_key(term)
        if key not in forecast_sums:
            order.append(key)
            forecast_sums[key] = 0
            baseline_sums[key] = 0
        forecast_sums[key] += term.forecast_term
        baseline_sums[key] += term.baseline_term
    return tuple(
        _ClusterTerms(forecast_sum=forecast_sums[key], baseline_sum=baseline_sums[key])
        for key in order
    )


def _replicate_skill(
    clusters: tuple[_ClusterTerms, ...], generator: SplitMix64
) -> Fraction:
    """Draw one resampled replicate and return its exact Brier skill.

    ``k`` clusters are drawn with replacement (``k`` == the cluster count), their
    Brier-term sums pooled, and the skill ``1 - forecast/baseline`` returned as an
    exact fraction.

    Args:
        clusters: The per-cluster Brier-term sums.
        generator: The seeded generator supplying the cluster draws.

    Returns:
        The replicate's Brier skill as a :class:`fractions.Fraction`.

    Raises:
        ValueError: If the resampled baseline-term sum is zero (skill undefined).
    """
    cluster_count = len(clusters)
    forecast_sum = 0
    baseline_sum = 0
    for _ in range(cluster_count):
        drawn = clusters[generator.below(cluster_count)]
        forecast_sum += drawn.forecast_sum
        baseline_sum += drawn.baseline_sum
    if baseline_sum == 0:
        raise ValueError("resampled baseline Brier-term sum is zero; skill undefined")
    return Fraction(baseline_sum - forecast_sum, baseline_sum)


@dataclass(frozen=True, slots=True)
class BootstrapSample:
    """The raw output of a clustered bootstrap run.

    Attributes:
        replicate_skills: One exact skill :class:`fractions.Fraction` per
            replicate, in draw order.
        cluster_count: The number of independent clusters (``effective_n``).
        resolved_count: The number of resolved forecasts scored.
    """

    replicate_skills: tuple[Fraction, ...]
    cluster_count: int
    resolved_count: int


def run_clustered_bootstrap(
    inputs: EvaluationInputs,
    *,
    seed: int,
    replicates: int,
    window: ObservationWindow,
) -> BootstrapSample:
    """Run the clustered bootstrap and return its raw replicate skills.

    Args:
        inputs: The evaluation inputs to resample.
        seed: The generator seed.
        replicates: The number of replicates to draw.
        window: The observation window to score over.

    Returns:
        The :class:`BootstrapSample` of replicate skills and cluster metadata.

    Raises:
        ValueError: If no forecast resolves.
    """
    terms = metrics.resolved_forecast_terms(inputs, window=window)
    clusters = _cluster_terms(terms)
    generator = SplitMix64(seed)
    replicate_skills = tuple(
        _replicate_skill(clusters, generator) for _ in range(replicates)
    )
    return BootstrapSample(
        replicate_skills=replicate_skills,
        cluster_count=len(clusters),
        resolved_count=len(terms),
    )


def _skill_to_ppm(skill: Fraction, *, rounding: RoundingDirection) -> int:
    """Reduce an exact skill fraction to a ppm ``int`` in the given direction.

    Args:
        skill: The exact skill fraction.
        rounding: The conservative rounding direction to apply.

    Returns:
        ``floor`` or ``ceil`` of ``skill * PPM_SCALE`` per ``rounding``.
    """
    return divide(skill.numerator * _PPM_SCALE, skill.denominator, rounding=rounding)


def brier_skill_ci(
    inputs: EvaluationInputs,
    *,
    confidence_ppm: int,
    seed: int,
    replicates: int = BOOTSTRAP_REPLICATES,
    window: ObservationWindow = ObservationWindow.LATEST_BEFORE_CLOSE,
) -> ClusteredCiResult:
    """Compute a clustered-bootstrap confidence interval for Brier skill.

    Args:
        inputs: The evaluation inputs to score and resample.
        confidence_ppm: The two-sided confidence level, in ppm (open interval).
        seed: The generator seed (identical seed + inputs -> identical result).
        replicates: The number of bootstrap replicates to draw.
        window: The observation window to score over.

    Returns:
        The :class:`ClusteredCiResult` for the estimate.

    Raises:
        ValueError: If ``confidence_ppm`` is a ``bool`` or outside ``(0, 1e6)``,
            or if no forecast resolves.
    """
    validate_confidence_ppm(confidence_ppm)
    point_estimate_ppm = metrics.brier_skill(inputs, window=window)
    sample = run_clustered_bootstrap(
        inputs, seed=seed, replicates=replicates, window=window
    )
    ordered = sorted(sample.replicate_skills)
    lower_index, upper_index = _percentile_indices(replicates, confidence_ppm)
    ci_low_ppm = _skill_to_ppm(
        ordered[lower_index], rounding=RoundingDirection.UNDERSTATE_EQUITY
    )
    ci_high_ppm = _skill_to_ppm(
        ordered[upper_index], rounding=RoundingDirection.OVERSTATE_COST
    )
    return ClusteredCiResult(
        point_estimate_ppm=point_estimate_ppm,
        ci_low_ppm=ci_low_ppm,
        ci_high_ppm=ci_high_ppm,
        ci_width=ci_high_ppm - ci_low_ppm,
        effective_n=sample.cluster_count,
        replicates=replicates,
        seed=seed,
        confidence_ppm=confidence_ppm,
        window=window,
    )
