"""Correlation-bucket tagging, override resolution, and aggregation (SPEC S9.9).

SPEC S9.9 groups markets that move together into *correlation buckets* so the
sizing stage can cap a fill against the exposure already carried across every
peer in the same bucket, not merely the single market. This module is the pure,
kernel-independent data model backing that cap:

    * :class:`CorrelationTag` -- a frozen, slotted ``(bucket_id, source,
      tagged_at)`` triple. A tag is either ``"llm"``-sourced (proposed by the
      forecasting ensemble) or ``"human"``-sourced (an operator override); its
      ``bucket_id`` must name one of the seven fixed seed-taxonomy buckets or a
      ``geopolitics-<region>`` id with a non-empty region suffix, validated at
      construction. ``tagged_at`` is provenance only -- it is never read by any
      arithmetic here, so a tag's instant cannot perturb a sizing decision.
    * :func:`effective_buckets` -- resolves a market's own tags into its
      *effective* bucket ids: any human tag present supersedes every LLM tag
      (an operator override wins), while the superseded LLM tags remain in the
      caller's input tuple for the ledger. The result is deduplicated and
      lexicographically sorted.
    * :class:`BucketExposureEntry` -- one peer market's exposure and its own
      tags, the unit :func:`aggregate_bucket_exposure` sums over.
    * :func:`aggregate_bucket_exposure` -- for a target's effective buckets,
      sums each peer's exposure into every bucket the peer's *own* effective
      buckets match, and returns the maximum such sum and the bucket id
      achieving it (lexicographically-smallest on ties), or ``(0, None)`` when
      the target has no effective buckets or no peer matches any of them.

Every value here is integer-only: no float, no bare ``/`` or ``//`` -- the whole
:mod:`hedgekit.selector` package is on ``scripts/lint_no_floats.py``'s denylist
(SPEC S6.1). This module imports only :mod:`hedgekit.numeric`, never
:mod:`hedgekit.selector.types`, so wiring it into ``SelectorInputs`` introduces
no import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from datetime import datetime

    from hedgekit.numeric import MoneyMicros

#: The provenance of a correlation tag: an ``"llm"`` ensemble proposal or a
#: ``"human"`` operator override. A human tag supersedes every LLM tag when a
#: market's effective buckets are resolved (see :func:`effective_buckets`).
TagSource: TypeAlias = Literal["llm", "human"]

#: The two permitted tag sources, for runtime validation of untrusted,
#: dynamically-arrived tagging payloads the type checker cannot vet.
_VALID_SOURCES: frozenset[str] = frozenset({"llm", "human"})

#: The seven fixed seed-taxonomy bucket ids.
BUCKET_US_ELECTION = "us-election"
BUCKET_FED_POLICY = "fed-policy"
BUCKET_INFLATION = "inflation"
BUCKET_WEATHER = "weather"
BUCKET_AI_REGULATION = "ai-regulation"
BUCKET_COMPANY_SPECIFIC = "company-specific"
BUCKET_LEGAL_CASE = "legal-case"

#: The prefix marking a region-parameterized geopolitics bucket. A valid
#: geopolitics id is this prefix followed by a non-empty region suffix (e.g.
#: ``"geopolitics-taiwan"``); the bare prefix alone is not a valid bucket id.
GEOPOLITICS_PREFIX = "geopolitics-"

#: The seven fixed seed-taxonomy bucket ids, as an immutable set for validation.
SEED_BUCKETS: frozenset[str] = frozenset(
    {
        BUCKET_US_ELECTION,
        BUCKET_FED_POLICY,
        BUCKET_INFLATION,
        BUCKET_WEATHER,
        BUCKET_AI_REGULATION,
        BUCKET_COMPANY_SPECIFIC,
        BUCKET_LEGAL_CASE,
    }
)


def _is_valid_bucket_id(bucket_id: str) -> bool:
    """Return whether ``bucket_id`` is a seed or region-parameterized bucket id.

    Args:
        bucket_id: The candidate bucket id.

    Returns:
        ``True`` when ``bucket_id`` is one of the seven seed ids, or the
        ``geopolitics-`` prefix followed by a non-empty region suffix; ``False``
        otherwise (including the bare prefix with an empty region).
    """
    if bucket_id in SEED_BUCKETS:
        return True
    if not bucket_id.startswith(GEOPOLITICS_PREFIX):
        return False
    region = bucket_id.removeprefix(GEOPOLITICS_PREFIX)
    return len(region) > 0


@dataclass(frozen=True, slots=True)
class CorrelationTag:
    """One correlation-bucket tag on a market, with its source and provenance.

    A frozen, slotted ``(bucket_id, source, tagged_at)`` triple validated at
    construction (SPEC S9.9): ``source`` must be ``"llm"`` or ``"human"``, and
    ``bucket_id`` must name a seed-taxonomy bucket or a ``geopolitics-<region>``
    id with a non-empty region. ``tagged_at`` records when the tag was applied
    for the ledger; it is never read by any bucket arithmetic, so it cannot
    perturb a sizing decision.

    Attributes:
        bucket_id: The correlation bucket this tag places the market in.
        source: Who proposed the tag -- ``"llm"`` (ensemble) or ``"human"``
            (operator override).
        tagged_at: When the tag was applied; provenance only, never arithmetic.
    """

    bucket_id: str
    source: TagSource
    tagged_at: datetime

    def __post_init__(self) -> None:
        """Validate the tag's source and bucket id (SPEC S9.9).

        Raises:
            ValueError: If ``source`` is not ``"llm"``/``"human"``, or
                ``bucket_id`` is neither a seed-taxonomy id nor a
                ``geopolitics-<region>`` id with a non-empty region.
        """
        if self.source not in _VALID_SOURCES:
            raise ValueError(
                f"source must be one of {sorted(_VALID_SOURCES)}, got {self.source!r}"
            )
        if not _is_valid_bucket_id(self.bucket_id):
            raise ValueError(
                f"bucket_id {self.bucket_id!r} is not a seed-taxonomy id nor a "
                f"'{GEOPOLITICS_PREFIX}<region>' id with a non-empty region"
            )


def effective_buckets(tags: tuple[CorrelationTag, ...]) -> tuple[str, ...]:
    """Resolve a market's tags into its effective bucket ids (SPEC S9.9).

    Any human tag present supersedes every LLM tag -- an operator override wins,
    and the effective buckets are the human tags' ids only. When no human tag is
    present, the LLM tags' ids stand. The superseded LLM tags are never removed
    from the ``tags`` input (the caller retains the full tagging ledger); they
    are only excluded from this resolved result. The returned ids are
    deduplicated and lexicographically sorted; an empty input resolves to ``()``.

    Args:
        tags: The market's own correlation tags, in any order.

    Returns:
        The effective bucket ids, deduplicated and lexicographically sorted.
    """
    human_tags = tuple(tag for tag in tags if tag.source == "human")
    chosen = human_tags if human_tags else tags
    return tuple(sorted({tag.bucket_id for tag in chosen}))


@dataclass(frozen=True, slots=True)
class BucketExposureEntry:
    """One peer market's exposure and correlation tags for bucket aggregation.

    The unit :func:`aggregate_bucket_exposure` sums over: a peer contributes its
    ``exposure_micros`` to each of its own effective buckets (SPEC S9.9).

    Attributes:
        market_ticker: The peer market's exchange ticker, for traceability.
        exposure_micros: The peer's current exposure, in micros.
        tags: The peer's own correlation tags, resolved via
            :func:`effective_buckets` when matching against a target's buckets.
    """

    market_ticker: str
    exposure_micros: MoneyMicros
    tags: tuple[CorrelationTag, ...]


def aggregate_bucket_exposure(
    target_buckets: tuple[str, ...],
    peers: tuple[BucketExposureEntry, ...],
) -> tuple[int, str | None]:
    """Return the max per-bucket peer exposure and the bucket achieving it (S9.9).

    For each of the target's effective buckets, sums the exposure of every peer
    whose *own* effective buckets contain that bucket id, then returns the
    largest such sum and the bucket id that achieved it. Ties are broken toward
    the lexicographically-smallest bucket id. When the target has no effective
    buckets, or no peer matches any of them, returns ``(0, None)``.

    Args:
        target_buckets: The target market's effective bucket ids.
        peers: The peer markets' exposure entries.

    Returns:
        A ``(max_sum, bucket_id)`` pair; ``(0, None)`` when nothing matched.
    """
    best_total = 0
    best_bucket: str | None = None
    for bucket_id in sorted(target_buckets):
        total = sum(
            peer.exposure_micros.value
            for peer in peers
            if bucket_id in effective_buckets(peer.tags)
        )
        if total > best_total:
            best_total = total
            best_bucket = bucket_id
    return best_total, best_bucket
