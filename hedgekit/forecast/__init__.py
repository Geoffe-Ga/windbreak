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
from hedgekit.forecast.coherence import (
    OTHER_BUCKET_KEY,
    GroupCoherenceResult,
    forecast_group,
)
from hedgekit.forecast.ensemble import VoteAggregate, aggregate_votes
from hedgekit.forecast.pipeline import run_pipeline
from hedgekit.forecast.records import (
    BaselineQuoteSnapshot,
    Citation,
    ForecastRecord,
    ModelVote,
    forecast_record_to_payload,
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
from hedgekit.forecast.triage import (
    TRIAGE_THRESHOLD_PPM,
    InMemoryTriageLedger,
    TriageEvent,
    TriageLedgerWriter,
    TriagePrior,
    run_triaged_pipeline,
)

__all__ = [
    "OTHER_BUCKET_KEY",
    "TRIAGE_THRESHOLD_PPM",
    "BaselineQuoteSnapshot",
    "CassetteMissError",
    "Citation",
    "EgressDeniedError",
    "FetchTransport",
    "ForbiddenLiveTransport",
    "ForecastRecord",
    "GroupCoherenceResult",
    "InMemoryTriageLedger",
    "LiveCallForbiddenError",
    "LlmRequest",
    "LlmTransport",
    "ModelVote",
    "RecordingCassette",
    "ReplayCassette",
    "ResearchCache",
    "ResearchTools",
    "SandboxPathViolationError",
    "SearchTransport",
    "TriageEvent",
    "TriageLedgerWriter",
    "TriagePrior",
    "VoteAggregate",
    "aggregate_votes",
    "build_research_tools",
    "forecast_group",
    "forecast_record_to_payload",
    "run_pipeline",
    "run_triaged_pipeline",
    "tool_registry",
]
