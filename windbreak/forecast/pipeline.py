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
touches the :class:`~windbreak.forecast.cassettes.LlmTransport` seam, so wiring
a :class:`~windbreak.forecast.cassettes.ForbiddenLiveTransport` (or an empty
replay cassette) fails the run closed rather than silently succeeding.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from windbreak.forecast.citations import (
    content_hash_of,
    count_verified,
    verify_citations,
)
from windbreak.forecast.ensemble import aggregate_votes
from windbreak.forecast.providers import (
    DEFAULT_VOTE_ENSEMBLE,
    FixtureVoteProvider,
    ProviderResponseRejectedError,
)
from windbreak.forecast.records import (
    Citation,
    ForecastRecord,
    ModelVote,
    is_live_eligible,
)
from windbreak.forecast.sanitize import (
    MAX_QUOTE_WORDS,
    ResearchQuote,
    extract_quote,
    sanitize_content,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from windbreak.connector.models import NormalizedMarket
    from windbreak.forecast.budget import ResearchBudget
    from windbreak.forecast.canary import CanaryGate
    from windbreak.forecast.cassettes import LlmTransport
    from windbreak.forecast.citations import CitationVerdict
    from windbreak.forecast.ensemble import VoteAggregate
    from windbreak.forecast.providers import EnsembleMemberLike, ProviderForecast
    from windbreak.forecast.records import BaselineQuoteSnapshot
    from windbreak.forecast.sandbox import ResearchTools

#: One full probability (1.0) expressed in parts-per-million; also the clamp
#: ceiling and the shrinkage denominator.
_PPM_SCALE = 1_000_000

#: Lowest legal ppm probability (the clamp floor).
_MIN_PPM = 0

#: Exact pips-to-ppm factor: pips are 1e-4 and ppm 1e-6, so a binary market
#: price in pips maps to a probability in ppm by multiplying by 100.
_PIPS_TO_PPM = 100

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

#: Abstention reason stamped when every ensemble vote is discarded by the
#: response-side injection screen (SPEC S8.5), leaving nothing to aggregate.
ABSTENTION_ALL_VOTES_DISCARDED: Final = "all_votes_discarded"

#: Event type ledgered when one model vote is discarded as injection-tainted.
FORECAST_OUTPUT_DISCARDED_EVENT: Final = "FORECAST_OUTPUT_DISCARDED"

#: Deterministic rationale stamped on a zero-verified-citation abstention
#: record (mirrors ``triage._TRIAGE_RATIONALE_MD``).
_ABSTENTION_RATIONALE_NO_VERIFIED_CITATIONS_MD: Final = (
    "## Abstained forecast\n\n"
    "The full pipeline ran, but no gathered citation could be independently "
    "verified, so the engine abstained. This record is live-ineligible.\n"
)

#: Deterministic rationale stamped when citations *were* verified but every
#: ensemble vote was discarded by the response-side injection screen (SPEC
#: S8.5). This path must never borrow the no-verified-citations rationale: it
#: would misreport why the engine abstained in the exact scenario S8.5 adds.
_ABSTENTION_RATIONALE_ALL_VOTES_DISCARDED_MD: Final = (
    "## Abstained forecast\n\n"
    "The full pipeline ran and citations were independently verified, but "
    "every ensemble vote was discarded by the response-side injection screen, "
    "leaving nothing to aggregate, so the engine abstained. This record is "
    "live-ineligible.\n"
)

#: Maps each abstention reason to its human-readable rationale, so the audit
#: trail's prose always matches the machine-readable ``abstention_reason``.
_ABSTENTION_RATIONALE_BY_REASON: Final[Mapping[str, str]] = {
    ABSTENTION_NO_VERIFIED_CITATIONS: _ABSTENTION_RATIONALE_NO_VERIFIED_CITATIONS_MD,
    ABSTENTION_ALL_VOTES_DISCARDED: _ABSTENTION_RATIONALE_ALL_VOTES_DISCARDED_MD,
}


@dataclass(frozen=True, slots=True)
class ForecastEvent:
    """One recorded forecast-engine decision (mirrors ``TriageEvent``).

    Attributes:
        event_type: The event kind (e.g. ``FORECAST_OUTPUT_DISCARDED``).
        payload: The JSON-safe event body (int/str/bool leaves only -- never a
            float and never the raw model response text).
        ts: ISO-8601 UTC timestamp of when the event was created.
    """

    event_type: str
    payload: Mapping[str, object]
    ts: str


class ForecastLedgerWriter(Protocol):
    """The seam through which a forecast-engine decision is persisted."""

    def record(self, event: ForecastEvent) -> None:
        """Persist a forecast event.

        Args:
            event: The event to persist.
        """
        ...


class InMemoryForecastLedger:
    """A :class:`ForecastLedgerWriter` retaining events in memory for tests."""

    def __init__(self) -> None:
        """Initialize with an empty event log."""
        self._events: list[ForecastEvent] = []

    def record(self, event: ForecastEvent) -> None:
        """Append a forecast event to the in-memory log.

        Args:
            event: The event to retain.
        """
        self._events.append(event)

    def events_by_type(self, event_type: str) -> tuple[ForecastEvent, ...]:
        """Return every retained event of a given type, in record order.

        Args:
            event_type: The event kind to filter by.

        Returns:
            The matching events.
        """
        return tuple(event for event in self._events if event.event_type == event_type)


@dataclass(frozen=True, slots=True)
class _DiscardRecorder:
    """Binds a ledger writer to a fixed timestamp for discard events.

    Built once per :func:`collect_model_votes` call (only when a ledger is
    wired), so the loop that screens votes never has to re-thread an optional
    ``created_at`` -- the timestamp is already resolved and non-optional here.

    Attributes:
        ledger: The forecast-event ledger writer to persist through.
        ts: The pre-rendered ISO-8601 UTC timestamp stamped on every event.
    """

    ledger: ForecastLedgerWriter
    ts: str

    def record_discard(
        self,
        *,
        market_ticker: str,
        member: EnsembleMemberLike,
        vote_index: int,
        failure: str,
        response_fingerprint: str,
    ) -> None:
        """Ledger one discarded vote with a fingerprint-only payload.

        The raw response never enters the payload -- only its sha256 fingerprint
        (computed by the provider and passed in here) does -- so a tainted
        response cannot leak through the audit trail.

        Args:
            market_ticker: The forecast market's ticker.
            member: The ensemble member whose vote was discarded.
            vote_index: The zero-based index of the discarded vote.
            failure: The ``RESPONSE_FAILURE_*`` code the screen returned.
            response_fingerprint: The rejected response's sha256 fingerprint.
        """
        payload: dict[str, object] = {
            "market_ticker": market_ticker,
            "provider": member.provider,
            "model_version": member.model_version,
            "vote_index": vote_index,
            "failure": failure,
            "response_fingerprint": response_fingerprint,
        }
        self.ledger.record(
            ForecastEvent(FORECAST_OUTPUT_DISCARDED_EVENT, payload, self.ts)
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


def _page_budget_reached(max_pages: int | None, pages_fetched: int) -> bool:
    """Return whether the per-forecast page budget has been reached.

    Args:
        max_pages: The fetch-attempt ceiling, or ``None`` for unbounded.
        pages_fetched: How many fetch attempts have already been made.

    Returns:
        ``True`` when ``max_pages`` is set and already met; ``False`` otherwise
        (including the unbounded ``None`` case).
    """
    return max_pages is not None and pages_fetched >= max_pages


def bounded_web_research(
    subquestions: tuple[str, ...],
    *,
    tools: ResearchTools,
    max_pages: int | None = None,
) -> tuple[Citation, ...]:
    """Stage 5: gather citations through the sandboxed research tools.

    Each subquestion is searched for a candidate URL, whose content is fetched
    through the egress-allowlisted :meth:`ResearchTools.fetch` -- so a search
    result off the allowlist raises :class:`EgressDeniedError` here, on the pipeline
    path itself (a policy violation fails the run closed, never silently). An
    *unreachable* source (the transport raising an ``OSError`` such as
    ``ConnectionError``) is not a policy violation but a dead link, so it is
    skipped: it simply contributes no citation, letting the downstream
    verification stage abstain on an evidence-free run. A dead-link fetch still
    *counts* as one page against ``max_pages`` -- the attempt was made -- so a
    run cannot evade its page budget by hitting unreachable URLs. Each citation's
    ``content_hash`` stays over the *raw* fetched content, while its
    ``quoted_text`` is the sanitized excerpt (SPEC S8.5):
    :func:`windbreak.forecast.sanitize.sanitize_content` strips scripts, hidden
    payloads, and forged delimiters, and
    :func:`windbreak.forecast.sanitize.extract_quote` caps it at
    :data:`~windbreak.forecast.sanitize.MAX_QUOTE_WORDS` words. Because
    :func:`windbreak.forecast.citations.verify_citation` rehashes the raw
    refetch and re-checks the sanitized quote as a raw substring, a page whose
    injection sits *after* the quote window still self-verifies (the quote is a
    contiguous raw substring), whereas a hidden span, ``<script>``, or forged
    delimiter *inside* the window breaks that substring property -- so the
    citation fails to verify and the run fails closed (abstains) rather than
    prompting an LLM with poisoned text. The resulting citations are
    deterministic (the fixture transports derive URL and content from their
    inputs) and integer-free.

    Args:
        subquestions: The decomposed subquestions to research.
        tools: The sandboxed research tools (search/fetch capabilities).
        max_pages: The maximum number of fetch attempts this run may make;
            ``None`` (the default) is unbounded, preserving the pre-budget
            behavior byte-for-byte. Each ``tools.fetch`` call -- including one
            that raises ``OSError`` -- counts as one page.

    Returns:
        One deterministic citation per reachable subquestion candidate URL, up
        to the ``max_pages`` fetch-attempt ceiling.

    Raises:
        ValueError: If ``max_pages`` is negative.
    """
    if max_pages is not None and max_pages < 0:
        msg = f"max_pages must be non-negative or None, got {max_pages}"
        raise ValueError(msg)
    citations: list[Citation] = []
    pages_fetched = 0
    for subquestion in subquestions:
        if _page_budget_reached(max_pages, pages_fetched):
            break
        urls = tools.search(subquestion)
        if not urls:
            continue
        url = urls[0]
        pages_fetched += 1
        try:
            content = tools.fetch(url)
        except OSError:
            continue
        citations.append(
            Citation(
                url=url,
                content_hash=content_hash_of(content),
                quoted_text=extract_quote(
                    sanitize_content(content), max_words=MAX_QUOTE_WORDS
                ),
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


def _build_discard_recorder(
    ledger: ForecastLedgerWriter | None, created_at: datetime | None
) -> _DiscardRecorder | None:
    """Resolve the optional ledger/timestamp pair into a discard recorder.

    Args:
        ledger: The forecast-event ledger writer, or ``None`` to record nothing.
        created_at: The creation instant events are stamped with; required
            whenever ``ledger`` is supplied.

    Returns:
        A :class:`_DiscardRecorder` when a ledger is wired, else ``None``.

    Raises:
        ValueError: If ``ledger`` is supplied without ``created_at`` -- an
            event could not be timestamped, so the call fails loudly rather
            than fabricating an instant.
    """
    if ledger is None:
        return None
    if created_at is None:
        raise ValueError(
            "collect_model_votes requires created_at when a ledger is supplied"
        )
    return _DiscardRecorder(ledger=ledger, ts=_iso_z(created_at))


def _build_model_vote(forecast: ProviderForecast) -> ModelVote:
    """Assemble one :class:`ModelVote` from a provider's parsed forecast.

    The vote's probability comes from the parsed response (via ``forecast``),
    never from the baseline, and its provenance and fingerprint are threaded
    straight through from the provider.

    Args:
        forecast: The provider's structured forecast for this vote.

    Returns:
        The assembled model vote.
    """
    return ModelVote(
        provider=forecast.provider,
        model_version=forecast.model_version,
        declared_training_cutoff=forecast.training_cutoff,
        probability_ppm=forecast.probability_ppm,
        response_fingerprint=forecast.response_fingerprint,
    )


def _resolve_vote_ensemble(
    ensemble: tuple[EnsembleMemberLike, ...] | None,
) -> tuple[EnsembleMemberLike, ...]:
    """Resolve the caller's ensemble override, or the pinned default.

    Args:
        ensemble: A caller-supplied ensemble, or ``None`` for the default.

    Returns:
        The supplied ensemble unchanged, or the package-default
        :data:`~windbreak.forecast.providers.DEFAULT_VOTE_ENSEMBLE`.
    """
    if ensemble is not None:
        return ensemble
    return DEFAULT_VOTE_ENSEMBLE


def collect_model_votes(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    *,
    transport: LlmTransport,
    quotes: tuple[ResearchQuote, ...] = (),
    ledger: ForecastLedgerWriter | None = None,
    created_at: datetime | None = None,
    ensemble: tuple[EnsembleMemberLike, ...] | None = None,
) -> tuple[ModelVote, ...]:
    """Stage 8: collect the surviving, injection-screened model votes.

    This is the only stage that touches the transport seam. It drives one
    :class:`~windbreak.forecast.providers.FixtureVoteProvider` per ensemble
    member -- one deterministic ``complete`` call each, so ``len(ensemble)``
    calls in call order (three by default) -- which screens the response through
    :func:`windbreak.forecast.sanitize.validate_vote_response` and parses the
    structured vote. A response that forges a delimiter, lures a tool call, or
    fails schema validation (e.g. a non-integer ``probability_ppm``) raises
    :class:`~windbreak.forecast.providers.ProviderResponseRejectedError`; that
    vote is *discarded* (never trusted, never retried) and, when a ledger is
    wired, recorded as a :data:`FORECAST_OUTPUT_DISCARDED_EVENT` with a
    fingerprint-only payload. Each surviving vote's ``probability_ppm`` is parsed
    from its response (SPEC S6.3), not derived from the baseline, while its
    fingerprint records provider drift (T14). The returned tuple therefore holds
    between zero and ``len(ensemble)`` votes.

    Args:
        market: The market under forecast.
        baseline: The baseline quote snapshot.
        transport: The LLM transport (recording, replay, or forbidden-live).
        quotes: The sanitized web quotes to thread into each vote prompt as
            untrusted-data blocks (keyword-only; empty by default).
        ledger: The forecast-event ledger writer for discard events, or
            ``None`` to record nothing (keyword-only).
        created_at: The creation instant discard events are stamped with
            (keyword-only); required whenever ``ledger`` is supplied.
        ensemble: The vote ensemble to drive, or ``None`` (keyword-only) for the
            pinned default three-member ensemble. Its length sets the number of
            transport calls, and each member's provenance stamps its vote.

    Returns:
        The surviving ensemble votes, in call order (0 to ``len(ensemble)``).

    Raises:
        ValueError: If ``ledger`` is supplied without ``created_at``.
    """
    recorder = _build_discard_recorder(ledger, created_at)
    members = _resolve_vote_ensemble(ensemble)
    votes: list[ModelVote] = []
    for index, member in enumerate(members):
        provider = FixtureVoteProvider(transport, member)
        try:
            forecast = provider.forecast(market, baseline, index, quotes)
        except ProviderResponseRejectedError as rejected:
            if recorder is not None:
                recorder.record_discard(
                    market_ticker=market.ticker,
                    member=member,
                    vote_index=index,
                    failure=rejected.failure_code,
                    response_fingerprint=rejected.response_fingerprint,
                )
            continue
        votes.append(_build_model_vote(forecast))
    return tuple(votes)


def aggregate_median(votes: tuple[ModelVote, ...]) -> VoteAggregate:
    """Stage 9: aggregate votes into a median with confidence bounds (S8.6).

    Delegates to :func:`windbreak.forecast.ensemble.aggregate_votes`, which owns
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
        abstention_reason: Why the engine abstained. Must be a known reason.

    Returns:
        A schema-valid, immutable abstention forecast record.

    Raises:
        ValueError: If ``abstention_reason`` has no registered rationale, so a
            new abstention path can never silently ship a mismatched rationale.
    """
    try:
        rationale_markdown = _ABSTENTION_RATIONALE_BY_REASON[abstention_reason]
    except KeyError as exc:
        raise ValueError(
            f"No rationale registered for abstention reason {abstention_reason!r}"
        ) from exc
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
        rationale_markdown=rationale_markdown,
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


def _live_gate_open(canary_gate: CanaryGate | None, created_at: datetime) -> bool:
    """Return whether the canary gate permits live eligibility for a record.

    Extracted so :func:`run_pipeline` stays a flat, low-complexity wiring
    function: the gate is open (returns ``True``) when there is no gate at all
    or the gate is not blocking a record created at ``created_at``.

    Args:
        canary_gate: The optional canary drift gate; ``None`` means no gate.
        created_at: The record's creation instant.

    Returns:
        ``True`` if the gate permits live eligibility, else ``False``.
    """
    return canary_gate is None or not canary_gate.is_live_blocked(created_at=created_at)


def run_pipeline(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    *,
    transport: LlmTransport,
    created_at: datetime,
    research_tools: ResearchTools,
    min_verified_citations: int = DEFAULT_MIN_VERIFIED_CITATIONS,
    ledger: ForecastLedgerWriter | None = None,
    budget: ResearchBudget | None = None,
    canary_gate: CanaryGate | None = None,
    ensemble: tuple[EnsembleMemberLike, ...] | None = None,
) -> ForecastRecord:
    """Run the twelve-stage pipeline into a schema-valid forecast record.

    All stages run offline and deterministically; only
    :func:`collect_model_votes` touches ``transport``, so wiring a forbidden
    or empty transport fails the run closed rather than reaching a network.
    Stage 5 reaches the web only through ``research_tools``, whose egress
    allowlist can make the run raise :class:`EgressDeniedError` before any vote.
    Each gathered citation is then independently re-verified (SPEC S8.8); when
    *zero* verify, the run abstains with
    :data:`ABSTENTION_NO_VERIFIED_CITATIONS` -- returning a live-ineligible
    record *before* the vote stage, so ``transport`` is never touched. Only the
    *verified* citations' sanitized quotes are threaded into the vote prompts
    (SPEC S8.5). Each vote response is itself injection-screened; a discarded
    vote is ledgered through ``ledger``, and if *every* vote is discarded the
    run abstains with :data:`ABSTENTION_ALL_VOTES_DISCARDED` rather than
    aggregating over zero votes. Otherwise live eligibility is gated on the
    verified count meeting ``min_verified_citations`` *and* the canary gate not
    blocking the record.

    The optional ``ledger``, ``budget``, and ``canary_gate`` seams all default
    to ``None``, a strict no-op: with no ledger, no budget, and no gate (or an
    unexhausted budget and an open gate) the run behaves exactly as before,
    byte-for-byte. When a budget is supplied it halts before any research once
    the day is exhausted, bounds stage 5's fetches to ``budget.max_pages``, and
    charges the run's research cost (raising fail-closed on a per-forecast
    overrun). Given identical inputs
    and ``created_at``, two runs produce equal records and byte-identical
    payloads.

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
        ledger: The forecast-event ledger writer for vote-discard events, or
            ``None`` to record nothing (keyword-only).
        budget: The optional research budget guarding day/per-forecast spend and
            bounding stage 5's page fetches (keyword-only). ``None`` is a no-op.
        canary_gate: The optional canary drift gate ANDed into live eligibility
            (keyword-only). ``None`` (or an open gate) is a no-op.
        ensemble: The vote ensemble to drive the vote stage with, or ``None``
            (keyword-only) for the pinned default three-member ensemble -- the
            default preserves the pre-#184 vote provenance byte-for-byte.

    Returns:
        The produced, immutable forecast record.

    Raises:
        DailyBudgetExhaustedError: If ``budget`` is supplied and the run's UTC
            day is already exhausted; raised before any research.
        PerForecastBudgetExceededError: If ``budget`` is supplied and the run's
            research cost exceeds the per-forecast ceiling.
    """
    if budget is not None:
        budget.ensure_day_open(at=created_at)
    question_hash = normalize_question(market)
    criteria = extract_resolution_criteria(market)
    base_rate_ppm = outside_view_base_rate(baseline)
    subquestions = decompose_subquestions(market)
    max_pages = budget.max_pages if budget is not None else None
    citations = bounded_web_research(
        subquestions, tools=research_tools, max_pages=max_pages
    )
    verdicts = verify_citations(research_tools, citations, as_of=created_at)
    verified_count = count_verified(verdicts)
    if budget is not None:
        budget.charge_forecast(
            _RESEARCH_COST_MICROS, market_ticker=market.ticker, at=created_at
        )
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
    quotes = tuple(
        ResearchQuote(url=verdict.citation.url, text=verdict.citation.quoted_text)
        for verdict in verdicts
        if verdict.verified
    )
    votes = collect_model_votes(
        market,
        baseline,
        transport=transport,
        quotes=quotes,
        ledger=ledger,
        created_at=created_at,
        ensemble=ensemble,
    )
    if not votes:
        return _build_abstention_record(
            market=market,
            baseline=baseline,
            created_at=created_at,
            question_hash=question_hash,
            citations=citations,
            abstention_reason=ABSTENTION_ALL_VOTES_DISCARDED,
        )
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
        eligible_for_live=_live_gate_open(canary_gate, created_at)
        and is_live_eligible(
            verified_citation_count=verified_count,
            min_verified_citations=min_verified_citations,
            triage_stage="full",
            coherence_flag=coherence_sum is not None,
            abstention_reason=None,
        ),
    )
