"""The forecast provider seam: transport-agnostic vote collection (issue #184).

Re-exports the provider protocol and result types, the pinned default vote
ensemble, and the first network-free :class:`FixtureVoteProvider` so callers
import from ``windbreak.forecast.providers`` rather than reaching into the
submodules. Per the SPEC S8.3 sandbox boundary, nothing here imports
``windbreak.config``; an ensemble member is accepted structurally via
:class:`EnsembleMemberLike`.
"""

from __future__ import annotations

from windbreak.forecast.providers.base import (
    DEFAULT_VOTE_ENSEMBLE,
    EnsembleMember,
    EnsembleMemberLike,
    ForecastProvider,
    ProviderError,
    ProviderForecast,
    ProviderResponseRejectedError,
    build_vote_prompt,
    fingerprint_response,
)
from windbreak.forecast.providers.fixture import FixtureVoteProvider

__all__ = [
    "DEFAULT_VOTE_ENSEMBLE",
    "EnsembleMember",
    "EnsembleMemberLike",
    "FixtureVoteProvider",
    "ForecastProvider",
    "ProviderError",
    "ProviderForecast",
    "ProviderResponseRejectedError",
    "build_vote_prompt",
    "fingerprint_response",
]
