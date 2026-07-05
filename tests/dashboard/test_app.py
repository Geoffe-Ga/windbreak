"""Failing-first tests for the stub localhost dashboard (issue #15, RED).

`hedgekit.dashboard.app` does not exist yet -- only the package's
docstring-only `__init__.py` does -- so the import below fails the whole
module at collection with `ModuleNotFoundError`. Once the implementation
specialist adds `DashboardStatus` and `create_server`, these tests pin the
observable HTTP contract:

- `create_server(*, token, status_source, port)` returns a
  `http.server.ThreadingHTTPServer` bound to the hardcoded loopback host
  `127.0.0.1` (never a caller-supplied host).
- `GET /` with a valid `Authorization: Bearer <token>` header returns 200
  with an HTML body reporting the current mode and last heartbeat, read
  fresh from `status_source` on every request.
- Any other token (missing, wrong, empty, near-miss, or wrong scheme)
  returns 401 with a `WWW-Authenticate` challenge header.
- Any path other than `/` returns 404, independent of auth.
- `create_server(token="")` raises `ValueError` before ever binding a
  socket.

All requests are bounded (`urllib.request` `timeout=`) and the whole module
carries a `pytest-timeout` ceiling so a hung handler cannot wedge the suite.
"""

from __future__ import annotations

import dataclasses
import http.server
import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import pytest

from hedgekit.dashboard.app import DashboardStatus, create_server

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.timeout(15)

#: Deliberately low-entropy so detect-secrets does not flag it as a real credential.
TEST_TOKEN = "test-token"  # pragma: allowlist secret

_REQUEST_TIMEOUT_SECONDS = 5.0


def _build_http_only_opener() -> urllib.request.OpenerDirector:
    """Build a URL opener that speaks only ``http``/``https``.

    Unlike ``urllib.request.urlopen`` (and the default opener from
    ``build_opener``), this director registers no ``FileHandler`` or
    ``FTPHandler``, so a ``file://`` or ``ftp://`` URL raises ``URLError``
    instead of silently reading the filesystem. The dashboard tests only
    ever talk to a loopback ``http://`` server, so restricting the scheme
    surface keeps the helper from becoming an accidental file reader.

    Returns:
        An ``OpenerDirector`` wired with the HTTP handler chain only,
        including ``HTTPErrorProcessor`` so 4xx/5xx responses still surface
        as ``urllib.error.HTTPError`` for callers to assert on.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.HTTPHandler,
        urllib.request.HTTPSHandler,
        urllib.request.HTTPDefaultErrorHandler,
        urllib.request.HTTPRedirectHandler,
        urllib.request.HTTPErrorProcessor,
    ):
        opener.add_handler(handler())
    return opener


#: Shared opener restricted to HTTP(S); see :func:`_build_http_only_opener`.
_HTTP_ONLY_OPENER = _build_http_only_opener()


@dataclasses.dataclass
class _StatusHolder:
    """Mutable holder whose `.current` value an injected `status_source` reads.

    Lets tests prove `status_source` is invoked fresh on every request
    (rather than snapshotted once at `create_server` time) by mutating
    `.current` between two requests and observing different response bodies.
    """

    current: DashboardStatus


@pytest.fixture
def status_holder() -> _StatusHolder:
    """Provide a status holder seeded with an initial RESEARCH-mode status."""
    return _StatusHolder(
        current=DashboardStatus(mode="RESEARCH", last_heartbeat="2026-01-01T00:00:00Z")
    )


@pytest.fixture
def dashboard_server(
    status_holder: _StatusHolder,
) -> Iterator[tuple[http.server.ThreadingHTTPServer, tuple[str, int]]]:
    """Start a dashboard server on an OS-assigned loopback port in a daemon thread.

    Yields the server instance and its bound `(host, port)` address; shuts
    the server down and joins its thread on teardown so no test leaks a
    listening socket or background thread into the next test.
    """
    server = create_server(
        token=TEST_TOKEN, status_source=lambda: status_holder.current, port=0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _get(
    address: tuple[str, int], path: str = "/", *, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], str]:
    """Perform a single bounded GET request against the dashboard server.

    Args:
        address: The `(host, port)` tuple the server is bound to.
        path: The request path.
        headers: Optional request headers (e.g. `Authorization`).

    Returns:
        A `(status_code, response_headers, body_text)` tuple. HTTP error
        responses (4xx/5xx) are captured via `urllib.error.HTTPError` rather
        than raised, so callers can assert on them directly.
    """
    host, port = address
    url = f"http://{host}:{port}{path}"
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with _HTTP_ONLY_OPENER.open(
            request, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = response.read().decode("utf-8")
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def _bearer(token: str) -> dict[str, str]:
    """Build an `Authorization: Bearer <token>` header mapping."""
    return {"Authorization": f"Bearer {token}"}


class TestDashboardStatus:
    """Tests for the `DashboardStatus` value object."""

    def test_is_frozen(self) -> None:
        """A constructed `DashboardStatus` cannot have its fields reassigned."""
        status = DashboardStatus(mode="RESEARCH", last_heartbeat=None)

        with pytest.raises(dataclasses.FrozenInstanceError):
            status.mode = "PAPER"  # type: ignore[misc]

    def test_last_heartbeat_accepts_none(self) -> None:
        """`last_heartbeat=None` is valid before the first heartbeat arrives."""
        status = DashboardStatus(mode="RESEARCH", last_heartbeat=None)

        assert status.last_heartbeat is None
        assert status.mode == "RESEARCH"


class TestCreateServer:
    """Tests for `create_server`'s construction-time contract."""

    def test_empty_token_raises_value_error(self) -> None:
        """An empty token can never be presented by a client, so it is rejected."""
        with pytest.raises(ValueError, match="token"):
            create_server(
                token="",
                status_source=lambda: DashboardStatus(
                    mode="RESEARCH", last_heartbeat=None
                ),
                port=0,
            )

    def test_binds_to_loopback_host_only(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """The bound address is always `127.0.0.1`, never a caller-supplied host."""
        _server, address = dashboard_server

        assert address[0] == "127.0.0.1"

    def test_returns_a_threading_http_server(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """`create_server` returns a `http.server.ThreadingHTTPServer` instance."""
        server, _address = dashboard_server

        assert isinstance(server, http.server.ThreadingHTTPServer)


class TestDashboardAuth:
    """Tests for the `Authorization: Bearer <token>` gate on `GET /`."""

    def test_valid_token_returns_200_with_mode_and_heartbeat(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """A correct bearer token returns 200 with the current status rendered."""
        _server, address = dashboard_server

        status, _headers, body = _get(address, "/", headers=_bearer(TEST_TOKEN))

        assert status == 200
        assert "RESEARCH" in body
        assert "2026-01-01T00:00:00Z" in body

    def test_none_heartbeat_renders_as_never(
        self,
        status_holder: _StatusHolder,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """A `None` last-heartbeat renders as the "never" placeholder, not a crash.

        Before the first heartbeat lands the status source reports
        `last_heartbeat=None`; the page must degrade to a readable placeholder
        rather than rendering ``None`` or raising.
        """
        _server, address = dashboard_server
        status_holder.current = DashboardStatus(mode="RESEARCH", last_heartbeat=None)

        status, _headers, body = _get(address, "/", headers=_bearer(TEST_TOKEN))

        assert status == 200
        assert "never" in body

    def test_status_fields_are_html_escaped(
        self,
        status_holder: _StatusHolder,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """Rendered status fields are HTML-escaped, never injected raw.

        The mode/heartbeat strings will one day come from the ledger (#13);
        rendering them unescaped would be a stored-XSS vector. Asserting the
        escaped form kills a mutant that drops the `html.escape` calls.
        """
        _server, address = dashboard_server
        status_holder.current = DashboardStatus(
            mode="<script>alert(1)</script>", last_heartbeat="<b>ts</b>"
        )

        status, _headers, body = _get(address, "/", headers=_bearer(TEST_TOKEN))

        assert status == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "<script>" not in body
        assert "&lt;b&gt;ts&lt;/b&gt;" in body

    def test_missing_authorization_header_returns_401(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """No `Authorization` header at all is unauthenticated, not a crash."""
        _server, address = dashboard_server

        status, headers, _body = _get(address, "/")

        assert status == 401
        assert "WWW-Authenticate" in headers

    def test_empty_authorization_header_returns_401(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """An empty `Authorization` header value is rejected, not treated as absent."""
        _server, address = dashboard_server

        status, headers, _body = _get(address, "/", headers={"Authorization": ""})

        assert status == 401
        assert "WWW-Authenticate" in headers

    def test_wrong_token_returns_401(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """A syntactically valid but incorrect bearer token is rejected."""
        _server, address = dashboard_server

        status, headers, _body = _get(
            address, "/", headers=_bearer("totally-wrong-token")
        )

        assert status == 401
        assert "WWW-Authenticate" in headers

    def test_near_miss_token_prefix_returns_401(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """A token that merely prefixes the real one is still rejected.

        Guards against a naive `token.startswith(candidate)` (or the reverse)
        comparison instead of an exact, timing-safe match.
        """
        _server, address = dashboard_server

        status, headers, _body = _get(address, "/", headers=_bearer(TEST_TOKEN + "x"))

        assert status == 401
        assert "WWW-Authenticate" in headers

    def test_non_bearer_scheme_returns_401(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """A correctly-valued but wrongly-schemed Authorization header is rejected."""
        _server, address = dashboard_server

        status, headers, _body = _get(
            address, "/", headers={"Authorization": f"Basic {TEST_TOKEN}"}
        )

        assert status == 401
        assert "WWW-Authenticate" in headers

    def test_401_challenge_names_the_bearer_scheme(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """The 401 `WWW-Authenticate` challenge value is exactly `Bearer`.

        Pins the advertised auth scheme so a mutant emitting a blank or wrong
        challenge value is caught, not just the header's presence.
        """
        _server, address = dashboard_server

        status, headers, _body = _get(address, "/")

        assert status == 401
        assert headers["WWW-Authenticate"] == "Bearer"

    def test_non_ascii_token_returns_401_not_a_crash(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """A non-ASCII bearer token is rejected with a clean 401, never a 500.

        `hmac.compare_digest` raises `TypeError` on non-ASCII `str` operands;
        comparing UTF-8 bytes instead means a garbage token (e.g. a non-ASCII
        byte smuggled through the latin-1-decoded header) fails closed as an
        ordinary unauthorized request rather than a dropped connection.
        """
        _server, address = dashboard_server

        status, headers, _body = _get(address, "/", headers=_bearer("tøken"))

        assert status == 401
        assert "WWW-Authenticate" in headers


class TestDashboardRouting:
    """Tests for path routing on the dashboard's HTTP handler."""

    def test_unknown_path_returns_404(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """Any path other than `/` is not found, even with a valid token."""
        _server, address = dashboard_server

        status, _headers, _body = _get(address, "/other", headers=_bearer(TEST_TOKEN))

        assert status == 404

    def test_unknown_path_without_token_is_still_404_not_401(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
    ) -> None:
        """Routing is independent of auth: an unknown path 404s regardless."""
        _server, address = dashboard_server

        status, _headers, _body = _get(address, "/other")

        assert status == 404


class TestStatusSourceFreshness:
    """Tests that `status_source` is re-invoked per request, never cached."""

    def test_two_requests_observe_different_injected_status(
        self,
        dashboard_server: tuple[http.server.ThreadingHTTPServer, tuple[str, int]],
        status_holder: _StatusHolder,
    ) -> None:
        """Mutating the holder between requests changes the very next response."""
        _server, address = dashboard_server

        _status, _headers, first_body = _get(address, "/", headers=_bearer(TEST_TOKEN))

        status_holder.current = DashboardStatus(
            mode="PAPER", last_heartbeat="2026-02-02T00:00:00Z"
        )
        _status, _headers, second_body = _get(address, "/", headers=_bearer(TEST_TOKEN))

        assert "RESEARCH" in first_body
        assert "PAPER" not in first_body
        assert "PAPER" in second_body
        assert "2026-02-02T00:00:00Z" in second_body
        assert first_body != second_body
