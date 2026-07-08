"""Failing-first tests for the dashboard's new PAPER-loop routes (issue #48, RED).

`windbreak.dashboard.app.create_server` does not yet accept a
`read_models_source` keyword, so every fixture/test below that passes it fails
at call time with `TypeError: create_server() got an unexpected keyword
argument 'read_models_source'` -- the expected Gate 1 RED state for issue #48.
Once that keyword and the `/positions`, `/equity`, `/decisions` routes land,
these tests pin:

- The three new routes are gated behind the *same* bearer-token auth as `/`
  (401 without/with-wrong bearer, 200 with the correct one).
- With `read_models_source=None` (the default), every new route still 200s
  with a "no data yet" placeholder body, rather than 404 or 500 -- mirroring
  `windbreak/dashboard/app.py`'s existing `last_heartbeat=None` ->
  `"never"`-placeholder precedent.
- A hostile (forged-HTML) reason string flowing through `/decisions` is
  rendered escaped, never raw (mirrors
  `test_app.py::test_status_fields_are_html_escaped`).
- `/` itself is unaffected: still 200/401/404 exactly as before, whether or
  not `read_models_source` is wired.

Reuses `tests/dashboard/test_app.py`'s own `TEST_TOKEN`/`_get`/`_bearer`
helpers (DRY) rather than re-deriving a second HTTP-client harness.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from tests.dashboard.test_app import TEST_TOKEN, _bearer, _get

if TYPE_CHECKING:
    import http.server
    from collections.abc import Iterator

pytestmark = pytest.mark.timeout(15)


def _make_read_models_source(rows: dict[str, list[dict[str, object]]]):
    """Build a zero-arg callable returning a fixed `DashboardReadModels`.

    Args:
        rows: A mapping of `positions`/`equity_curve`/`decisions` to their
            read-model row lists.

    Returns:
        A callable suitable for `create_server(read_models_source=...)`.
    """

    def _source() -> object:
        from windbreak.dashboard.views import DashboardReadModels

        return DashboardReadModels(
            positions=rows.get("positions", []),
            equity_curve=rows.get("equity_curve", []),
            decisions=rows.get("decisions", []),
        )

    return _source


@pytest.fixture
def dashboard_server_with_read_models() -> Iterator[
    tuple[http.server.ThreadingHTTPServer, tuple[str, int]]
]:
    """Start a dashboard server wired with a fixed, non-empty read-models source."""
    from windbreak.dashboard.app import DashboardStatus, create_server

    rows = {
        "positions": [
            {
                "seq": 8,
                "created_at": "2026-01-01T00:00:00.000000+00:00",
                "event_type": "PositionsSnapshotRecorded",
                "data": {
                    "positions": [
                        {
                            "ticker": "MKT-DEEP",
                            "quantity_centis": 200,
                            "average_price_pips": 4600,
                        }
                    ]
                },
            }
        ],
        "equity_curve": [
            {
                "seq": 2,
                "created_at": "2026-01-01T00:00:00.000000+00:00",
                "event_type": "EquitySampled",
                "data": {
                    "equity_micros": 1_000_000_000,
                    "floor_micros": 0,
                    "epoch_s": 1_700_000_000,
                },
            }
        ],
        "decisions": [
            {
                "seq": 4,
                "created_at": "2026-01-01T00:00:00.000000+00:00",
                "event_type": "IntentVetoed",
                "data": {
                    "intent_id": "intent-0001",
                    "reasons": ["<script>alert(1)</script>"],
                },
            }
        ],
    }
    server = create_server(
        token=TEST_TOKEN,
        status_source=lambda: DashboardStatus(mode="PAPER", last_heartbeat=None),
        read_models_source=_make_read_models_source(rows),
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def dashboard_server_without_read_models() -> Iterator[
    tuple[http.server.ThreadingHTTPServer, tuple[str, int]]
]:
    """Start a dashboard server with `read_models_source` left at its default `None`."""
    from windbreak.dashboard.app import DashboardStatus, create_server

    server = create_server(
        token=TEST_TOKEN,
        status_source=lambda: DashboardStatus(mode="RESEARCH", last_heartbeat=None),
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("path", ["/positions", "/equity", "/decisions"])
class TestNewRoutesShareTheExistingBearerAuth:
    """Every new route is gated behind the same auth as `/`."""

    def test_missing_bearer_returns_401(
        self,
        path: str,
        dashboard_server_with_read_models: tuple[
            http.server.ThreadingHTTPServer, tuple[str, int]
        ],
    ) -> None:
        """No `Authorization` header at all is unauthenticated on the new route."""
        _server, address = dashboard_server_with_read_models

        status, headers, _body = _get(address, path)

        assert status == 401
        assert "WWW-Authenticate" in headers

    def test_wrong_bearer_returns_401(
        self,
        path: str,
        dashboard_server_with_read_models: tuple[
            http.server.ThreadingHTTPServer, tuple[str, int]
        ],
    ) -> None:
        """An incorrect bearer token is rejected on the new route too."""
        _server, address = dashboard_server_with_read_models

        status, headers, _body = _get(address, path, headers=_bearer("wrong-token"))

        assert status == 401
        assert "WWW-Authenticate" in headers

    def test_correct_bearer_returns_200(
        self,
        path: str,
        dashboard_server_with_read_models: tuple[
            http.server.ThreadingHTTPServer, tuple[str, int]
        ],
    ) -> None:
        """The correct bearer token is accepted on the new route."""
        _server, address = dashboard_server_with_read_models

        status, _headers, _body = _get(address, path, headers=_bearer(TEST_TOKEN))

        assert status == 200


def test_positions_route_renders_the_seeded_ticker(
    dashboard_server_with_read_models: tuple[
        http.server.ThreadingHTTPServer, tuple[str, int]
    ],
) -> None:
    """`/positions` renders the seeded read model's ticker."""
    _server, address = dashboard_server_with_read_models

    status, _headers, body = _get(address, "/positions", headers=_bearer(TEST_TOKEN))

    assert status == 200
    assert "MKT-DEEP" in body


def test_decisions_route_escapes_a_hostile_reason(
    dashboard_server_with_read_models: tuple[
        http.server.ThreadingHTTPServer, tuple[str, int]
    ],
) -> None:
    """`/decisions` escapes the seeded hostile reason string."""
    _server, address = dashboard_server_with_read_models

    status, _headers, body = _get(address, "/decisions", headers=_bearer(TEST_TOKEN))

    assert status == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<script>" not in body


@pytest.mark.parametrize("path", ["/positions", "/equity", "/decisions"])
def test_new_route_renders_no_data_yet_when_read_models_source_is_none(
    path: str,
    dashboard_server_without_read_models: tuple[
        http.server.ThreadingHTTPServer, tuple[str, int]
    ],
) -> None:
    """With `read_models_source=None` (the default), the route still 200s
    with a "no data yet" placeholder, never a 404 or a 500.
    """
    _server, address = dashboard_server_without_read_models

    status, _headers, body = _get(address, path, headers=_bearer(TEST_TOKEN))

    assert status == 200
    assert "no data yet" in body.lower()


def test_root_path_is_unaffected_by_read_models_source_being_wired(
    dashboard_server_with_read_models: tuple[
        http.server.ThreadingHTTPServer, tuple[str, int]
    ],
) -> None:
    """`/` still renders the ordinary status page, unaffected by the new
    read-models wiring existing alongside it.
    """
    _server, address = dashboard_server_with_read_models

    status, _headers, body = _get(address, "/", headers=_bearer(TEST_TOKEN))

    assert status == 200
    assert "windbreak dashboard" in body
