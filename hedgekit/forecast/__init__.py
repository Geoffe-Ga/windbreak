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

__all__ = [
    "BaselineQuoteSnapshot",
    "CassetteMissError",
    "Citation",
    "EgressDeniedError",
    "FetchTransport",
    "ForbiddenLiveTransport",
    "ForecastRecord",
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
    "build_research_tools",
    "forecast_record_to_payload",
    "run_pipeline",
    "tool_registry",
]
