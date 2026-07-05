"""End-to-end tests for issue #20's client/adapter wiring.

Ties `hedgekit.connector.resilience` and `hedgekit.connector.validation`
into `KalshiClient` / `KalshiConnector`, exercised through the fake sessions
in `tests/connector/kalshi/conftest.py`:

* `KalshiClient.get()` runs the transport+parse through an injected
  `ResilientCaller` when one is wired (passthrough when `resilience=None`,
  so the pre-existing `test_client.py` suite's un-wired construction keeps
  working), then always runs the (on-by-default) `SchemaValidator` -- a
  `SchemaAnomalyHaltError` from that validator step runs *outside* the retry
  loop: it is never retried and never counted against the circuit breaker.
* `KalshiConnector._ensure_operational()` fetches exchange status at the
  top of `get_order_book` / `list_markets`; a non-`"open"` status ledgers
  one `CONNECTOR_HALT` (`reason="maintenance"`) and raises
  `MaintenanceHaltError` before any further transport happens.

`hedgekit.connector.resilience` / `hedgekit.connector.validation` do not
exist yet, so importing them fails collection with `ModuleNotFoundError` --
the expected Gate 1 RED state for issue #20.

Heads-up for the implementer (flagged, not silently worked around): wiring
an on-by-default `SchemaValidator` into `KalshiClient.get()` will also run
against the pre-existing `tests/connector/kalshi/test_client.py` suite's
*synthetic* payloads (e.g. `{"ok": True}` returned for `.get("markets", ...,
"orderbook")` / `.get("markets", params=...)`). Those two payloads are not
schema-shaped at all, so once the default validator lands, `test_get_joins_
quoted_segments_onto_base_url` and `test_get_forwards_params_and_int_timeout`
will start raising `SchemaAnomalyHaltError` where they previously returned
`{"ok": True}` uninspected. This test file's own payloads are all either the
real recorded fixtures or the dedicated `faults/` fixtures, so it does not
hit that conflict -- but it is real and needs a deliberate call (e.g.
updating those two payloads to a schema-clean `{"markets": [], "cursor":
""}` shape) when this issue is implemented.
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hedgekit.connector.kalshi.adapter import KalshiConnector
from hedgekit.connector.kalshi.client import KalshiApiError, KalshiClient
from hedgekit.connector.resilience import (
    CONNECTOR_HALT_EVENT,
    ConnectorHaltError,
    MaintenanceHaltError,
    ResiliencePolicy,
    ResilientCaller,
)
from hedgekit.connector.validation import (
    SCHEMA_ANOMALY_EVENT,
    SchemaAnomalyHaltError,
    SchemaValidator,
    kalshi_default_schema_registry,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from hedgekit.connector.snapshot import InMemoryEventLedgerWriter

#: `tests/connector/kalshi/test_client_resilience.py` -> `tests/` ->
#: `tests/fixtures/exchange/kalshi/`.
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "exchange" / "kalshi"

_FAKE_BASE_URL = "https://fake-kalshi.test"


def _read_fixture(relative_name: str) -> Any:
    """Parse one recorded or fault-drift Kalshi JSON fixture.

    Args:
        relative_name: The path relative to `tests/fixtures/exchange/kalshi/`,
            e.g. `"faults/orderbook_drift_money_fee.json"`.

    Returns:
        The parsed JSON.
    """
    return json.loads((_FIXTURE_DIR / relative_name).read_text(encoding="utf-8"))


def _resilience_policy(**overrides: int) -> ResiliencePolicy:
    """Build a generous `ResiliencePolicy`, overridable per test.

    Args:
        **overrides: Field values overriding the defaults below.

    Returns:
        The constructed policy.
    """
    fields: dict[str, int] = {
        "bucket_capacity": 1_000,
        "refill_interval_seconds": 10,
        "max_attempts": 3,
        "base_backoff_seconds": 1,
        "max_backoff_seconds": 30,
        "max_jitter_seconds": 0,
        "failure_threshold": 3,
        "cooldown_seconds": 60,
    }
    fields.update(overrides)
    return ResiliencePolicy(**fields)


class _SingleRouteSession:
    """Serves one fixed payload to every `.get()` call, regardless of URL.

    Used for the schema-drift tests below, which only need one route (the
    order book) and care about the number of underlying HTTP calls, not
    per-route dispatch.
    """

    def __init__(self, status_code: int, payload: Any) -> None:
        """Initialize with the single response every call returns.

        Args:
            status_code: The HTTP status code every call reports.
            payload: The value `.json()` returns on every call.
        """
        self._status_code = status_code
        self._payload = payload
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        timeout: int | None = None,
    ) -> _SingleRouteSession._Response:
        """Record the call and return the fixed scripted response.

        Args:
            url: The full request URL `KalshiClient` built.
            params: Forwarded query parameters (unused).
            timeout: The forwarded request timeout (unused).

        Returns:
            The fixed scripted response.
        """
        self.calls.append(url)
        return self._Response(self._status_code, self._payload)

    class _Response:
        """A minimal stand-in for a `requests.Response`."""

        def __init__(self, status_code: int, payload: Any) -> None:
            """Initialize with a status code and a `.json()` payload.

            Args:
                status_code: The HTTP status code to report.
                payload: The value `.json()` returns.
            """
            self.status_code = status_code
            self._payload = payload
            self.headers: dict[str, str] = {}

        def json(self) -> Any:
            """Return the scripted payload."""
            return self._payload


class _MaintenanceSession:
    """Serves a scripted `/exchange/status` flag pair plus clean data routes.

    Backs `_ensure_operational` tests: the order book and markets/events
    routes always return a minimal, schema-clean payload, so only the
    exchange-status flags vary between test cases.
    """

    def __init__(self, *, exchange_active: bool, trading_active: bool) -> None:
        """Initialize with the exchange-status flag pair to serve.

        Args:
            exchange_active: The `exchange_active` flag to report.
            trading_active: The `trading_active` flag to report.
        """
        self._status = {
            "exchange_active": exchange_active,
            "trading_active": trading_active,
        }
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        timeout: int | None = None,
    ) -> _MaintenanceSession._Response:
        """Route by URL suffix to the status flags or a clean data payload.

        Args:
            url: The full request URL `KalshiClient` built.
            params: Forwarded query parameters (unused).
            timeout: The forwarded request timeout (unused).

        Returns:
            The scripted response for the matched route.
        """
        self.calls.append(url)
        if url.endswith("/exchange/status"):
            return self._Response(200, self._status)
        if url.endswith("/orderbook"):
            return self._Response(200, {"orderbook": {"yes": [], "no": []}})
        if url.endswith("/markets"):
            return self._Response(200, {"markets": [], "cursor": ""})
        if url.endswith("/events"):
            return self._Response(200, {"events": []})
        return self._Response(404, {"error": "not found"})

    class _Response:
        """A minimal stand-in for a `requests.Response`."""

        def __init__(self, status_code: int, payload: Any) -> None:
            """Initialize with a status code and a `.json()` payload.

            Args:
                status_code: The HTTP status code to report.
                payload: The value `.json()` returns.
            """
            self.status_code = status_code
            self._payload = payload
            self.headers: dict[str, str] = {}

        def json(self) -> Any:
            """Return the scripted payload."""
            return self._payload


# =============================================================================
# ResilientCaller wired through KalshiClient.get()
# =============================================================================


def test_get_recovers_after_two_5xx_then_a_200(
    scripted_fault_session: Callable[[list[Any]], Any],
    queued_fault_response: Callable[..., Any],
    ledger: InMemoryEventLedgerWriter,
    fake_int_clock: Callable[[], int],
    recording_sleeper: Callable[[int], None],
    seeded_rng: random.Random,
    clock: Callable[[], datetime],
) -> None:
    """Two transient 500s followed by a 200 succeed, transparently retried."""
    orderbook_payload = _read_fixture("orderbook_KXFED-24DEC.json")
    session = scripted_fault_session(
        [
            queued_fault_response(500, {"error": "boom"}),
            queued_fault_response(500, {"error": "boom"}),
            queued_fault_response(200, orderbook_payload),
        ]
    )
    resilience = ResilientCaller(
        _resilience_policy(max_attempts=3),
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=seeded_rng,
        wall_clock=clock,
    )
    client = KalshiClient(
        base_url=_FAKE_BASE_URL, timeout=5, session=session, resilience=resilience
    )

    response = client.get("markets", "KXFED-24DEC", "orderbook")

    assert response.payload == orderbook_payload
    assert len(session.calls) == 3


def test_persistent_5xx_exhausts_retries_and_eventually_trips_the_breaker(
    scripted_fault_session: Callable[[list[Any]], Any],
    queued_fault_response: Callable[..., Any],
    ledger: InMemoryEventLedgerWriter,
    fake_int_clock: Callable[[], int],
    recording_sleeper: Callable[[int], None],
    seeded_rng: random.Random,
    clock: Callable[[], datetime],
) -> None:
    """A venue stuck returning 500s exhausts retries, then halts via the breaker."""
    always_500 = [queued_fault_response(500, {"error": "boom"}) for _ in range(20)]
    session = scripted_fault_session(always_500)
    resilience = ResilientCaller(
        _resilience_policy(max_attempts=2, failure_threshold=2, cooldown_seconds=999),
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=seeded_rng,
        wall_clock=clock,
    )
    client = KalshiClient(
        base_url=_FAKE_BASE_URL, timeout=5, session=session, resilience=resilience
    )

    with pytest.raises(KalshiApiError):
        client.get(
            "markets", "KXFED-24DEC", "orderbook"
        )  # call #1: 1st breaker failure
    with pytest.raises(KalshiApiError):
        client.get("markets", "KXFED-24DEC", "orderbook")  # call #2: trips OPEN

    with pytest.raises(ConnectorHaltError):
        client.get("markets", "KXFED-24DEC", "orderbook")  # call #3: breaker OPEN

    assert len(ledger.events_by_type(CONNECTOR_HALT_EVENT)) == 1


# =============================================================================
# SchemaValidator wired through KalshiClient.get(): drift halts outside retry
# =============================================================================


def test_money_field_drift_halts_and_bypasses_the_retry_loop_entirely(
    ledger: InMemoryEventLedgerWriter, clock: Callable[[], datetime]
) -> None:
    """An unexpected money/risk field halts on the *first* call: never retried.

    Wires a `ResilientCaller` that would happily retry a transport failure,
    proving the `SchemaAnomalyHaltError` short-circuits before ever reaching it:
    exactly one underlying HTTP call is made, and the breaker is untouched.
    """
    drift_payload = _read_fixture("faults/orderbook_drift_money_fee.json")
    session = _SingleRouteSession(200, drift_payload)
    resilience = ResilientCaller(
        _resilience_policy(max_attempts=5, failure_threshold=1),
        ledger,
        clock=lambda: 0,
        sleeper=lambda seconds: None,
        rng=random.Random(1),
        wall_clock=clock,
    )
    validator = SchemaValidator(
        kalshi_default_schema_registry(), ledger, wall_clock=clock
    )
    client = KalshiClient(
        base_url=_FAKE_BASE_URL,
        timeout=5,
        session=session,
        resilience=resilience,
        validator=validator,
    )

    with pytest.raises(SchemaAnomalyHaltError):
        client.get("markets", "KXFED-24DEC", "orderbook")

    assert len(session.calls) == 1  # never retried
    assert ledger.events_by_type(CONNECTOR_HALT_EVENT) == ()  # breaker never touched
    (event,) = ledger.events_by_type(SCHEMA_ANOMALY_EVENT)
    assert "fee" in event.payload["fields"]

    # The breaker is provably untouched: a second call still raises the same
    # SchemaAnomalyHaltError, not a ConnectorHaltError from a (nonexistent) trip.
    with pytest.raises(SchemaAnomalyHaltError):
        client.get("markets", "KXFED-24DEC", "orderbook")


def test_cosmetic_field_drift_only_warns_and_the_call_still_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cosmetic-allowlisted extra field warns but the response still returns."""
    caplog.set_level(logging.WARNING)
    drift_payload = _read_fixture("faults/orderbook_drift_cosmetic.json")
    session = _SingleRouteSession(200, drift_payload)
    client = KalshiClient(base_url=_FAKE_BASE_URL, timeout=5, session=session)

    response = client.get("markets", "KXFED-24DEC", "orderbook")

    assert response.payload == drift_payload
    assert any(record.levelno >= logging.WARNING for record in caplog.records)


def test_default_validator_validates_the_standard_fixtures_clean(
    fake_kalshi_client: KalshiClient,
) -> None:
    """The client's on-by-default validator never rejects the recorded fixtures.

    Regression guard: wiring an on-by-default `SchemaValidator` into
    `KalshiClient` must not break a plain, un-overridden client fetching the
    real recorded fixtures (`markets.json` / `events.json` /
    `orderbook_KXFED-24DEC.json` / `exchange_status.json` / `series_KXFED.json`).
    """
    assert fake_kalshi_client.get("markets").payload is not None
    assert fake_kalshi_client.get("events").payload is not None
    assert fake_kalshi_client.get("exchange", "status").payload is not None
    assert fake_kalshi_client.get("series", "KXFED").payload is not None
    assert (
        fake_kalshi_client.get("markets", "KXFED-24DEC", "orderbook").payload
        is not None
    )


# =============================================================================
# KalshiConnector._ensure_operational(): maintenance suspension
# =============================================================================


@pytest.mark.parametrize(
    ("exchange_active", "trading_active"),
    [(True, False), (False, False)],  # paused, closed
)
def test_get_order_book_raises_maintenance_halt_when_not_open(
    ledger: InMemoryEventLedgerWriter,
    clock: Callable[[], datetime],
    exchange_active: bool,
    trading_active: bool,
) -> None:
    """`get_order_book` refuses to proceed while the exchange isn't `"open"`."""
    session = _MaintenanceSession(
        exchange_active=exchange_active, trading_active=trading_active
    )
    client = KalshiClient(base_url=_FAKE_BASE_URL, timeout=5, session=session)
    connector = KalshiConnector(client, ledger, clock=clock)

    with pytest.raises(MaintenanceHaltError):
        connector.get_order_book("KXFED-24DEC")

    (event,) = ledger.events_by_type(CONNECTOR_HALT_EVENT)
    assert event.payload["reason"] == "maintenance"
    assert not any(url.endswith("/orderbook") for url in session.calls)


@pytest.mark.parametrize(
    ("exchange_active", "trading_active"),
    [(True, False), (False, False)],  # paused, closed
)
def test_list_markets_raises_maintenance_halt_when_not_open(
    ledger: InMemoryEventLedgerWriter,
    clock: Callable[[], datetime],
    exchange_active: bool,
    trading_active: bool,
) -> None:
    """`list_markets` refuses to proceed while the exchange isn't `"open"`."""
    session = _MaintenanceSession(
        exchange_active=exchange_active, trading_active=trading_active
    )
    client = KalshiClient(base_url=_FAKE_BASE_URL, timeout=5, session=session)
    connector = KalshiConnector(client, ledger, clock=clock)

    with pytest.raises(MaintenanceHaltError):
        connector.list_markets()

    (event,) = ledger.events_by_type(CONNECTOR_HALT_EVENT)
    assert event.payload["reason"] == "maintenance"
    assert not any(url.endswith("/markets") for url in session.calls)


def test_get_order_book_proceeds_normally_when_exchange_is_open(
    ledger: InMemoryEventLedgerWriter, clock: Callable[[], datetime]
) -> None:
    """An `"open"` exchange status never triggers `MaintenanceHaltError`."""
    session = _MaintenanceSession(exchange_active=True, trading_active=True)
    client = KalshiClient(base_url=_FAKE_BASE_URL, timeout=5, session=session)
    connector = KalshiConnector(client, ledger, clock=clock)

    book = connector.get_order_book("KXFED-24DEC")

    assert book.ticker == "KXFED-24DEC"
    assert ledger.events_by_type(CONNECTOR_HALT_EVENT) == ()


def test_list_markets_proceeds_normally_when_exchange_is_open(
    ledger: InMemoryEventLedgerWriter, clock: Callable[[], datetime]
) -> None:
    """An `"open"` exchange status never blocks `list_markets`."""
    session = _MaintenanceSession(exchange_active=True, trading_active=True)
    client = KalshiClient(base_url=_FAKE_BASE_URL, timeout=5, session=session)
    connector = KalshiConnector(client, ledger, clock=clock)

    assert connector.list_markets() == ()
    assert ledger.events_by_type(CONNECTOR_HALT_EVENT) == ()
