"""Shared fixtures and test doubles for windbreak.preflight tests (issue #56, RED).

`windbreak.preflight` does not exist yet, so importing `KeyScopeProbe` below
fails collection for every test module in this directory with
`ModuleNotFoundError: No module named 'windbreak.preflight'` -- the expected
Gate 1 RED state for issue #56 (EPIC_08_ISSUE_01, the preflight skeleton).

Every double here is deliberately narrow: it implements exactly the one seam
(`probe()`, `trade_key_visible()`, `record()`) a preflight check reads, never
the full real collaborator, so a test failure always points at the preflight
check's own logic rather than at an unrelated collaborator's behavior.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from windbreak.connector.fake import FakeExchange
from windbreak.connector.models import NormalizedMarket
from windbreak.preflight import KeyScopeProbe

if TYPE_CHECKING:
    from windbreak.alerts.dispatch import AlertEmitted

#: The shared connector fixtures every `tests/connector/` test also reads;
#: reused here (read-only) for the `exchange.reachable_readonly` check.
_SHARED_EXCHANGE_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "exchange"
)

#: The nine flat JSON files `FakeExchange.from_fixture_dir` requires. The
#: shared fixture directory also nests a `kalshi/` subdirectory this package
#: never reads, so the all-eligible copy below takes exactly these nine.
_FAKE_EXCHANGE_FIXTURE_FILES = (
    "markets.json",
    "order_books.json",
    "exchange.json",
    "balances.json",
    "positions.json",
    "open_orders.json",
    "fills.json",
    "fee_models.json",
    "balance_semantics.json",
)


class RaisingConnector:
    """A `MarketConnector` double whose every read-only method raises.

    Stands in for an unreachable venue: `check_exchange_reachable` must
    classify any exception from either `get_exchange_status` or
    `get_balances` as FAIL (fail-closed, SPEC S3.3), never propagate it.
    """

    def get_exchange_status(self) -> object:
        """Raise, simulating an unreachable exchange status endpoint."""
        raise ConnectionError("exchange status endpoint unreachable")

    def get_balances(self) -> object:
        """Raise, simulating an unreachable balances endpoint."""
        raise ConnectionError("balances endpoint unreachable")


@pytest.fixture
def raising_connector() -> RaisingConnector:
    """Provide a connector double whose status/balance calls always raise."""
    return RaisingConnector()


@pytest.fixture
def fixture_dir() -> Path:
    """Return the path to the shared, read-only exchange JSON fixtures."""
    return _SHARED_EXCHANGE_FIXTURE_DIR


@pytest.fixture
def fake_exchange(fixture_dir: Path) -> FakeExchange:
    """Provide a `FakeExchange` loaded from the shared fixtures."""
    return FakeExchange.from_fixture_dir(fixture_dir)


@dataclass
class FakeScopeProber:
    """A `CredentialScopeProber` double returning a fixed `KeyScopeProbe`.

    Set `raises` to make `probe()` raise instead, exercising the fail-closed
    path shared by `credentials.no_withdrawal_scope` and
    `credentials.scope_verifiable`.
    """

    result: KeyScopeProbe
    raises: bool = False

    def probe(self) -> KeyScopeProbe:
        """Return the fixed probe result, or raise if `raises` is set."""
        if self.raises:
            raise RuntimeError("scope self-test transport failed")
        return self.result


@pytest.fixture
def withdrawal_capable_prober() -> FakeScopeProber:
    """A prober reporting withdrawal capability -- the issue's verbatim FAIL
    example: `report["credentials.no_withdrawal_scope"].status is CheckStatus.FAIL`.
    """
    return FakeScopeProber(
        KeyScopeProbe(
            self_test_supported=True, scope_verified=True, withdrawal_capable=True
        )
    )


@pytest.fixture
def read_only_verified_prober() -> FakeScopeProber:
    """A prober reporting a fully self-tested, non-withdrawal-capable key."""
    return FakeScopeProber(
        KeyScopeProbe(
            self_test_supported=True, scope_verified=True, withdrawal_capable=False
        )
    )


@pytest.fixture
def unverified_scope_prober() -> FakeScopeProber:
    """A prober whose self-test ran but could not verify the key's scope."""
    return FakeScopeProber(
        KeyScopeProbe(
            self_test_supported=True, scope_verified=False, withdrawal_capable=False
        )
    )


@pytest.fixture
def no_self_test_prober() -> FakeScopeProber:
    """A prober whose venue offers no scope self-test at all."""
    return FakeScopeProber(
        KeyScopeProbe(
            self_test_supported=False, scope_verified=False, withdrawal_capable=False
        )
    )


@pytest.fixture
def raising_scope_prober() -> FakeScopeProber:
    """A prober whose `probe()` raises, exercising the fail-closed path."""
    return FakeScopeProber(
        KeyScopeProbe(
            self_test_supported=True, scope_verified=True, withdrawal_capable=False
        ),
        raises=True,
    )


class RaisingLeakProber:
    """A trade-key-leak prober double whose `trade_key_visible()` always raises."""

    def trade_key_visible(self) -> bool:
        """Raise, simulating a broken environment-inspection transport."""
        raise RuntimeError("environment inspection failed")


@pytest.fixture
def raising_leak_prober() -> RaisingLeakProber:
    """Provide a leak-prober double whose `trade_key_visible()` always raises."""
    return RaisingLeakProber()


@dataclass
class RecordingLedgerWriter:
    """A `LedgerWriter` double recording every `AlertEmitted` event handed to it.

    Passed as `AlertDispatcher(sinks=[], ledger_writer=recording_ledger_writer)`
    so a test can assert exactly which alerts a check dispatched, in order.
    """

    events: list[AlertEmitted] = field(default_factory=list)

    def record(self, event: AlertEmitted) -> None:
        """Append `event` to `.events`.

        Args:
            event: The emitted-alert event to record.
        """
        self.events.append(event)


@pytest.fixture
def recording_ledger_writer() -> RecordingLedgerWriter:
    """Provide a fresh recording ledger writer for alert-dispatch assertions."""
    return RecordingLedgerWriter()


def make_market(
    ticker: str,
    jurisdiction_status: str,
    *,
    exchange: str = "fake-exchange",
) -> NormalizedMarket:
    """Build a minimally-valid `NormalizedMarket` for one ticker/jurisdiction pair.

    Args:
        ticker: The market's unique ticker.
        jurisdiction_status: One of `"eligible"`, `"ineligible"`, `"unknown"`.
        exchange: The owning exchange identifier.

    Returns:
        A `NormalizedMarket` with every other field set to an
        arbitrary-but-valid placeholder, since the jurisdiction check only
        reads `jurisdiction_status` (and `ticker`, for its per-market alert
        message).
    """
    return NormalizedMarket(
        exchange=exchange,
        ticker=ticker,
        event_ticker=f"{ticker}-EVT",
        title=f"{ticker} placeholder market",
        resolution_criteria="placeholder",
        category="economics",
        close_time=datetime(2024, 12, 18, 19, 0, tzinfo=UTC),
        expected_resolution_time=None,
        market_type="fully_collateralized_binary",
        price_tick_pips=100,
        min_order_contract_centis=100,
        fractional_trading_enabled=False,
        mutually_exclusive_group_id=None,
        jurisdiction_status=jurisdiction_status,
        raw_exchange_payload_hash="sha256:placeholder",
    )


@pytest.fixture
def all_eligible_fixture_dir(tmp_path: Path) -> Path:
    """Build a dedicated fixture dir identical to the shared one, but with
    every market's `jurisdiction_status` forced to `"eligible"`.

    Copies the nine flat JSON files `FakeExchange.from_fixture_dir` reads from
    `tests/fixtures/exchange/` (skipping its `kalshi/` subdirectory, which
    this package never touches) and rewrites `markets.json` so every entry is
    eligible -- giving CLI happy-path tests a deterministic all-green fixture
    independent of the shared fixture's own (intentionally mixed) jurisdiction
    statuses.
    """
    destination = tmp_path / "exchange"
    destination.mkdir()
    for filename in _FAKE_EXCHANGE_FIXTURE_FILES:
        shutil.copy(_SHARED_EXCHANGE_FIXTURE_DIR / filename, destination / filename)

    markets = json.loads((destination / "markets.json").read_text(encoding="utf-8"))
    for market in markets:
        market["jurisdiction_status"] = "eligible"
    (destination / "markets.json").write_text(json.dumps(markets), encoding="utf-8")

    return destination
