"""Stub localhost dashboard HTTP surface for Process D (SPEC S5.1, S14).

Serves a single authenticated status page on the loopback interface only.
Per SPEC S14 the dashboard accepts **no public inbound traffic**, so the bind
host is the hardcoded :data:`_BIND_HOST` (``127.0.0.1``) and is deliberately
*not* configurable -- there is no code path that binds any other interface.

Two dependency-injection seams are wired by successor issues: the dashboard
auth ``token`` is minted from configuration (issue #11), and the
``status_source`` callable will be backed by the read-only ledger view (issue
#13). Until then callers pass both explicitly, so this module has no ambient
dependency on either.
"""

from __future__ import annotations

import hmac
import html
import http.server
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger("hedgekit.dashboard")

#: The only interface the dashboard ever binds. Not configurable: SPEC S14
#: forbids public inbound, so there is no code path to any other host.
_BIND_HOST = "127.0.0.1"

#: The single routable path; every other path is a 404.
_ROOT_PATH = "/"

#: The Authorization scheme the bearer token must be presented under.
_BEARER_PREFIX = "Bearer "

#: Rendered in place of ``last_heartbeat`` before the first heartbeat arrives.
_NO_HEARTBEAT = "never"

#: Plain-text body returned with a 401 challenge.
_UNAUTHORIZED_BODY = "401 Unauthorized: a valid bearer token is required.\n"

#: Plain-text body returned for any path other than the root.
_NOT_FOUND_BODY = "404 Not Found.\n"

#: HTML skeleton for the authenticated status page. ``mode`` and ``heartbeat``
#: are HTML-escaped before substitution to prevent injection from a future
#: untrusted status string.
_STATUS_TEMPLATE = (
    "<!DOCTYPE html>\n"
    '<html lang="en">\n'
    '<head><meta charset="utf-8"><title>hedgekit dashboard</title></head>\n'
    "<body>\n"
    "<h1>hedgekit dashboard</h1>\n"
    "<p>mode: {mode}</p>\n"
    "<p>last heartbeat: {heartbeat}</p>\n"
    "</body>\n"
    "</html>\n"
)


@dataclass(frozen=True)
class DashboardStatus:
    """Immutable snapshot of the operational status the dashboard renders.

    Attributes:
        mode: The current SPEC operating mode (e.g. ``RESEARCH``, ``PAPER``).
        last_heartbeat: ISO-8601 timestamp of the most recent heartbeat, or
            ``None`` before any heartbeat has been observed.
    """

    mode: str
    last_heartbeat: str | None


class _DashboardServer(http.server.ThreadingHTTPServer):
    """Threading HTTP server carrying the dashboard's injected dependencies.

    Holds the auth ``token`` and ``status_source`` so the request handler --
    which the stdlib instantiates per connection -- can reach them through
    ``self.server`` without any module-level global state.
    """

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[http.server.BaseHTTPRequestHandler],
        *,
        token: str,
        status_source: Callable[[], DashboardStatus],
    ) -> None:
        """Bind the server and stash its auth token and status source.

        Args:
            server_address: The ``(host, port)`` to bind.
            handler_class: The request handler class to instantiate per request.
            token: The expected bearer token for every authenticated request.
            status_source: Zero-arg callable returning the current status,
                invoked fresh on each authenticated request.
        """
        super().__init__(server_address, handler_class)
        self.token = token
        self.status_source = status_source


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Handle ``GET /`` with a timing-safe bearer-token gate.

    Reads its auth token and status source from the owning
    :class:`_DashboardServer`, so a single handler class serves any server.
    """

    @property
    def _dashboard_server(self) -> _DashboardServer:
        """Return the owning server narrowed to :class:`_DashboardServer`."""
        return cast("_DashboardServer", self.server)

    def do_GET(self) -> None:
        """Route ``GET`` requests: 404 off-root, 401 unauthenticated, else 200.

        Named ``do_GET`` because :class:`http.server.BaseHTTPRequestHandler`
        dispatches by ``"do_" + command``; the name is fixed by that contract,
        not a style choice (see the ``ignore-names`` ruff config).
        """
        if self.path != _ROOT_PATH:
            self._send(HTTPStatus.NOT_FOUND, _NOT_FOUND_BODY, "text/plain")
            return
        if not self._is_authorized():
            self._send_unauthorized()
            return
        self._send_status()

    def _is_authorized(self) -> bool:
        """Return whether the request carries the exact bearer token.

        Uses :func:`hmac.compare_digest` for a timing-safe, exact match so a
        near-miss or prefix token cannot be inferred by response timing. Both
        sides are compared as UTF-8 bytes so a non-ASCII presented token is
        rejected cleanly (a 401) instead of raising ``TypeError`` from
        ``compare_digest``'s ASCII-only string path.

        Returns:
            True only for an ``Authorization: Bearer <token>`` header whose
            token matches exactly; False for missing, blank, wrong-scheme, or
            wrong-value headers.
        """
        header = self.headers.get("Authorization", "")
        if not header.startswith(_BEARER_PREFIX):
            return False
        presented = header.removeprefix(_BEARER_PREFIX)
        return hmac.compare_digest(
            presented.encode("utf-8"), self._dashboard_server.token.encode("utf-8")
        )

    def _send_unauthorized(self) -> None:
        """Send a 401 with a ``WWW-Authenticate: Bearer`` challenge."""
        self._send(
            HTTPStatus.UNAUTHORIZED,
            _UNAUTHORIZED_BODY,
            "text/plain",
            extra_headers={"WWW-Authenticate": "Bearer"},
        )

    def _send_status(self) -> None:
        """Render the current status (read fresh) as an authenticated 200."""
        status = self._dashboard_server.status_source()
        raw_heartbeat = status.last_heartbeat
        heartbeat = _NO_HEARTBEAT if raw_heartbeat is None else raw_heartbeat
        body = _STATUS_TEMPLATE.format(
            mode=html.escape(status.mode),
            heartbeat=html.escape(heartbeat),
        )
        self._send(HTTPStatus.OK, body, "text/html")

    def _send(
        self,
        status: HTTPStatus,
        body: str,
        media_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Write a UTF-8 response with the given status, body, and headers.

        Args:
            status: The HTTP status code to send.
            body: The response body text (UTF-8 encoded before sending).
            media_type: The ``Content-Type`` media type, e.g. ``text/html``
                or ``text/plain`` (``; charset=utf-8`` is appended).
            extra_headers: Optional additional response headers to emit.
        """
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        """Route request logging through the module logger, sans credentials.

        The stdlib default writes to ``stderr`` and can surface request
        details; this override sends the request line through the structured
        module logger instead. The Authorization header is never included, so
        bearer tokens stay out of the log stream. The parameter name ``format``
        is fixed by the supertype's signature (renaming it would break Liskov
        substitution under mypy).

        Args:
            format: The stdlib ``%``-style format string (name mandated by
                :class:`http.server.BaseHTTPRequestHandler`).
            args: The positional arguments for ``format``.
        """
        _LOGGER.info("dashboard request %s", format % args)


def create_server(
    *,
    token: str,
    status_source: Callable[[], DashboardStatus],
    port: int,
) -> http.server.ThreadingHTTPServer:
    """Build a loopback-bound dashboard server guarded by a bearer token.

    Args:
        token: The bearer token every authenticated request must present.
            Must be non-empty -- a blank token can never be presented, so it
            is rejected as a misconfiguration rather than binding a server no
            client could ever reach.
        status_source: Zero-arg callable returning the current
            :class:`DashboardStatus`, invoked fresh on each authenticated
            request so responses always reflect live state.
        port: The loopback TCP port to bind. ``0`` binds an OS-assigned port.

    Returns:
        A :class:`http.server.ThreadingHTTPServer` bound to
        ``(127.0.0.1, port)`` and ready for ``serve_forever``.

    Raises:
        ValueError: If ``token`` is the empty string.
    """
    if not token:
        raise ValueError("token must be a non-empty bearer token")
    return _DashboardServer(
        (_BIND_HOST, port),
        _DashboardHandler,
        token=token,
        status_source=status_source,
    )
