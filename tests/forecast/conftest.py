"""Shared fixtures for hedgekit.forecast tests (issue #22, SPEC S8.2 / S6.3).

`hedgekit/forecast/` does not exist yet, so any test module in this directory
that imports from it fails collection with `ModuleNotFoundError: No module
named 'hedgekit.forecast'` -- the expected Gate 1 RED state for issue #22.

Two deliberate fixture-design choices, both explained here because they shape
every test file in this package:

Fixture-construction choice (market / baseline)
    `NormalizedMarket` and `BaselineQuoteSnapshot` are constructed directly in
    Python (mirroring `tests/connector/test_models.py`'s `_VALID_KWARGS`
    pattern) rather than loaded from a JSON fixture file. Unlike
    `FakeExchange.from_fixture_dir`, `hedgekit.forecast` has no existing
    "load a normalized model from a fixture directory" helper -- inventing one
    here would test fixture-loading plumbing that has nothing to do with
    issue #22's actual contract (the pipeline stages, the record schema, the
    cassette harness).

Cassette-fixture choice (populated replay cassette)
    The *populated* cassette used to prove "replay never touches the network"
    is deliberately NOT built from a static, pre-committed JSON file with
    hardcoded SHA-256 request hashes. `LlmRequest.request_hash()` is specified
    as "sha256 hex of canonical JSON of its fields", but the exact request
    *prompts* the not-yet-written pipeline stages construct are an
    implementation detail this test suite cannot see in advance -- a
    hand-computed hash baked into a committed fixture would be silently
    brittle to any wording change in the real implementation's prompts, for
    zero added confidence. Instead, `make_fake_vote_transport` below provides
    a factory for a deterministic, network-free transport double
    (`FakeVoteTransport`) that pipeline tests wrap in a `RecordingCassette`
    pointed at `tmp_path`, then reload with `ReplayCassette.from_path` -- so
    every hash is self-consistent by construction, regardless of what the real
    implementation's prompt text turns out to be. The two committed
    `tests/fixtures/forecast/*.json` files (`cassettes.json`,
    `cassettes_with_float.json`) are used only for narrow, hash-independent
    structural tests in `test_cassettes.py`: a successful load, a *guaranteed*
    miss (their keys are human-readable placeholders, never a real 64-char
    hex digest), and float-leaf rejection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hedgekit.connector.models import NormalizedMarket
from hedgekit.forecast.records import BaselineQuoteSnapshot

if TYPE_CHECKING:
    from collections.abc import Callable

#: Canned completion text for the three deterministic `collect_model_votes`
#: calls a pipeline run is expected to make. No test asserts anything about
#: this wording (see the "Cassette-fixture choice" note above) -- only that
#: each resulting vote carries a non-empty `response_fingerprint`.
CANNED_VOTE_RESPONSES: tuple[str, str, str] = (
    "vote-response-alpha",
    "vote-response-beta",
    "vote-response-gamma",
)


class FakeVoteTransport:
    """Deterministic, network-free stand-in `LlmTransport` for RED-state tests.

    Returns `CANNED_VOTE_RESPONSES` in call order, cycling if called more than
    three times, so a recording session is fully reproducible. Unlike
    `ForbiddenLiveTransport`, this double never raises -- it is the transport
    every non-cassette pipeline test wires in.
    """

    def __init__(self, responses: tuple[str, ...] = CANNED_VOTE_RESPONSES) -> None:
        """Store the canned response sequence and reset the call counter.

        Args:
            responses: The canned completions to cycle through, in call order.
        """
        self._responses = responses
        self._calls = 0

    def complete(self, request: object) -> str:
        """Return the next canned response, ignoring `request`'s contents.

        Args:
            request: The (unused) `LlmRequest`-shaped call.

        Returns:
            The next canned response in `self._responses`, cycling by index.
        """
        response = self._responses[self._calls % len(self._responses)]
        self._calls += 1
        return response


@pytest.fixture
def make_fake_vote_transport() -> Callable[[], FakeVoteTransport]:
    """Provide a factory for fresh, independently-stateful `FakeVoteTransport`s.

    A factory (not one shared instance) so a determinism test can build two
    completely independent transports that still produce the identical canned
    sequence.
    """
    return FakeVoteTransport


@pytest.fixture
def fixture_dir() -> Path:
    """Return the path to hedgekit.forecast's own committed JSON fixtures."""
    return Path(__file__).resolve().parents[1] / "fixtures" / "forecast"


@pytest.fixture
def created_at() -> datetime:
    """Provide a fixed, timezone-aware UTC instant for deterministic records."""
    return datetime(2024, 12, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def market() -> NormalizedMarket:
    """Provide a valid `NormalizedMarket`, constructed directly.

    See the module docstring's "Fixture-construction choice" for why this is
    not loaded from a JSON fixture file.
    """
    return NormalizedMarket(
        exchange="fake-exchange",
        ticker="KXFED-24DEC",
        event_ticker="KXFED-24",
        title="Fed raises rates in December 2024?",
        resolution_criteria="Resolves YES if the FOMC raises rates.",
        category="economics",
        close_time=datetime(2024, 12, 18, 19, tzinfo=UTC),
        expected_resolution_time=None,
        market_type="fully_collateralized_binary",
        price_tick_pips=100,
        min_order_contract_centis=100,
        fractional_trading_enabled=False,
        mutually_exclusive_group_id=None,
        jurisdiction_status="eligible",
        raw_exchange_payload_hash="sha256:abc123",
    )


@pytest.fixture
def baseline(created_at: datetime) -> BaselineQuoteSnapshot:
    """Provide a valid `BaselineQuoteSnapshot` fixture."""
    return BaselineQuoteSnapshot(
        snapshot_id="snap-0001",
        price_pips=4500,
        fetched_at=created_at,
    )
