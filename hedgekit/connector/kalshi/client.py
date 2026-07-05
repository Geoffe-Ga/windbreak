"""A thin, read-only HTTPS JSON GET client for Kalshi's public v2 REST API.

:class:`KalshiClient` builds a request URL by percent-encoding each path
segment individually (:func:`urllib.parse.quote` with ``safe=""``) and joining
them onto an ``https://`` base, then performs a single GET through an injected
``requests``-like session seam -- so tests supply a fake session with no real
network (SPEC S17.1: the full pipeline runs offline and deterministically in
CI). It models only *public, read-only market access* (SPEC S5.2): it never
attaches auth headers or credentials, and it has no retry/backoff (deferred to
issue #5). Non-2xx responses raise
:class:`KalshiApiError`; a 2xx response yields a :class:`KalshiResponse`
carrying the parsed JSON payload and the parsed ``Date`` header.

This module sits on the money path guarded by ``scripts/lint_no_floats.py``:
the request timeout is an ``int`` and no ``/`` or ``float`` appears anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final, Protocol, cast
from urllib.parse import quote

import requests

from hedgekit.connector.snapshot import LoggingEventLedgerWriter
from hedgekit.connector.validation import (
    SchemaValidator,
    kalshi_default_schema_registry,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hedgekit.connector.resilience import ResilientCaller

#: The current-generation Kalshi public API base (SPEC S7.1).
KALSHI_API_BASE: Final = "https://api.elections.kalshi.com/trade-api/v2"

#: Default per-request timeout, in whole seconds (int: this is the money path).
DEFAULT_TIMEOUT_SECONDS: Final = 10

#: The only URL scheme this client will dial; enforced at construction.
_HTTPS_PREFIX: Final = "https://"

#: Inclusive lower/upper bounds of the HTTP 2xx success range.
_MIN_OK_STATUS: Final = 200
_MAX_OK_STATUS: Final = 299


class KalshiApiError(RuntimeError):
    """Raised when Kalshi returns a non-2xx HTTP status."""

    def __init__(self, status_code: int) -> None:
        """Initialize with the failing status code, named in the message.

        Args:
            status_code: The non-2xx HTTP status code Kalshi returned.
        """
        self.status_code = status_code
        super().__init__(f"Kalshi API request failed with HTTP status {status_code}")


@dataclass(frozen=True, slots=True)
class KalshiResponse:
    """A parsed successful Kalshi response.

    Attributes:
        payload: The parsed JSON body.
        server_date: The ``Date`` header parsed to a UTC datetime, or None when
            the header is absent or unparseable.
    """

    payload: object
    server_date: datetime | None


class _Response(Protocol):
    """The minimal response surface :class:`KalshiClient` reads.

    Attributes:
        status_code: The HTTP status code.
        headers: The response headers.
    """

    status_code: int
    headers: Mapping[str, str]

    def json(self) -> object:
        """Return the parsed JSON body."""
        ...


class Session(Protocol):
    """The minimal ``requests``-like session seam :class:`KalshiClient` calls."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        timeout: int,
    ) -> _Response:
        """Perform a GET request.

        Args:
            url: The fully built request URL.
            params: Query parameters, or None.
            timeout: The request timeout in whole seconds.

        Returns:
            The HTTP response.
        """
        ...


def _parse_server_date(headers: Mapping[str, str]) -> datetime | None:
    """Parse a response ``Date`` header into a UTC datetime.

    Args:
        headers: The response headers.

    Returns:
        The parsed, UTC-normalized datetime, or None when the header is absent
        or cannot be parsed.
    """
    raw = headers.get("Date")
    if raw is None:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class _RedirectFreeSession:
    """Adapt a real ``requests`` session, refusing to follow redirects.

    This client dials one fixed, public JSON endpoint, so it must never chase a
    ``3xx`` ``Location`` to an arbitrary host: following a redirect would let an
    on-path or compromised responder steer a (credential-free) GET to an
    attacker's server and have its body parsed as Kalshi data. Pinning
    ``allow_redirects=False`` turns any redirect into a non-2xx that
    :class:`KalshiClient` surfaces as :class:`KalshiApiError` -- failing closed
    rather than silently switching hosts. Only the real transport is wrapped;
    injected test seams keep their own two-argument ``get``.
    """

    def __init__(self, session: requests.Session) -> None:
        """Store the real session this wrapper forwards to.

        Args:
            session: The live :class:`requests.Session` to dial through.
        """
        self._session = session

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        timeout: int,
    ) -> _Response:
        """Forward a GET, forbidding redirect-following.

        Args:
            url: The fully built request URL.
            params: Query parameters, or None.
            timeout: The request timeout in whole seconds.

        Returns:
            The HTTP response.
        """
        return cast(
            "_Response",
            self._session.get(
                url, params=params, timeout=timeout, allow_redirects=False
            ),
        )


def _default_wall_clock() -> datetime:
    """Return the current UTC time, the default validator's event-stamp clock."""
    return datetime.now(UTC)


class KalshiClient:
    """A thin HTTPS JSON GET client over Kalshi's public v2 REST API."""

    def __init__(
        self,
        base_url: str = KALSHI_API_BASE,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        session: Session | None = None,
        resilience: ResilientCaller | None = None,
        validator: SchemaValidator | None = None,
    ) -> None:
        """Initialize the client, validating the base URL scheme.

        Args:
            base_url: The API base URL; must begin with ``https://``.
            timeout: Per-request timeout, in whole seconds.
            session: An injected ``requests``-like session; a real
                :class:`requests.Session` is created lazily when None.
            resilience: An optional caller wrapping transport+parse in rate
                limiting, retries, and a circuit breaker; passthrough when None.
            validator: The schema validator run on every parsed payload;
                defaults to an on-by-default validator built from
                :func:`kalshi_default_schema_registry`, so schema drift fails
                closed for every endpoint unless a caller opts into another.

        Raises:
            ValueError: If ``base_url`` does not begin with ``https://``.
        """
        if not base_url.startswith(_HTTPS_PREFIX):
            raise ValueError(
                f"base_url must begin with {_HTTPS_PREFIX!r}, got {base_url!r}"
            )
        self._base_url = base_url
        self._timeout = timeout
        self._session: Session = (
            session if session is not None else _RedirectFreeSession(requests.Session())
        )
        self._resilience = resilience
        if validator is not None:
            self._validator = validator
        else:
            self._validator = SchemaValidator(
                kalshi_default_schema_registry(),
                LoggingEventLedgerWriter(),
                wall_clock=_default_wall_clock,
            )

    def _build_url(self, segments: tuple[str, ...]) -> str:
        """Join percent-encoded path segments onto the base URL.

        Args:
            segments: The path segments, each encoded individually so a ``/``
                or space inside a segment is escaped rather than splitting the
                path.

        Returns:
            The fully built request URL.
        """
        encoded = "/".join(quote(segment, safe="") for segment in segments)
        return f"{self._base_url}/{encoded}"

    def get(
        self, *segments: str, params: Mapping[str, str] | None = None
    ) -> KalshiResponse:
        """Perform a GET over the joined path segments and parse the response.

        Transport and parsing run through the injected
        :class:`~hedgekit.connector.resilience.ResilientCaller` when one is
        wired (rate limiting, retries, breaker), or directly otherwise. The
        parsed payload is then always run through the schema validator, so a
        :class:`~hedgekit.connector.validation.SchemaAnomalyHaltError` is raised
        *outside* any retry loop -- schema drift is never retried and never
        counted against the circuit breaker.

        Args:
            *segments: Path segments appended to the base URL, each
                percent-encoded individually.
            params: Optional query parameters, forwarded unchanged.

        Returns:
            The parsed successful response.

        Raises:
            KalshiApiError: If the response status is outside the 2xx range.
            SchemaAnomalyHaltError: If the parsed payload fails schema validation.
        """

        def _fetch() -> KalshiResponse:
            """Run one transport+parse attempt (retried by the resilient caller)."""
            return self._transport(segments, params)

        response = (
            self._resilience.call(_fetch) if self._resilience is not None else _fetch()
        )
        self._validator.validate(
            segments, cast("Mapping[str, object]", response.payload)
        )
        return response

    def _transport(
        self, segments: tuple[str, ...], params: Mapping[str, str] | None
    ) -> KalshiResponse:
        """Perform one GET and parse a 2xx response, raising on any non-2xx.

        Args:
            segments: The path segments to build the request URL from.
            params: Optional query parameters, forwarded unchanged.

        Returns:
            The parsed successful response.

        Raises:
            KalshiApiError: If the response status is outside the 2xx range.
        """
        response = self._session.get(
            self._build_url(segments), params=params, timeout=self._timeout
        )
        if not _MIN_OK_STATUS <= response.status_code <= _MAX_OK_STATUS:
            raise KalshiApiError(response.status_code)
        return KalshiResponse(
            payload=response.json(), server_date=_parse_server_date(response.headers)
        )
