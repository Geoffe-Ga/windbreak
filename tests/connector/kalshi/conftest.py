"""Shared fixtures for hedgekit.connector.kalshi tests (issue #17).

Serves the checked-in Kalshi-shaped JSON fixtures in
`tests/fixtures/exchange/kalshi/` through a fake, `requests`-like session
injected into `KalshiClient`, so every test in this package runs fully
offline against recorded API responses -- never a live network call
(SPEC S7.1: CI runs offline).

`FakeKalshiSession` routes on the *trailing* path segments of the URL
`KalshiClient` builds, mirroring Kalshi's real v2 REST layout
(`.../markets`, `.../events`, `.../markets/{ticker}/orderbook`,
`.../exchange/status`). This pins the endpoints `KalshiConnector` must call
without over-constraining the exact path prefix the implementer chooses.

Neither `hedgekit.connector.kalshi` nor its `client`/`adapter` submodules
exist yet, so importing this conftest fails collection with
`ModuleNotFoundError: No module named 'hedgekit.connector.kalshi'` -- the
expected Gate 1 RED state for issue #17.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hedgekit.connector.kalshi.adapter import KalshiConnector
from hedgekit.connector.kalshi.client import KalshiClient
from hedgekit.connector.snapshot import InMemoryEventLedgerWriter

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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
        return FakeKalshiResponse(404, {"error": "not found"}, date_header=date_header)


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
    return KalshiClient(base_url=_FAKE_BASE_URL, timeout=5, session=fake_kalshi_session)


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
    client = KalshiClient(base_url=_FAKE_BASE_URL, timeout=5, session=session)
    return KalshiConnector(client, ledger, clock=clock)
