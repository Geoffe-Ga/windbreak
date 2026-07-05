"""The forecast engine package (SPEC S8): records, cassettes, and pipeline.

Re-exports the record schema (SPEC S6.3), the offline LLM record/replay
harness, and the deterministic twelve-stage pipeline so callers import from
``hedgekit.forecast`` rather than reaching into the submodules. See SPEC S8
for the engine's responsibilities and firewall boundaries.
"""

from __future__ import annotations

from hedgekit.forecast.cassettes import (
    CassetteMissError,
    ForbiddenLiveTransport,
    LiveCallForbiddenError,
    LlmRequest,
    LlmTransport,
    RecordingCassette,
    ReplayCassette,
)
from hedgekit.forecast.citations import (
    FAILURE_CONTENT_HASH_MISMATCH,
    FAILURE_PUBLICATION_DATE_INVALID,
    FAILURE_QUOTE_NOT_FOUND,
    FAILURE_UNKNOWN_SOURCE_TYPE,
    FAILURE_UNREACHABLE,
    KNOWN_SOURCE_TYPES,
    CitationVerdict,
    content_hash_of,
    count_verified,
    verify_citation,
    verify_citations,
)
from hedgekit.forecast.coherence import (
    OTHER_BUCKET_KEY,
    GroupCoherenceResult,
    forecast_group,
)
from hedgekit.forecast.ensemble import VoteAggregate, aggregate_votes
from hedgekit.forecast.pipeline import (
    ABSTENTION_ALL_VOTES_DISCARDED,
    ABSTENTION_NO_VERIFIED_CITATIONS,
    DEFAULT_MIN_VERIFIED_CITATIONS,
    FORECAST_OUTPUT_DISCARDED_EVENT,
    ForecastEvent,
    ForecastLedgerWriter,
    InMemoryForecastLedger,
    run_pipeline,
)
from hedgekit.forecast.records import (
    BaselineQuoteSnapshot,
    Citation,
    ForecastRecord,
    ModelVote,
    forecast_record_to_payload,
    is_live_eligible,
)
from hedgekit.forecast.sandbox import (
    EgressDeniedError,
    FetchTransport,
    ResearchCache,
    ResearchTools,
    SandboxPathViolationError,
    SearchTransport,
    build_research_tools,
    tool_registry,
)
from hedgekit.forecast.sanitize import (
    DATA_BLOCK_BEGIN,
    DATA_BLOCK_END,
    MAX_QUOTE_WORDS,
    RESPONSE_FAILURE_DELIMITER_FORGERY,
    RESPONSE_FAILURE_EMPTY,
    RESPONSE_FAILURE_TOOL_CALL_LURE,
    TOOL_CALL_MARKERS,
    ResearchQuote,
    extract_quote,
    sanitize_content,
    validate_vote_response,
    wrap_data_block,
)
from hedgekit.forecast.triage import (
    TRIAGE_THRESHOLD_PPM,
    InMemoryTriageLedger,
    TriageEvent,
    TriageLedgerWriter,
    TriagePrior,
    run_triaged_pipeline,
)

__all__ = [
    "ABSTENTION_ALL_VOTES_DISCARDED",
    "ABSTENTION_NO_VERIFIED_CITATIONS",
    "DATA_BLOCK_BEGIN",
    "DATA_BLOCK_END",
    "DEFAULT_MIN_VERIFIED_CITATIONS",
    "FAILURE_CONTENT_HASH_MISMATCH",
    "FAILURE_PUBLICATION_DATE_INVALID",
    "FAILURE_QUOTE_NOT_FOUND",
    "FAILURE_UNKNOWN_SOURCE_TYPE",
    "FAILURE_UNREACHABLE",
    "FORECAST_OUTPUT_DISCARDED_EVENT",
    "KNOWN_SOURCE_TYPES",
    "MAX_QUOTE_WORDS",
    "OTHER_BUCKET_KEY",
    "RESPONSE_FAILURE_DELIMITER_FORGERY",
    "RESPONSE_FAILURE_EMPTY",
    "RESPONSE_FAILURE_TOOL_CALL_LURE",
    "TOOL_CALL_MARKERS",
    "TRIAGE_THRESHOLD_PPM",
    "BaselineQuoteSnapshot",
    "CassetteMissError",
    "Citation",
    "CitationVerdict",
    "EgressDeniedError",
    "FetchTransport",
    "ForbiddenLiveTransport",
    "ForecastEvent",
    "ForecastLedgerWriter",
    "ForecastRecord",
    "GroupCoherenceResult",
    "InMemoryForecastLedger",
    "InMemoryTriageLedger",
    "LiveCallForbiddenError",
    "LlmRequest",
    "LlmTransport",
    "ModelVote",
    "RecordingCassette",
    "ReplayCassette",
    "ResearchCache",
    "ResearchQuote",
    "ResearchTools",
    "SandboxPathViolationError",
    "SearchTransport",
    "TriageEvent",
    "TriageLedgerWriter",
    "TriagePrior",
    "VoteAggregate",
    "aggregate_votes",
    "build_research_tools",
    "content_hash_of",
    "count_verified",
    "extract_quote",
    "forecast_group",
    "forecast_record_to_payload",
    "is_live_eligible",
    "run_pipeline",
    "run_triaged_pipeline",
    "sanitize_content",
    "tool_registry",
    "validate_vote_response",
    "verify_citation",
    "verify_citations",
    "wrap_data_block",
]
