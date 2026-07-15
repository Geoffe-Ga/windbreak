"""Tests for windbreak.forecast.providers (issue #184): the provider seam.

Introduces a provider-agnostic seam between the vote-collection stage and any
concrete LLM transport: `ForecastProvider` (a `Protocol` naming
`forecast(market, baseline, vote_index, quotes) -> ProviderForecast`), the
frozen `ProviderForecast` result dataclass, `ProviderError` /
`ProviderResponseRejectedError`, `fingerprint_response`, `build_vote_prompt`
(moved out of `windbreak.forecast.pipeline._vote_prompt`, prompt text
byte-identical), and `FixtureVoteProvider` -- the first, network-free
implementation, which screens a transport's raw response through
`windbreak.forecast.sanitize.validate_vote_response` and either raises
`ProviderResponseRejectedError` or returns a `ProviderForecast` built from the
parsed vote.

`windbreak/forecast/providers/` does not exist yet, so importing from it below
fails collection with `ModuleNotFoundError: No module named
'windbreak.forecast.providers'` -- the expected Gate 1 RED state for issue
#184.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import TYPE_CHECKING, NamedTuple

import pytest

from windbreak.forecast.providers import (
    FixtureVoteProvider,
    ProviderError,
    ProviderForecast,
    ProviderResponseRejectedError,
    build_vote_prompt,
    fingerprint_response,
)
from windbreak.forecast.sanitize import (
    RESPONSE_FAILURE_TOOL_CALL_LURE,
    TOOL_CALL_MARKERS,
)

if TYPE_CHECKING:
    from windbreak.connector.models import NormalizedMarket
    from windbreak.forecast.records import BaselineQuoteSnapshot


class _Member(NamedTuple):
    """A minimal ensemble-member provenance triple (structural, not the
    `windbreak.config.schema.EnsembleMemberConfig` dataclass itself): tests in
    this module only need something exposing `provider` / `model_version` /
    `training_cutoff`, matching the `windbreak.forecast.providers.EnsembleMemberLike`
    protocol the pipeline consumes (its concrete default being
    `windbreak.forecast.providers.EnsembleMember`), so they stay decoupled from
    exactly which concrete type the implementation ultimately threads through
    `FixtureVoteProvider`.
    """

    provider: str
    model_version: str
    training_cutoff: str


_MEMBER = _Member("openai", "gpt-5-forecast", "2024-06-01")

_VALID_RESPONSE = (
    '{"probability_ppm": 654321, "rationale_summary": "solid steady evidence", '
    '"abstain": false}'
)

#: A response that clears the schema layer's shape but still carries a
#: tool-call lure -- `validate_vote_response`'s pre-existing SPEC S8.5 checks
#: must reject it before `FixtureVoteProvider` ever attempts to parse ppm.
_TAINTED_RESPONSE = f'{{"result": "ok", {next(iter(sorted(TOOL_CALL_MARKERS)))}: "x"}}'


class _StubTransport:
    """A minimal `LlmTransport` double returning one fixed response verbatim."""

    def __init__(self, response: str) -> None:
        """Store the response every `complete` call returns.

        Args:
            response: The fixed completion text to return.
        """
        self._response = response
        self.calls = 0

    def complete(self, request: object) -> str:
        """Record one call and return the fixed response, ignoring `request`.

        Args:
            request: The (unused) completion request.

        Returns:
            `self._response`, verbatim, every time.
        """
        self.calls += 1
        return self._response


# --- FixtureVoteProvider: happy path -----------------------------------------------


def test_fixture_vote_provider_happy_path_returns_provider_forecast(
    market: NormalizedMarket, baseline: BaselineQuoteSnapshot
) -> None:
    """A clean, schema-valid response yields a `ProviderForecast` carrying the
    parsed probability/rationale, the member's provenance, a zero fixture cost,
    no citations, and a fingerprint of the raw response text.
    """
    transport = _StubTransport(_VALID_RESPONSE)
    provider = FixtureVoteProvider(transport, _MEMBER)

    result = provider.forecast(market, baseline, 0, ())

    assert isinstance(result, ProviderForecast)
    assert result.probability_ppm == 654_321
    assert result.rationale_summary == "solid steady evidence"
    assert result.citations == ()
    assert result.cost_micros == 0
    assert result.provider == "openai"
    assert result.model_version == "gpt-5-forecast"
    assert result.training_cutoff == "2024-06-01"
    assert (
        result.response_fingerprint
        == hashlib.sha256(_VALID_RESPONSE.encode("utf-8")).hexdigest()
    )
    assert transport.calls == 1


# --- FixtureVoteProvider: tainted response is rejected, not silently trusted -----


def test_fixture_vote_provider_tainted_response_raises_rejected_error(
    market: NormalizedMarket, baseline: BaselineQuoteSnapshot
) -> None:
    """A tool-call-lure response raises `ProviderResponseRejectedError` carrying
    the exact failure code and the response's fingerprint -- never the raw
    (untrusted) response text.
    """
    transport = _StubTransport(_TAINTED_RESPONSE)
    provider = FixtureVoteProvider(transport, _MEMBER)

    with pytest.raises(ProviderResponseRejectedError) as excinfo:
        provider.forecast(market, baseline, 1, ())

    assert excinfo.value.failure_code == RESPONSE_FAILURE_TOOL_CALL_LURE
    assert (
        excinfo.value.response_fingerprint
        == hashlib.sha256(_TAINTED_RESPONSE.encode("utf-8")).hexdigest()
    )


def test_provider_response_rejected_error_is_a_provider_error() -> None:
    """`ProviderResponseRejectedError` is a `ProviderError`, so a caller
    catching the broad root exception still catches this specific one.
    """
    error = ProviderResponseRejectedError(
        failure_code="tool_call_lure", response_fingerprint="a" * 64
    )

    assert isinstance(error, ProviderError)
    assert error.failure_code == "tool_call_lure"
    assert error.response_fingerprint == "a" * 64


# --- ProviderForecast: frozen, and validates its probability_ppm -----------------


def _provider_forecast(**overrides: object) -> ProviderForecast:
    """Build a valid `ProviderForecast`, with any field overridden by `overrides`."""
    kwargs: dict[str, object] = {
        "probability_ppm": 500_000,
        "rationale_summary": "steady evidence",
        "citations": (),
        "cost_micros": 0,
        "provider": "openai",
        "model_version": "gpt-5-forecast",
        "training_cutoff": "2024-06-01",
        "response_fingerprint": "a" * 64,
    }
    kwargs.update(overrides)
    return ProviderForecast(**kwargs)  # type: ignore[arg-type]


def test_provider_forecast_is_frozen() -> None:
    """Mutating any field of a constructed `ProviderForecast` raises."""
    forecast = _provider_forecast()

    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        forecast.probability_ppm = 0  # type: ignore[misc]


@pytest.mark.parametrize("out_of_range", [-1, 1_000_001])
def test_provider_forecast_rejects_out_of_range_probability_ppm(
    out_of_range: int,
) -> None:
    """A `probability_ppm` outside `[0, 1_000_000]` is rejected at construction,
    mirroring `windbreak.forecast.records.ModelVote.__post_init__`.
    """
    with pytest.raises(ValueError, match="probability_ppm"):
        _provider_forecast(probability_ppm=out_of_range)


def test_provider_forecast_rejects_bool_probability_ppm() -> None:
    """A stray `bool` must never masquerade as a `ProviderForecast` probability."""
    with pytest.raises(TypeError, match="probability_ppm"):
        _provider_forecast(probability_ppm=True)


@pytest.mark.parametrize("boundary", [0, 1_000_000])
def test_provider_forecast_accepts_inclusive_probability_ppm_boundaries(
    boundary: int,
) -> None:
    """The inclusive `[0, 1_000_000]` boundaries both construct successfully."""
    forecast = _provider_forecast(probability_ppm=boundary)

    assert forecast.probability_ppm == boundary


# --- fingerprint_response: exact sha256 hex digest --------------------------------


def test_fingerprint_response_matches_hashlib_sha256_hexdigest() -> None:
    """`fingerprint_response` is exactly `sha256(text.encode()).hexdigest()`."""
    text = "an arbitrary response body"

    assert (
        fingerprint_response(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


def test_fingerprint_response_differs_for_different_text() -> None:
    """Two distinct response bodies fingerprint to two distinct digests."""
    assert fingerprint_response("alpha") != fingerprint_response("beta")


# --- build_vote_prompt: byte-identical to the documented scaffold format ---------


def test_build_vote_prompt_matches_the_documented_bare_scaffold(
    market: NormalizedMarket, baseline: BaselineQuoteSnapshot
) -> None:
    """With no quotes, the prompt is exactly the bare, model-authored scaffold
    (byte-identical to the pre-#184 `windbreak.forecast.pipeline._vote_prompt`
    this function was moved from).
    """
    prompt = build_vote_prompt(market, baseline, 0, ())

    expected = (
        f"Estimate the resolution probability for {market.ticker} "
        f"({market.title}); baseline {baseline.price_pips} pips; vote 0."
    )
    assert prompt == expected


def test_build_vote_prompt_indexes_the_vote_number_verbatim(
    market: NormalizedMarket, baseline: BaselineQuoteSnapshot
) -> None:
    """The `vote_index` argument appears verbatim as the prompt's trailing
    integer, so distinct ensemble members receive distinguishable prompts.
    """
    prompt = build_vote_prompt(market, baseline, 2, ())

    assert prompt.endswith("vote 2.")
