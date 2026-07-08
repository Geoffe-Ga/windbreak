"""Shared fixtures for windbreak.connector.kalshi tests (issues #17, #18, #20).

Serves the checked-in Kalshi-shaped JSON fixtures in
`tests/fixtures/exchange/kalshi/` through a fake, `requests`-like session
injected into `KalshiClient`, so every test in this package runs fully
offline against recorded API responses -- never a live network call
(SPEC S7.1: CI runs offline).

`FakeKalshiSession` routes on the *trailing* path segments of the URL
`KalshiClient` builds, mirroring Kalshi's real v2 REST layout
(`.../markets`, `.../events`, `.../markets/{ticker}/orderbook`,
`.../exchange/status`, `.../series/{series_ticker}`). This pins the endpoints
`KalshiConnector` must call without over-constraining the exact path prefix
the implementer chooses. The recorded `markets.json`/`events.json` fixtures
each fit on a single page (their `cursor` is empty), so these shared fixtures
exercise the common single-page path; the multi-page `cursor` walk is covered
by dedicated paginated sessions in `test_adapter.py`.

Issue #18 adds the `/series/{ticker}` route backing `get_fee_model`: `KXFED`
resolves to the recorded `series_KXFED.json` fee schedule; any other series
ticker 404s, exercising the `UnknownFeeModelError` fail-closed path. A
dedicated `kalshi_malformed_fee_connector` fixture, built over its own tiny
session, serves `series_KXBAD.json` -- a series document with an unrecognized
`fee_type` -- so the malformed-schedule fail-closed path is exercisable
without perturbing the shared fixture-backed connector used everywhere else.

Issue #20 (data-quality halts, freshness TTLs, rate limiting, circuit breaker)
adds four more fixtures, purely additively -- nothing above this point is
modified: `scripted_fault_session` serves a fixed FIFO queue of responses (a
mix of 2xx/4xx/5xx payloads, or a response whose `.json()` raises to simulate a
malformed/truncated body) for `ResilientCaller` retry/backoff/breaker tests
wired end to end through a real `KalshiClient`; `recording_sleeper` is a
no-op callable that records every requested duration instead of ever calling
`time.sleep`; `fake_int_clock` is a mutable, manually-advanceable integer
clock (never wall-clock time) backing `TokenBucket` / `CircuitBreaker` cooldown
and refill timing; `seeded_rng` is a fixed-seed `random.Random` so a test can
compute the exact jitter values `ResilientCaller` must add to its backoff.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from windbreak.connector.kalshi.adapter import KalshiConnector
from windbreak.connector.kalshi.client import KalshiClient
from windbreak.connector.snapshot import InMemoryEventLedgerWriter
from windbreak.net.allowlist import OutboundAllowlist

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    #: Factory signature for building a fresh ScriptedFaultSession per test;
    #: aliased so the fixture signature fits one line (ruff/black agree).
    ScriptedFaultSessionFactory = Callable[
        [list["QueuedFaultResponse"]], "ScriptedFaultSession"
    ]

#: Directory holding the recorded Kalshi API JSON fixtures, resolved
#: relative to this conftest's own directory so it works regardless of cwd.
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "exchange" / "kalshi"

#: The fixed, deterministic `Date` response header every fake response
#: carries when `include_date_header=True` -- distinct from the injected
#: `clock` fixture's value so tests can tell the two sources apart.
_FIXED_SERVER_DATE = datetime(2024, 12, 1, 0, 0, 0, tzinfo=UTC)

#: The base URL every fake-backed `KalshiClient` in this suite is built
#: against; never dialed for real (SPEC S7.1: CI runs offline).
_FAKE_BASE_URL = "https://fake-kalshi.test"

#: Allowlist admitting the fake host, now that ``KalshiClient`` enforces its
#: base URL host at construction (issue #57).
_FAKE_ALLOWLIST = OutboundAllowlist(frozenset({"fake-kalshi.test"}))


def _read_fixture(name: str) -> Any:
    """Parse one recorded JSON fixture by filename.

    Args:
        name: The fixture file's name, e.g. ``"markets.json"``.

    Returns:
        The parsed JSON.
    """
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


class FakeKalshiResponse:
    """A minimal stand-in for a `requests.Response`, used only by these tests."""

    def __init__(
        self, status_code: int, payload: Any, *, date_header: str | None
    ) -> None:
        """Initialize a scripted fake response.

        Args:
            status_code: The HTTP status code to report.
            payload: The value `.json()` returns.
            date_header: The raw `Date` header value, or None to omit it.
        """
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        if date_header is not None:
            self.headers["Date"] = date_header

    def json(self) -> Any:
        """Return the scripted JSON payload."""
        return self._payload


class FakeKalshiSession:
    """Routes `.get(url, ...)` calls to the recorded fixtures by URL suffix.

    No real HTTP ever happens: every route below is matched by inspecting the
    trailing path segments of `url`, mirroring Kalshi's real v2 REST layout.
    An unrecognized ticker (in an orderbook request) or an unrecognized path
    entirely yields a 404, so `KalshiApiError` / `UnknownMarketError` paths
    are exercisable without a second fixture directory.
    """

    def __init__(self, *, include_date_header: bool = True) -> None:
        """Initialize a fresh session with no recorded calls yet.

        Args:
            include_date_header: Whether scripted responses carry a `Date`
                header. False simulates a venue that omits it, exercising
                the connector's clock-fallback path.
        """
        self.calls: list[dict[str, Any]] = []
        self._include_date_header = include_date_header

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        timeout: int | None = None,
    ) -> FakeKalshiResponse:
        """Record the call and return the fixture matching `url`'s route.

        Args:
            url: The full request URL `KalshiClient` built.
            params: Forwarded query parameters (recorded, not used to route).
            timeout: The forwarded request timeout (recorded, not used).

        Returns:
            The scripted fake response for the matched route.
        """
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        date_header = (
            format_datetime(_FIXED_SERVER_DATE) if self._include_date_header else None
        )
        if url.endswith("/exchange/status"):
            return FakeKalshiResponse(
                200, _read_fixture("exchange_status.json"), date_header=date_header
            )
        if url.endswith("/orderbook"):
            ticker = url.rsplit("/", 2)[-2]
            if ticker == "KXFED-24DEC":
                return FakeKalshiResponse(
                    200,
                    _read_fixture("orderbook_KXFED-24DEC.json"),
                    date_header=date_header,
                )
            return FakeKalshiResponse(
                404, {"error": "unknown ticker"}, date_header=date_header
            )
        if url.endswith("/events"):
            return FakeKalshiResponse(
                200, _read_fixture("events.json"), date_header=date_header
            )
        if url.endswith("/markets"):
            return FakeKalshiResponse(
                200, _read_fixture("markets.json"), date_header=date_header
            )
        if "/series/" in url:
            series_ticker = url.rsplit("/series/", 1)[-1]
            if series_ticker == "KXFED":
                return FakeKalshiResponse(
                    200, _read_fixture("series_KXFED.json"), date_header=date_header
                )
            return FakeKalshiResponse(
                404, {"error": "unknown series"}, date_header=date_header
            )
        return FakeKalshiResponse(404, {"error": "not found"}, date_header=date_header)


class _MalformedSeriesSession:
    """Serves a single `/series/KXBAD` route with an unrecognized `fee_type`.

    Kept separate from `FakeKalshiSession` so the shared fixture-backed
    connector used by the rest of this test package never has to know about
    the malformed-schedule case; this session exists solely to back the
    `kalshi_malformed_fee_connector` fixture below.
    """

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        timeout: int | None = None,
    ) -> FakeKalshiResponse:
        """Serve `series_KXBAD.json` for `KXBAD`; 404 for anything else.

        Args:
            url: The full request URL `KalshiClient` built.
            params: Forwarded query parameters (recorded, not used to route).
            timeout: The forwarded request timeout (recorded, not used).

        Returns:
            The scripted fake response for the matched route.
        """
        if url.endswith("/series/KXBAD"):
            return FakeKalshiResponse(
                200, _read_fixture("series_KXBAD.json"), date_header=None
            )
        return FakeKalshiResponse(404, {"error": "unknown series"}, date_header=None)


@pytest.fixture
def ledger() -> InMemoryEventLedgerWriter:
    """Provide a fresh in-memory event ledger writer."""
    return InMemoryEventLedgerWriter()


@pytest.fixture
def clock() -> Callable[[], datetime]:
    """Provide a fixed, deterministic clock, distinct from the fixture Date header."""
    fixed = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def kalshi_fixture_server_date() -> datetime:
    """Provide the fixed `Date` header every fixture-backed response carries."""
    return _FIXED_SERVER_DATE


@pytest.fixture
def fake_kalshi_session() -> FakeKalshiSession:
    """Provide a fresh fake session serving the recorded fixtures."""
    return FakeKalshiSession()


@pytest.fixture
def fake_kalshi_client(fake_kalshi_session: FakeKalshiSession) -> KalshiClient:
    """Provide a `KalshiClient` wired to the fake session (no network)."""
    return KalshiClient(
        base_url=_FAKE_BASE_URL,
        allowlist=_FAKE_ALLOWLIST,
        timeout=5,
        session=fake_kalshi_session,
    )


@pytest.fixture
def kalshi_fixture_connector(
    fake_kalshi_client: KalshiClient,
    ledger: InMemoryEventLedgerWriter,
    clock: Callable[[], datetime],
) -> KalshiConnector:
    """Provide a `KalshiConnector` wired to the fake client, ledger, and clock."""
    return KalshiConnector(fake_kalshi_client, ledger, clock=clock)


@pytest.fixture
def kalshi_connector_missing_date_header(
    ledger: InMemoryEventLedgerWriter, clock: Callable[[], datetime]
) -> KalshiConnector:
    """Provide a `KalshiConnector` whose fake session omits the `Date` header.

    Exercises `get_exchange_time`'s fallback to the injected `clock` when the
    venue's response carries no `Date` header at all.
    """
    session = FakeKalshiSession(include_date_header=False)
    client = KalshiClient(
        base_url=_FAKE_BASE_URL, allowlist=_FAKE_ALLOWLIST, timeout=5, session=session
    )
    return KalshiConnector(client, ledger, clock=clock)


@pytest.fixture
def kalshi_malformed_fee_connector(
    ledger: InMemoryEventLedgerWriter, clock: Callable[[], datetime]
) -> KalshiConnector:
    """Provide a `KalshiConnector` whose `/series/KXBAD` route is malformed.

    Backs the `get_fee_model` fail-closed test: `series_KXBAD.json` carries an
    unrecognized `fee_type`, so `get_fee_model` must raise `UnknownFeeModelError`
    rather than misinterpret the schedule.
    """
    client = KalshiClient(
        base_url=_FAKE_BASE_URL,
        allowlist=_FAKE_ALLOWLIST,
        timeout=5,
        session=_MalformedSeriesSession(),
    )
    return KalshiConnector(client, ledger, clock=clock)


# --- issue #20: resilience/schema-validation fault-injection fixtures -------


class QueuedFaultResponse:
    """A single scripted response for `ScriptedFaultSession`'s FIFO queue.

    `.json()` raises the injected exception instead of returning a payload
    when `json_raises` is set, simulating a malformed or truncated response
    body -- the "ANY non-`KalshiApiError` exception is a retryable transport
    failure" case -- without touching a real socket or a real JSON parser.
    """

    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        *,
        json_raises: Exception | None = None,
    ) -> None:
        """Initialize one scripted response.

        Args:
            status_code: The HTTP status code to report.
            payload: The value `.json()` returns; ignored when `json_raises`
                is set.
            json_raises: An exception `.json()` raises instead of returning,
                or None to return `payload` normally.
        """
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        """Return the scripted payload, or raise the scripted parse failure."""
        if self._json_raises is not None:
            raise self._json_raises
        return self._payload


class ScriptedFaultSession:
    """Serve one fixed FIFO queue of responses to every `.get()` call.

    Every call -- regardless of URL -- pops and returns the next response,
    modeling a single flaky endpoint's exact response sequence (e.g. 500,
    500, 200). The queue is deliberately *not* auto-repeating: a test wires
    exactly as many responses as it expects requests, so an unexpected extra
    call fails loudly (`IndexError`) rather than silently reusing the last
    response and masking a retry-count bug.
    """

    def __init__(self, responses: list[QueuedFaultResponse]) -> None:
        """Initialize with the ordered queue of scripted responses.

        Args:
            responses: The FIFO queue of responses, one per expected call.
        """
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        timeout: int | None = None,
    ) -> QueuedFaultResponse:
        """Record the call and pop the next scripted response off the queue.

        Args:
            url: The full request URL `KalshiClient` built.
            params: Forwarded query parameters (recorded, not used to route).
            timeout: The forwarded request timeout (recorded, not used).

        Returns:
            The next scripted response.

        Raises:
            IndexError: If more calls are made than responses were scripted.
        """
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self._responses.pop(0)


class RecordingSleeper:
    """A no-op sleeper that records every requested duration instead of sleeping.

    Never calls `time.sleep`; `ResilientCaller` / `TokenBucket` tests assert
    against `.calls` to pin exact backoff and rate-limit wait durations
    without ever slowing the suite down or depending on wall-clock time.
    """

    def __init__(self) -> None:
        """Initialize with no recorded calls yet."""
        self.calls: list[int] = []

    def __call__(self, seconds: int) -> None:
        """Record a requested wait duration instead of sleeping.

        Args:
            seconds: The whole-second duration that would have been slept.
        """
        self.calls.append(seconds)


class FakeIntClock:
    """A mutable, manually-advanceable integer clock; never wall-clock time.

    Starts at a fixed integer and only moves when `.advance()` is called, so
    `TokenBucket` refill and `CircuitBreaker` cooldown timing assertions are
    exact and fully deterministic.
    """

    def __init__(self, start: int = 1_000) -> None:
        """Initialize the clock at a fixed starting value.

        Args:
            start: The initial integer "now" the clock reports.
        """
        self._now = start

    def __call__(self) -> int:
        """Return the current fake integer time."""
        return self._now

    def advance(self, seconds: int) -> None:
        """Move the fake clock forward.

        Args:
            seconds: The whole number of seconds to advance by.
        """
        self._now += seconds


@pytest.fixture
def scripted_fault_session() -> ScriptedFaultSessionFactory:
    """Provide a factory building a fresh `ScriptedFaultSession` per test.

    A factory (rather than a single pre-built instance) because each test
    scripts its own distinct response sequence (e.g. 500, 500, 200 vs. 404).
    """
    return ScriptedFaultSession


@pytest.fixture
def queued_fault_response() -> type[QueuedFaultResponse]:
    """Provide the `QueuedFaultResponse` class for scripting individual responses.

    Exposed as a fixture (rather than requiring an import of this conftest
    module as a package) so `tests/connector/kalshi/test_client_resilience.py`
    can build its own scripted response sequences without depending on
    `tests/` being an importable package.
    """
    return QueuedFaultResponse


@pytest.fixture
def recording_sleeper() -> RecordingSleeper:
    """Provide a fresh recording no-op sleeper."""
    return RecordingSleeper()


@pytest.fixture
def fake_int_clock() -> FakeIntClock:
    """Provide a fresh fake integer clock starting at a fixed value."""
    return FakeIntClock()


@pytest.fixture
def seeded_rng() -> random.Random:
    """Provide a `random.Random` seeded for exact, reproducible jitter values.

    The fixed seed lets a test build its own identically-seeded `random.Random`
    to independently compute the exact jitter `ResilientCaller` must add to
    each backoff, rather than asserting only a range.
    """
    return random.Random(20260704)
