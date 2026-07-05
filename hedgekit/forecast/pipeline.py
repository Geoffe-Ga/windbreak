"""SPEC S8.2 forecast pipeline wiring (offline, deterministic stub stages).

This module implements the twelve pipeline stages of SPEC S8.2 as discrete,
typed functions and threads them together in :func:`run_pipeline`. The spec's
S8.2 diagram (``plans/SPEC_v3.md:292-301``) reads as an eleven-arrow chain but
carries *twelve* arrow segments once the terminal
"schema-validated ForecastRecord" step is counted; the twelve stage functions
below map one-to-one onto those segments.

The stage bodies are deterministic, network-free stubs (identity and
fixture-derived logic) with real control flow -- the milestone is a correct,
byte-deterministic wiring skeleton, not modeled forecasting. Determinism is
load-bearing: ``created_at`` is injected (never ``datetime.now()``), all math
is integer-only, and :func:`collect_model_votes` is the sole stage that
touches the :class:`~hedgekit.forecast.cassettes.LlmTransport` seam, so wiring
a :class:`~hedgekit.forecast.cassettes.ForbiddenLiveTransport` (or an empty
replay cassette) fails the run closed rather than silently succeeding.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, NamedTuple

from hedgekit.forecast.cassettes import LlmRequest
from hedgekit.forecast.citations import (
    content_hash_of,
    count_verified,
    verify_citations,
)
from hedgekit.forecast.ensemble import aggregate_votes
from hedgekit.forecast.records import (
    Citation,
    ForecastRecord,
    ModelVote,
    is_live_eligible,
)

if TYPE_CHECKING:
    from hedgekit.connector.models import NormalizedMarket
    from hedgekit.forecast.cassettes import LlmTransport
    from hedgekit.forecast.citations import CitationVerdict
    from hedgekit.forecast.ensemble import VoteAggregate
    from hedgekit.forecast.records import BaselineQuoteSnapshot
    from hedgekit.forecast.sandbox import ResearchTools

#: One full probability (1.0) expressed in parts-per-million; also the clamp
#: ceiling and the shrinkage denominator.
_PPM_SCALE = 1_000_000

#: Lowest legal ppm probability (the clamp floor).
_MIN_PPM = 0

#: Exact pips-to-ppm factor: pips are 1e-4 and ppm 1e-6, so a binary market
#: price in pips maps to a probability in ppm by multiplying by 100.
_PIPS_TO_PPM = 100

#: Fixed per-member offset (in ppm) spreading the three votes around the
#: baseline so their median is meaningful.
_VOTE_DELTA_PPM = 10_000

#: The three ensemble vote offsets, low to high, keeping the median centered.
_VOTE_OFFSETS_PPM: tuple[int, int, int] = (
    -_VOTE_DELTA_PPM,
    0,
    _VOTE_DELTA_PPM,
)

#: Fixed shrinkage weight toward the market baseline (SPEC S16
#: ``shrink_to_market_lambda_ppm`` default), applied as integer math.
_SHRINK_LAMBDA_PPM = 250_000

#: Deterministic subquestion prefixes for the decomposition stage.
_SUBQUESTION_PREFIXES: tuple[str, ...] = (
    "Base rate for",
    "Recent evidence on",
    "Counter-signal for",
)

#: Fixed publication date stamped on every fixture-derived citation (no
#: network fetch, so no real date is available).
_CITATION_PUBLICATION_DATE = datetime(2024, 1, 1, tzinfo=UTC)

#: Deterministic stub research cost for a full-pipeline run, in micros.
_RESEARCH_COST_MICROS = 3_000_000

#: Hours per day and seconds per hour, for the integer horizon computation.
_HOURS_PER_DAY = 24
_SECONDS_PER_HOUR = 3_600

#: Default minimum independently-verified citations a full record needs to be
#: live-eligible (SPEC S16 ``ForecastConfig.min_verified_citations`` default).
DEFAULT_MIN_VERIFIED_CITATIONS: Final = 3

#: Abstention reason stamped when a full run gathers zero verified citations.
ABSTENTION_NO_VERIFIED_CITATIONS: Final = "no_verified_citations"

#: Maximum words retained in a citation's quoted excerpt (SPEC S8.5 quote
#: length cap).
_MAX_QUOTE_WORDS: Final = 25

#: Deterministic rationale stamped on every zero-verified-citation abstention
#: record (mirrors ``triage._TRIAGE_RATIONALE_MD``).
_ABSTENTION_RATIONALE_MD = (
    "## Abstained forecast\n\n"
    "The full pipeline ran, but no gathered citation could be independently "
    "verified, so the engine abstained. This record is live-ineligible.\n"
)


class _EnsembleMember(NamedTuple):
    """One pinned ensemble member's provenance strings.

    Attributes:
        provider: The LLM provider identifier.
        model_version: The pinned model version string.
        training_cutoff: The model's declared training cutoff.
    """

    provider: str
    model_version: str
    training_cutoff: str


#: The three pinned ensemble members that cast votes, in call order.
_VOTE_MODELS: tuple[_EnsembleMember, _EnsembleMember, _EnsembleMember] = (
    _EnsembleMember("openai", "gpt-5-forecast", "2024-06-01"),
    _EnsembleMember("anthropic", "claude-forecast", "2024-04-01"),
    _EnsembleMember("openai", "gpt-5-forecast-mini", "2024-06-01"),
)


def _clamp_ppm(value: int) -> int:
    """Clamp an integer into the legal ppm domain ``[0, 1_000_000]``.

    Args:
        value: The candidate ppm value.

    Returns:
        ``value`` clamped into ``[0, 1_000_000]``.
    """
    return max(_MIN_PPM, min(value, _PPM_SCALE))


def _clamp_between(value: int, low: int, high: int) -> int:
    """Clamp ``value`` into the inclusive ``[low, high]`` interval.

    Args:
        value: The candidate value.
        low: The interval floor.
        high: The interval ceiling.

    Returns:
        ``value`` clamped into ``[low, high]``.
    """
    return max(low, min(value, high))


def _fingerprint(text: str) -> str:
    """Return a sha256 hex fingerprint of a response's text.

    Args:
        text: The response text to fingerprint.

    Returns:
        A lowercase, 64-character sha256 hex digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso_z(moment: datetime) -> str:
    """Render a datetime as ISO-8601 UTC with a trailing ``Z``.

    Args:
        moment: The (timezone-aware) datetime to render; normalized to UTC.

    Returns:
        A string like ``2024-12-10T12:00:00.000000Z``.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _baseline_probability_ppm(baseline: BaselineQuoteSnapshot) -> int:
    """Convert a baseline pip price into a clamped ppm probability.

    Args:
        baseline: The baseline quote snapshot.

    Returns:
        The baseline price as a probability in ppm.
    """
    return _clamp_ppm(baseline.price_pips * _PIPS_TO_PPM)


def _vote_prompt(
    market: NormalizedMarket, baseline: BaselineQuoteSnapshot, index: int
) -> str:
    """Build the deterministic prompt for one ensemble vote.

    Args:
        market: The market under forecast.
        baseline: The baseline quote snapshot.
        index: The zero-based vote index.

    Returns:
        A deterministic prompt string.
    """
    return (
        f"Estimate the resolution probability for {market.ticker} "
        f"({market.title}); baseline {baseline.price_pips} pips; vote {index}."
    )


# --- SPEC S8.2 stages (twelve discrete steps) ------------------------------------


def normalize_question(market: NormalizedMarket) -> str:
    """Stage 1: normalize the question into a stable content hash.

    Args:
        market: The market under forecast.

    Returns:
        A sha256 hex hash of the title plus resolution criteria.
    """
    digest_source = market.title + market.resolution_criteria
    return hashlib.sha256(digest_source.encode("utf-8")).hexdigest()


def extract_resolution_criteria(market: NormalizedMarket) -> str:
    """Stage 2: extract the market's resolution criteria (identity stub).

    Args:
        market: The market under forecast.

    Returns:
        The market's resolution-criteria prose.
    """
    return market.resolution_criteria


def outside_view_base_rate(baseline: BaselineQuoteSnapshot) -> int:
    """Stage 3: take the outside-view base rate from the baseline price.

    Args:
        baseline: The baseline quote snapshot.

    Returns:
        The baseline price as a ppm probability.
    """
    return _baseline_probability_ppm(baseline)


def decompose_subquestions(market: NormalizedMarket) -> tuple[str, ...]:
    """Stage 4: decompose the question into deterministic subquestions.

    Args:
        market: The market under forecast.

    Returns:
        One subquestion per configured prefix.
    """
    return tuple(f"{prefix} {market.title}" for prefix in _SUBQUESTION_PREFIXES)


def bounded_web_research(
    subquestions: tuple[str, ...],
    *,
    tools: ResearchTools,
) -> tuple[Citation, ...]:
    """Stage 5: gather citations through the sandboxed research tools.

    Each subquestion is searched for a candidate URL, whose content is fetched
    through the egress-allowlisted :meth:`ResearchTools.fetch` -- so a search
    result off the allowlist raises :class:`EgressDeniedError` here, on the pipeline
    path itself (a policy violation fails the run closed, never silently). An
    *unreachable* source (the transport raising an ``OSError`` such as
    ``ConnectionError``) is not a policy violation but a dead link, so it is
    skipped: it simply contributes no citation, letting the downstream
    verification stage abstain on an evidence-free run. Each citation's
    ``content_hash`` and ``quoted_text`` are derived from that one fetched
    content (the excerpt is its first ``_MAX_QUOTE_WORDS`` words, SPEC S8.5), so
    a citation self-verifies against a stable refetch of the same URL. That
    self-verification assumes whitespace-normalized content: the excerpt is
    space-joined, while :func:`hedgekit.forecast.citations.verify_citation`'s
    quote check is a raw substring test, so a live fetch returning runs of
    whitespace or newlines would need normalizing before the substring check
    (the current deterministic-stub sources are single-spaced, so this holds
    today). The resulting citations are deterministic (the fixture transports
    derive URL and content from their inputs) and integer-free.

    Args:
        subquestions: The decomposed subquestions to research.
        tools: The sandboxed research tools (search/fetch capabilities).

    Returns:
        One deterministic citation per reachable subquestion candidate URL.
    """
    citations: list[Citation] = []
    for subquestion in subquestions:
        urls = tools.search(subquestion)
        if not urls:
            continue
        url = urls[0]
        try:
            content = tools.fetch(url)
        except OSError:
            continue
        citations.append(
            Citation(
                url=url,
                content_hash=content_hash_of(content),
                quoted_text=" ".join(content.split()[:_MAX_QUOTE_WORDS]),
                publication_date=_CITATION_PUBLICATION_DATE,
                source_type="research_note",
            )
        )
    return tuple(citations)


def _source_note(verdict: CitationVerdict) -> str:
    """Render one truthful source-quality note from a citation verdict.

    Args:
        verdict: The citation's verification verdict.

    Returns:
        A note naming the source type and URL, prefixed ``verified`` when the
        citation verified or ``unverified (<failure>)`` when it did not.
    """
    citation = verdict.citation
    if verdict.verified:
        return f"verified {citation.source_type} at {citation.url}"
    return f"unverified ({verdict.failure}) {citation.source_type} at {citation.url}"


def assess_source_reliability(verdicts: tuple[CitationVerdict, ...]) -> tuple[str, ...]:
    """Stage 6: note the verified reliability of each gathered source.

    The note is truthful about verification: a verdict that failed to verify is
    labelled ``unverified`` with its failure code, never silently reported as
    verified.

    Args:
        verdicts: The per-citation verification verdicts.

    Returns:
        One source-quality note per verdict.
    """
    return tuple(_source_note(verdict) for verdict in verdicts)


def adversarial_counterargument(subquestions: tuple[str, ...]) -> str:
    """Stage 7: form the adversarial counterargument pass.

    Args:
        subquestions: The decomposed subquestions.

    Returns:
        A deterministic counterargument summary.
    """
    return "; ".join(f"counterpoint on {subquestion}" for subquestion in subquestions)


def collect_model_votes(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    *,
    transport: LlmTransport,
) -> tuple[ModelVote, ...]:
    """Stage 8: collect three independent, structured model votes.

    This is the only stage that touches the transport seam: it issues exactly
    three deterministic requests (identical across runs) and turns each
    response into a :class:`ModelVote`. Vote probabilities are derived from the
    baseline (not the response text) so they stay deterministic, while each
    response's fingerprint records provider drift (T14).

    Args:
        market: The market under forecast.
        baseline: The baseline quote snapshot.
        transport: The LLM transport (recording, replay, or forbidden-live).

    Returns:
        The three ensemble votes, in call order.
    """
    base_ppm = _baseline_probability_ppm(baseline)
    votes: list[ModelVote] = []
    for index, (member, offset) in enumerate(
        zip(_VOTE_MODELS, _VOTE_OFFSETS_PPM, strict=True)
    ):
        request = LlmRequest(
            provider=member.provider,
            model_version=member.model_version,
            prompt=_vote_prompt(market, baseline, index),
        )
        response = transport.complete(request)
        votes.append(
            ModelVote(
                provider=member.provider,
                model_version=member.model_version,
                declared_training_cutoff=member.training_cutoff,
                probability_ppm=_clamp_ppm(base_ppm + offset),
                response_fingerprint=_fingerprint(response),
            )
        )
    return tuple(votes)


def aggregate_median(votes: tuple[ModelVote, ...]) -> VoteAggregate:
    """Stage 9: aggregate votes into a median with confidence bounds (S8.6).

    Delegates to :func:`hedgekit.forecast.ensemble.aggregate_votes`, which owns
    the integer-median and exclusive-median IQR math; ``vote_dispersion_ppm``
    is now the inter-quartile spread rather than the raw max-minus-min range.

    Args:
        votes: The ensemble votes to aggregate.

    Returns:
        The median probability with low/high bounds and IQR dispersion.
    """
    return aggregate_votes(votes)


def normalize_coherence(market: NormalizedMarket) -> int | None:
    """Stage 10: normalize probability coherence within a group (S8.7 stub).

    A single-market v1 forecast has no mutually-exclusive peer group, so there
    is no cross-outcome probability sum to normalize; the group sum is
    therefore undefined. Structural group-coherence is post-v1 (SPEC S8.7).

    Args:
        market: The market under forecast (its group membership determines
            whether coherence applies; v1 markets always stand alone).

    Returns:
        Always ``None`` in v1: there is no group sum to report.
    """
    return None


def apply_calibration_map(probability_ppm: int) -> int:
    """Stage 11: apply the versioned calibration map (v0 identity map).

    No resolved forecasts exist yet to fit a correction, so the v0 calibration
    map is the identity map; the value is returned unchanged after a defensive
    clamp back into the ppm domain.

    Args:
        probability_ppm: The aggregated probability, in ppm.

    Returns:
        The calibrated probability, in ppm.
    """
    return _clamp_ppm(probability_ppm)


def shrink_toward_baseline(probability_ppm: int, baseline_ppm: int) -> int:
    """Stage 12: shrink the estimate toward the market baseline (integer λ).

    Blends the estimate with the baseline at a fixed weight using integer
    math only (floor division), so no float ever enters the probability path.

    Args:
        probability_ppm: The calibrated probability, in ppm.
        baseline_ppm: The market baseline probability, in ppm.

    Returns:
        The shrunk probability, in ppm.
    """
    blended = (
        probability_ppm * (_PPM_SCALE - _SHRINK_LAMBDA_PPM)
        + baseline_ppm * _SHRINK_LAMBDA_PPM
    ) // _PPM_SCALE
    return _clamp_ppm(blended)


def _build_rationale(criteria: str, counterpoints: str) -> str:
    """Assemble the deterministic rationale markdown.

    Args:
        criteria: The extracted resolution criteria.
        counterpoints: The adversarial counterargument summary.

    Returns:
        A markdown rationale string.
    """
    return (
        "## Rationale\n\n"
        f"Resolution criteria: {criteria}\n\n"
        f"Counterarguments considered: {counterpoints}\n"
    )


def _forecast_id(question_hash: str, snapshot_id: str, created_at: datetime) -> str:
    """Derive a deterministic forecast id from its provenance fields.

    Args:
        question_hash: The normalized-question hash.
        snapshot_id: The baseline snapshot identifier.
        created_at: The forecast creation instant.

    Returns:
        A sha256 hex digest over the canonical JSON of the three inputs.
    """
    canonical = json.dumps(
        [question_hash, snapshot_id, _iso_z(created_at)],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _forecast_horizon_hours(market: NormalizedMarket, created_at: datetime) -> int:
    """Compute whole hours from creation to the market's close (integer math).

    Args:
        market: The market under forecast.
        created_at: The forecast creation instant.

    Returns:
        The horizon in whole hours.
    """
    delta = market.close_time - created_at
    return delta.days * _HOURS_PER_DAY + delta.seconds // _SECONDS_PER_HOUR


def build_forecast_record(
    *,
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    question_hash: str,
    probability_ppm: int,
    aggregate: VoteAggregate,
    votes: tuple[ModelVote, ...],
    citations: tuple[Citation, ...],
    source_notes: tuple[str, ...],
    rationale: str,
    coherence_sum: int | None,
    eligible_for_live: bool = True,
) -> ForecastRecord:
    """Assemble and validate the final :class:`ForecastRecord`.

    Args:
        market: The market under forecast.
        baseline: The baseline quote snapshot.
        created_at: The forecast creation instant.
        question_hash: The normalized-question hash.
        probability_ppm: The final, bound-clamped probability, in ppm.
        aggregate: The median vote aggregation (bounds and dispersion).
        votes: The ensemble votes.
        citations: The verified supporting citations.
        source_notes: The source-quality notes.
        rationale: The rationale markdown.
        coherence_sum: The coherence group sum, or None.
        eligible_for_live: Whether the record may back a live order; defaults to
            ``True`` and is overridden by the caller's live-eligibility gate.

    Returns:
        A schema-valid, immutable forecast record.
    """
    return ForecastRecord(
        forecast_id=_forecast_id(question_hash, baseline.snapshot_id, created_at),
        market_ticker=market.ticker,
        normalized_question_hash=question_hash,
        probability_ppm=probability_ppm,
        ci_low_ppm=aggregate.ci_low_ppm,
        ci_high_ppm=aggregate.ci_high_ppm,
        model_votes=votes,
        vote_dispersion_ppm=aggregate.vote_dispersion_ppm,
        rationale_markdown=rationale,
        citations=citations,
        source_quality_notes=source_notes,
        research_cost_micros=_RESEARCH_COST_MICROS,
        triage_stage="full",
        created_at=created_at,
        forecast_horizon_hours=_forecast_horizon_hours(market, created_at),
        market_price_baseline_pips=baseline.price_pips,
        baseline_quote_snapshot_id=baseline.snapshot_id,
        coherence_group_sum_ppm=coherence_sum,
        coherence_flag=coherence_sum is not None,
        abstention_reason=None,
        eligible_for_live=eligible_for_live,
    )


def _build_abstention_record(
    *,
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    question_hash: str,
    citations: tuple[Citation, ...],
    abstention_reason: str,
) -> ForecastRecord:
    """Assemble a schema-valid abstention record (mirrors the triage-only one).

    The baseline collapses the point estimate and both confidence bounds onto
    the same ppm value (no ensemble ran), the gathered citations are retained
    for audit, and the record is permanently live-ineligible with its
    abstention reason set.

    Args:
        market: The market under forecast.
        baseline: The baseline quote snapshot.
        created_at: The forecast creation instant.
        question_hash: The normalized-question hash.
        citations: The gathered citations, retained for audit.
        abstention_reason: Why the engine abstained.

    Returns:
        A schema-valid, immutable abstention forecast record.
    """
    baseline_ppm = _baseline_probability_ppm(baseline)
    return ForecastRecord(
        forecast_id=_forecast_id(question_hash, baseline.snapshot_id, created_at),
        market_ticker=market.ticker,
        normalized_question_hash=question_hash,
        probability_ppm=baseline_ppm,
        ci_low_ppm=baseline_ppm,
        ci_high_ppm=baseline_ppm,
        model_votes=(),
        vote_dispersion_ppm=0,
        rationale_markdown=_ABSTENTION_RATIONALE_MD,
        citations=citations,
        source_quality_notes=(),
        research_cost_micros=_RESEARCH_COST_MICROS,
        triage_stage="full",
        created_at=created_at,
        forecast_horizon_hours=_forecast_horizon_hours(market, created_at),
        market_price_baseline_pips=baseline.price_pips,
        baseline_quote_snapshot_id=baseline.snapshot_id,
        coherence_group_sum_ppm=None,
        coherence_flag=False,
        abstention_reason=abstention_reason,
        eligible_for_live=False,
    )


def run_pipeline(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    *,
    transport: LlmTransport,
    created_at: datetime,
    research_tools: ResearchTools,
    min_verified_citations: int = DEFAULT_MIN_VERIFIED_CITATIONS,
) -> ForecastRecord:
    """Run the twelve-stage pipeline into a schema-valid forecast record.

    All stages run offline and deterministically; only
    :func:`collect_model_votes` touches ``transport``, so wiring a forbidden
    or empty transport fails the run closed rather than reaching a network.
    Stage 5 reaches the web only through ``research_tools``, whose egress
    allowlist can make the run raise :class:`EgressDeniedError` before any vote.
    Each gathered citation is then independently re-verified (SPEC S8.8); when
    *zero* verify, the run abstains -- returning a live-ineligible abstention
    record *before* the vote stage, so ``transport`` is never touched. Otherwise
    live eligibility is gated on the verified count meeting
    ``min_verified_citations``. Given identical inputs and ``created_at``, two
    runs produce equal records and byte-identical payloads.

    Args:
        market: The market under forecast.
        baseline: The baseline quote snapshot the forecast is struck against.
        transport: The LLM transport for the vote stage (keyword-only).
        created_at: The injected creation instant, for determinism
            (keyword-only; never ``datetime.now()``).
        research_tools: The sandboxed research tools threaded into stage 5's
            bounded web research (keyword-only).
        min_verified_citations: The minimum independently-verified citations a
            record needs to be live-eligible (keyword-only). Abstention on zero
            verified citations takes absolute precedence over this knob.

    Returns:
        The produced, immutable forecast record.
    """
    question_hash = normalize_question(market)
    criteria = extract_resolution_criteria(market)
    base_rate_ppm = outside_view_base_rate(baseline)
    subquestions = decompose_subquestions(market)
    citations = bounded_web_research(subquestions, tools=research_tools)
    verdicts = verify_citations(research_tools, citations, as_of=created_at)
    verified_count = count_verified(verdicts)
    if verified_count == 0:
        return _build_abstention_record(
            market=market,
            baseline=baseline,
            created_at=created_at,
            question_hash=question_hash,
            citations=citations,
            abstention_reason=ABSTENTION_NO_VERIFIED_CITATIONS,
        )
    source_notes = assess_source_reliability(verdicts)
    counterpoints = adversarial_counterargument(subquestions)
    votes = collect_model_votes(market, baseline, transport=transport)
    aggregate = aggregate_median(votes)
    coherence_sum = normalize_coherence(market)
    calibrated_ppm = apply_calibration_map(aggregate.probability_ppm)
    shrunk_ppm = shrink_toward_baseline(calibrated_ppm, base_rate_ppm)
    probability_ppm = _clamp_between(
        shrunk_ppm, aggregate.ci_low_ppm, aggregate.ci_high_ppm
    )
    rationale = _build_rationale(criteria, counterpoints)
    return build_forecast_record(
        market=market,
        baseline=baseline,
        created_at=created_at,
        question_hash=question_hash,
        probability_ppm=probability_ppm,
        aggregate=aggregate,
        votes=votes,
        citations=citations,
        source_notes=source_notes,
        rationale=rationale,
        coherence_sum=coherence_sum,
        eligible_for_live=is_live_eligible(
            verified_citation_count=verified_count,
            min_verified_citations=min_verified_citations,
            triage_stage="full",
            coherence_flag=coherence_sum is not None,
            abstention_reason=None,
        ),
    )
