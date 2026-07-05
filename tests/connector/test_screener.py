"""Tests for hedgekit.screener.StubScreener (issue #16).

`StubScreener`'s only rule (real filters are issue #6's job):
`jurisdiction_status != "eligible"` blocks a market with a jurisdiction-
referencing reason; `"eligible"` passes. `hedgekit/screener/` does not exist
yet, so importing it fails collection with `ModuleNotFoundError: No module
named 'hedgekit.screener'` -- the expected Gate 1 RED state for issue #16.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from hedgekit.connector.models import NormalizedMarket
from hedgekit.screener import ScreenDecision, StubScreener


def _market(jurisdiction_status: str) -> NormalizedMarket:
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
        jurisdiction_status=jurisdiction_status,
        raw_exchange_payload_hash="sha256:abc123",
    )


@pytest.mark.parametrize("jurisdiction_status", ["ineligible", "unknown"])
def test_non_eligible_jurisdiction_is_blocked_with_a_jurisdiction_reason(
    jurisdiction_status: str,
) -> None:
    decision = StubScreener().screen(_market(jurisdiction_status))

    assert decision.decision == "blocked"
    assert "jurisdiction" in decision.reason.lower()
    assert decision.ticker == "KXFED-24DEC"


def test_eligible_jurisdiction_passes() -> None:
    decision = StubScreener().screen(_market("eligible"))

    assert decision.decision == "eligible"
    assert decision.ticker == "KXFED-24DEC"


def test_screen_decision_is_frozen() -> None:
    decision = ScreenDecision(ticker="X", decision="eligible", reason="ok")

    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.decision = "blocked"  # type: ignore[misc]
