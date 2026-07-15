"""The forecast provider seam: transport-agnostic vote collection (issue #184).

Re-exports the provider protocol and result types, the pinned default vote
ensemble, the network-free :class:`FixtureVoteProvider`, and the hosted
research-forecaster :class:`FutureSearchProvider` (plus its HTTP record/replay
harness) so callers import from ``windbreak.forecast.providers`` rather than
reaching into the submodules. Per the SPEC S8.3 sandbox boundary, nothing here
imports ``windbreak.config``; an ensemble member is accepted structurally via
:class:`EnsembleMemberLike`.
"""

from __future__ import annotations

from windbreak.forecast.providers.base import (
    DEFAULT_VOTE_ENSEMBLE,
    EnsembleMember,
    EnsembleMemberLike,
    ForecastProvider,
    ProviderCitation,
    ProviderError,
    ProviderForecast,
    ProviderResponseRejectedError,
    ProviderVersionDriftError,
    build_vote_prompt,
    fingerprint_response,
)
from windbreak.forecast.providers.fixture import FixtureVoteProvider
from windbreak.forecast.providers.futuresearch import (
    FutureSearchProvider,
    FutureSearchProviderConfig,
)
from windbreak.forecast.providers.http_cassettes import (
    ForbiddenLiveHttpTransport,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    RecordingHttpCassette,
    ReplayHttpCassette,
)

__all__ = [
    "DEFAULT_VOTE_ENSEMBLE",
    "EnsembleMember",
    "EnsembleMemberLike",
    "FixtureVoteProvider",
    "ForbiddenLiveHttpTransport",
    "ForecastProvider",
    "FutureSearchProvider",
    "FutureSearchProviderConfig",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "ProviderCitation",
    "ProviderError",
    "ProviderForecast",
    "ProviderResponseRejectedError",
    "ProviderVersionDriftError",
    "RecordingHttpCassette",
    "ReplayHttpCassette",
    "build_vote_prompt",
    "fingerprint_response",
]
