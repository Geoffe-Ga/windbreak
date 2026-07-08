"""Tests for windbreak.connector.kalshi.client (issue #17): thin HTTPS JSON GET.

`KalshiClient.get(*segments, params=...)` treats each positional argument as
one path segment, percent-encoding it individually (`urllib.parse.quote(seg,
safe="")`) before joining with `/` -- so a ticker segment containing `/` or a
space is escaped rather than corrupting the request path. `session.get` is an
injected seam (never real HTTP; SPEC S7.1: CI runs offline).
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from windbreak.connector.kalshi.client import (
    KalshiApiError,
    KalshiClient,
    KalshiResponse,
    _RedirectFreeSession,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import requests


class _FakeResponse:
    """A minimal stand-in for a `requests.Response`."""

    def __init__(
        self,
        status_code: int,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize a scripted fake response.

        Args:
            status_code: The HTTP status code to report.
            payload: The value `.json()` returns.
            headers: The response headers; empty when omitted.
        """
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        """Return the scripted JSON payload."""
        return self._payload


class _RecordingSession:
    """Captures every `.get(...)` call and returns one scripted response."""

    def __init__(self, response: _FakeResponse) -> None:
        """Initialize with the single response every call will return.

        Args:
            response: The fake response returned by every `.get()` call.
        """
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        timeout: int | None = None,
    ) -> _FakeResponse:
        """Record the call's arguments and return the scripted response.

        Args:
            url: The request URL.
            params: The forwarded query parameters.
            timeout: The forwarded request timeout.

        Returns:
            The scripted fake response.
        """
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.response


def test_get_joins_quoted_segments_onto_base_url() -> None:
    """A segment containing `/` or a space is percent-encoded, not split."""
    # A schema-clean order book: the on-by-default validator inspects every
    # parsed payload, so a synthetic ``{"ok": True}`` would now (correctly)
    # raise ``SchemaAnomalyHaltError`` -- this test only cares about URL building.
    clean_orderbook = {"orderbook": {"yes": [], "no": []}}
    session = _RecordingSession(_FakeResponse(200, clean_orderbook))
    client = KalshiClient(
        base_url="https://example.kalshi.test", timeout=7, session=session
    )

    client.get("markets", "AB/CD E", "orderbook")

    assert (
        session.calls[0]["url"]
        == "https://example.kalshi.test/markets/AB%2FCD%20E/orderbook"
    )


def test_get_forwards_params_and_int_timeout() -> None:
    """`params` and the constructor's `timeout` reach `session.get` unchanged."""
    # A schema-clean ``/markets`` page: the on-by-default validator would reject
    # a synthetic ``{"ok": True}`` payload; this test only checks param/timeout
    # forwarding, so a valid empty page keeps its intent intact.
    session = _RecordingSession(_FakeResponse(200, {"markets": [], "cursor": ""}))
    client = KalshiClient(
        base_url="https://example.kalshi.test", timeout=9, session=session
    )

    client.get("markets", params={"cursor": "abc"})

    call = session.calls[0]
    assert call["params"] == {"cursor": "abc"}
    assert call["timeout"] == 9
    assert isinstance(call["timeout"], int)


@pytest.mark.parametrize("status_code", [300, 400, 404, 500, 503])
def test_non_2xx_status_raises_kalshi_api_error_naming_the_status(
    status_code: int,
) -> None:
    """A non-2xx response raises `KalshiApiError` naming the status code.

    ``300`` pins the upper boundary of the accepted range: a redirect status is
    outside 2xx and must fail closed, guarding the ``status_code <= 299`` bound.

    Resilience is disabled (`resilience=None`) so this exercises the *raw*
    transport status handling: a retryable ``5xx`` surfaces on the first attempt
    with no backoff sleep. The retry/breaker behavior around ``5xx`` is covered
    end-to-end in `test_client_resilience.py`.
    """
    session = _RecordingSession(_FakeResponse(status_code, {"error": "nope"}))
    client = KalshiClient(
        base_url="https://example.kalshi.test", session=session, resilience=None
    )

    with pytest.raises(KalshiApiError, match=str(status_code)):
        client.get("markets")


def test_status_299_is_accepted_as_success() -> None:
    """``299`` is the inclusive upper bound of the 2xx success range.

    Pairs with the ``300`` case above to pin both sides of the accept boundary,
    so a ``<=``-to-``<`` mutation on the upper bound is caught.
    """
    session = _RecordingSession(_FakeResponse(299, {"markets": []}, headers={}))
    client = KalshiClient(base_url="https://example.kalshi.test", session=session)

    assert client.get("markets").payload == {"markets": []}


def test_2xx_returns_kalshi_response_with_payload_and_parsed_date() -> None:
    """A 2xx response yields the parsed JSON payload and UTC server date."""
    fixed = datetime(2024, 12, 1, 12, 30, 0, tzinfo=UTC)
    session = _RecordingSession(
        _FakeResponse(200, {"markets": []}, headers={"Date": format_datetime(fixed)})
    )
    client = KalshiClient(base_url="https://example.kalshi.test", session=session)

    response = client.get("markets")

    assert isinstance(response, KalshiResponse)
    assert response.payload == {"markets": []}
    assert response.server_date == fixed


def test_2xx_without_date_header_has_none_server_date() -> None:
    """A missing `Date` header yields `server_date is None`, not an error."""
    session = _RecordingSession(_FakeResponse(200, {"markets": []}, headers={}))
    client = KalshiClient(base_url="https://example.kalshi.test", session=session)

    response = client.get("markets")

    assert response.server_date is None


def test_construction_rejects_non_https_base_url() -> None:
    """A non-`https://` base URL is rejected at construction, not at request time."""
    with pytest.raises(ValueError, match="https"):
        KalshiClient(base_url="http://example.kalshi.test")


class _RedirectRecordingSession:
    """Captures the `allow_redirects` a `_RedirectFreeSession` forwards."""

    def __init__(self) -> None:
        """Initialize with no recorded `allow_redirects` yet."""
        self.allow_redirects: object = "unset"

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        """Record `allow_redirects` and return a scripted 200 response.

        Args:
            url: The request URL (unused).
            **kwargs: The forwarded request kwargs.

        Returns:
            A scripted fake response.
        """
        self.allow_redirects = kwargs.get("allow_redirects")
        return _FakeResponse(200, {"ok": True})


def test_real_transport_forbids_redirect_following() -> None:
    """The real-transport seam pins `allow_redirects=False` (SSRF posture).

    A fixed-endpoint public client must never chase a `3xx` `Location` to an
    arbitrary host, so every GET through the real transport disables redirects.
    """
    recorder = _RedirectRecordingSession()
    session = _RedirectFreeSession(cast("requests.Session", recorder))

    session.get("https://example.kalshi.test/markets", params=None, timeout=5)

    assert recorder.allow_redirects is False


def test_client_without_injected_session_uses_redirect_free_transport() -> None:
    """Constructing without a session wraps `requests` in the redirect-free seam."""
    client = KalshiClient(base_url="https://example.kalshi.test")

    assert isinstance(client._session, _RedirectFreeSession)
