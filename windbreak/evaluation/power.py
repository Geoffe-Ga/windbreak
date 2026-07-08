"""Power analysis for the clustered Brier-skill bootstrap (#51; SPEC S13.5).

Given a fixture, this module answers "how large an edge could we even detect?"
It runs the clustered bootstrap, reads the standard error of the replicate skill
distribution, rescales that error to a target sample size while respecting the
cluster design effect, and reports the minimum detectable Brier skill at the
target power.

Every value stays on the integer/exact-:class:`fractions.Fraction` path: the
standard errors are integer square roots (:func:`math.isqrt`, which is exact for
integers) of exact rational variances, and the final effect size is reduced
through the sanctioned :func:`windbreak.numeric.rounding.divide`. No float
appears anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import isqrt
from typing import TYPE_CHECKING

from windbreak.evaluation.bootstrap import (
    BOOTSTRAP_REPLICATES,
    run_clustered_bootstrap,
    validate_confidence_ppm,
)
from windbreak.evaluation.windows import ObservationWindow
from windbreak.numeric.rounding import RoundingDirection, divide

if TYPE_CHECKING:
    from windbreak.evaluation.registry import EvaluationInputs

#: The documented default target sample size for the power analysis.
POWER_TARGET_N = 300
#: The documented default target power, in ppm (80% power).
POWER_TARGET_PPM = 800_000
#: The default two-sided confidence level of the underlying interval, in ppm.
_DEFAULT_CONFIDENCE_PPM = 950_000
#: One whole probability in ppm; the ppm-scaling factor for skill values.
_PPM_SCALE = 1_000_000

#: The standard-normal 97.5th-percentile quantile (``z_{0.975} ~= 1.959964``),
#: scaled to ppm. Two-sided 95% confidence uses this critical value.
Z_975_PPM = 1_959_964
#: The standard-normal 80th-percentile quantile (``z_{0.80} ~= 0.841621``),
#: scaled to ppm. 80% power uses this critical value.
Z_80_PPM = 841_621


@dataclass(frozen=True, slots=True)
class PowerAnalysis:
    """The power-analysis result for a clustered Brier-skill bootstrap.

    Attributes:
        min_detectable_brier_skill_ppm: The minimum Brier skill detectable at the
            target sample size and power, in ppm.
        se_observed_ppm: The observed replicate standard error, in ppm.
        se_target_ppm: The design-effect-adjusted standard error at
            ``target_n``, in ppm.
        effective_n: The number of independent clusters resampled.
        resolved_n: The number of resolved forecasts scored.
        target_n: The target sample size the effect size is projected to.
        confidence_ppm: The two-sided confidence level, in ppm.
        power_ppm: The target power, in ppm.
        replicates: The number of bootstrap replicates drawn.
        seed: The seed the bootstrap was initialised with.
        window: The observation window scored over.
    """

    min_detectable_brier_skill_ppm: int
    se_observed_ppm: int
    se_target_ppm: int
    effective_n: int
    resolved_n: int
    target_n: int
    confidence_ppm: int
    power_ppm: int
    replicates: int
    seed: int
    window: ObservationWindow

    def render_text(self) -> str:
        """Render the power analysis as a blunt, auditable plain-text block.

        The block is code-generated from the fields (never prose) and carries
        both the minimum detectable effect and the seed, so a reader can audit
        reproducibility without re-running the analysis.

        Returns:
            The rendered ``== power ==`` section.
        """
        mde = self.min_detectable_brier_skill_ppm
        return "\n".join(
            (
                "== power ==",
                f"min_detectable_brier_skill_ppm = {mde}",
                f"seed = {self.seed}",
                f"target_n = {self.target_n}",
                f"effective_n = {self.effective_n}",
                f"resolved_n = {self.resolved_n}",
                f"confidence_ppm = {self.confidence_ppm}",
                f"power_ppm = {self.power_ppm}",
                f"se_observed_ppm = {self.se_observed_ppm}",
                f"se_target_ppm = {self.se_target_ppm}",
            )
        )


def _ceil_sqrt_fraction(value: Fraction) -> int:
    """Return the least integer whose square is at least ``value``.

    Uses :func:`math.isqrt` (exact integer square root) on the fraction's
    numerator/denominator and bumps upward until ``root^2 >= value`` exactly.

    Args:
        value: A non-negative rational.

    Returns:
        ``ceil(sqrt(value))`` as an ``int`` (``0`` for a non-positive input).
    """
    numerator = value.numerator
    denominator = value.denominator
    if numerator <= 0:
        return 0
    root = isqrt(numerator // denominator)
    while root * root * denominator < numerator:
        root += 1
    return root


def _replicate_variance_ppm2(skills: tuple[Fraction, ...]) -> Fraction:
    """Return the exact variance of the replicate skills, in ppm-squared.

    Args:
        skills: The replicate skill fractions.

    Returns:
        ``(count * sum(v^2) - sum(v)^2) / count^2`` where ``v`` is each skill in
        ppm -- the population variance of the replicate distribution, exact.
    """
    count = len(skills)
    values = [skill * _PPM_SCALE for skill in skills]
    total = sum(values, Fraction(0))
    square_total = sum((value * value for value in values), Fraction(0))
    # Multiply by the exact reciprocal rather than dividing: `/` true-division
    # is banned on the value path, and `Fraction(1, n)` keeps this exact.
    return (count * square_total - total * total) * Fraction(1, count * count)


def _design_effect_se_ppm(
    se_observed_ppm: int, *, resolved_n: int, target_n: int
) -> int:
    """Rescale the observed standard error to ``target_n``, in ppm.

    The cluster design effect makes the standard error scale with the cluster
    count rather than the raw sample count; holding the cluster ratio fixed, the
    target-``n`` error is ``se_observed * sqrt(resolved_n / target_n)``, rounded
    up so the projected error is never understated.

    Args:
        se_observed_ppm: The observed standard error, in ppm.
        resolved_n: The number of resolved forecasts scored.
        target_n: The target sample size.

    Returns:
        The design-effect-adjusted standard error at ``target_n``, in ppm.
    """
    # `resolved_n` (not the cluster count) is the deliberate base here: holding
    # the cluster ratio fixed, the projected cluster count scales as
    # `k_target = target_n * k / resolved_n`, so the design-effect ratio
    # `k / k_target` collapses exactly to `resolved_n / target_n`. Do not
    # "correct" this to `cluster_count` -- that would double-count the effect.
    return _ceil_sqrt_fraction(
        Fraction(se_observed_ppm * se_observed_ppm * resolved_n, target_n)
    )


def power_analysis(
    inputs: EvaluationInputs,
    *,
    seed: int,
    target_n: int = POWER_TARGET_N,
    confidence_ppm: int = _DEFAULT_CONFIDENCE_PPM,
    power_ppm: int = POWER_TARGET_PPM,
    window: ObservationWindow = ObservationWindow.LATEST_BEFORE_CLOSE,
) -> PowerAnalysis:
    """Run a clustered-bootstrap power analysis for the Brier-skill metric.

    Args:
        inputs: The evaluation inputs to analyse.
        seed: The bootstrap generator seed.
        target_n: The target sample size to project the effect size to.
        confidence_ppm: The two-sided confidence level, in ppm (open interval).
        power_ppm: The target power, in ppm.
        window: The observation window to score over.

    Returns:
        The :class:`PowerAnalysis` result.

    Raises:
        ValueError: If ``confidence_ppm`` is a ``bool`` or outside ``(0, 1e6)``,
            or if no forecast resolves.
    """
    validate_confidence_ppm(confidence_ppm)
    sample = run_clustered_bootstrap(
        inputs, seed=seed, replicates=BOOTSTRAP_REPLICATES, window=window
    )
    se_observed_ppm = _ceil_sqrt_fraction(
        _replicate_variance_ppm2(sample.replicate_skills)
    )
    se_target_ppm = _design_effect_se_ppm(
        se_observed_ppm, resolved_n=sample.resolved_count, target_n=target_n
    )
    min_detectable_brier_skill_ppm = divide(
        (Z_975_PPM + Z_80_PPM) * se_target_ppm,
        _PPM_SCALE,
        rounding=RoundingDirection.OVERSTATE_COST,
    )
    return PowerAnalysis(
        min_detectable_brier_skill_ppm=min_detectable_brier_skill_ppm,
        se_observed_ppm=se_observed_ppm,
        se_target_ppm=se_target_ppm,
        effective_n=sample.cluster_count,
        resolved_n=sample.resolved_count,
        target_n=target_n,
        confidence_ppm=confidence_ppm,
        power_ppm=power_ppm,
        replicates=BOOTSTRAP_REPLICATES,
        seed=seed,
        window=window,
    )
