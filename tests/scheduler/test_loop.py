"""Per-stage failing-first tests for `hedgekit.scheduler.loop` (issue #48, RED).

`hedgekit/scheduler/` does not exist yet -- only `hedgekit/__init__.py` and its
sibling packages do -- so every import below of `hedgekit.scheduler.loop` fails
collection with `ModuleNotFoundError: No module named 'hedgekit.scheduler'`,
the expected Gate 1 RED state for issue #48.

This module pins the per-stage composition contract the ralph-chief-architect
specified, plus a handful of small, invented supporting names this test suite
needs and documents inline (the architect fixed `PaperTickDeps`,
`build_paper_deps`, `run_single_tick`, `TickOutcome`, `ApprovalSeam`, and
`KernelApproval`; everything else below -- `build_evaluation_context`,
`risk_limits_from_config`, `compute_equity_micros`, `is_quote_fresh`,
`market_snapshot_event_to_record` -- is this test's own minimal, documented
invention for the per-stage seams, kept small on purpose).

The single most load-bearing fact this module proves (issue #48's own
"Load-bearing constraint"): composing the *real*, unmodified
`RiskKernel.evaluate_intent` with the *real* `ApprovalPipeline.approve` via
`KernelApproval` can never mint a token today, because three SPEC S10.3
checks are still unconditional-veto stubs (`hedgekit/riskkernel/checks.py`)
and the three reconciliation checks fail closed on a `None` verification
snapshot. `test_kernel_approval_vetoes_before_minting_any_token` pins the
*exact* six veto reasons this yields, mirroring
`tests/riskkernel/test_checks.py::test_default_checks_over_permissive_context_leaves_only_stubs_vetoing`
but with `verification=None` (the honest PAPER-loop wiring: no live exchange
verification cycle runs yet) instead of that test's permissive CLEAN
snapshot, so the three reconciliation checks join the three stubs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hedgekit.numeric.types import MoneyMicros
from hedgekit.riskkernel.modes import Mode
from tests.riskkernel.conftest import DEFAULT_MARKET_TICKER, make_context, make_intent
from tests.scheduler.conftest import (
    DEFAULT_NOW_EPOCH_S,
    build_kernel_approval_components,
)

#: The six veto reasons a PAPER-mode evaluation with `verification=None` must
#: produce today, in the exact SPEC S10.3 check order
#: (`hedgekit/riskkernel/checks.py::_SPEC_10_3_CHECK_NAMES`): the
#: `jurisdiction_product_eligibility` stub (position 2) fires before the three
#: reconciliation checks (positions 5-7, each failing closed on the missing
#: verification snapshot), and the two `#110` stubs fire last (positions 21-22).
_EXPECTED_VETO_REASONS = (
    "awaiting NormalizedMarket metadata",
    "balance verification stale or missing",
    "position verification stale or missing",
    "open-order verification stale or missing",
    "blocked on #110 (exchange status feed)",
    "blocked on #110 (pipeline heartbeat)",
)


# --- ApprovalSeam / KernelApproval composition (the load-bearing constraint) ----


def test_kernel_approval_vetoes_before_minting_any_token() -> None:
    """`KernelApproval.decide` vetoes and mints no token (issue #48, #110).

    Composes the real `RiskKernel.evaluate_intent` (for the ledgered audit
    event) with the real `ApprovalPipeline.approve` (for the reserve-and-issue
    path), over an otherwise fully-permissive context (every one of the 21
    real SPEC S10.3 checks passes -- `tests.riskkernel.conftest.make_context`'s
    documented guarantee) except `verification=None`. Exactly the six known
    reasons veto; the pipeline's `approve` is never reached far enough to
    reserve capital or issue a token.
    """
    from hedgekit.scheduler.loop import ApprovalOutcome, KernelApproval

    kernel, pipeline, _writer = build_kernel_approval_components()
    approval = KernelApproval(kernel, pipeline)
    intent = make_intent()
    context = make_context(
        mode=Mode.PAPER,
        verification=None,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
    )

    outcome = approval.decide(intent, context)

    assert isinstance(outcome, ApprovalOutcome)
    assert outcome.token is None
    assert outcome.decision.vetoed is True
    assert outcome.decision.reasons == _EXPECTED_VETO_REASONS


def test_kernel_approval_ledgers_exactly_one_intent_vetoed_event() -> None:
    """The kernel's own ledgered audit trail carries exactly one veto event.

    `KernelApproval` must not double-record: `RiskKernel.evaluate_intent`
    ledgers the audit `IntentVetoed` event once, and a vetoed decision must
    never reach `ApprovalPipeline.approve`'s reservation-ledger writes.
    """
    from hedgekit.scheduler.loop import KernelApproval

    kernel, pipeline, writer = build_kernel_approval_components()
    approval = KernelApproval(kernel, pipeline)
    intent = make_intent()
    context = make_context(mode=Mode.PAPER, verification=None)

    approval.decide(intent, context)

    vetoed_events = [
        event for event in writer.events if event.event_type == "IntentVetoed"
    ]
    reservation_events = [
        event for event in writer.events if event.event_type == "ReservationCreated"
    ]
    approval_events = [
        event for event in writer.events if event.event_type == "ApprovalTokenIssued"
    ]
    assert len(vetoed_events) == 1
    assert reservation_events == []
    assert approval_events == []


def test_kernel_approval_mints_a_token_when_every_check_passes() -> None:
    """Given a context where every one of the 24 checks passes, a token mints.

    Proves `KernelApproval` is not *structurally* incapable of approving --
    only today's stub/verification wiring blocks it -- by stubbing out the
    three hard-veto checks and supplying a permissive `VerificationSnapshot`
    (mirroring `tests.riskkernel.conftest`'s own default), so this test does
    not silently pass for the wrong reason (e.g. a `KernelApproval` that
    always vetoes).
    """
    import dataclasses

    from hedgekit.riskkernel import checks as checks_module
    from hedgekit.scheduler.loop import KernelApproval
    from tests.riskkernel.conftest import make_verification_snapshot

    kernel, pipeline, _writer = build_kernel_approval_components()
    approval = KernelApproval(kernel, pipeline)
    intent = make_intent()
    context = make_context(
        mode=Mode.PAPER,
        verification=make_verification_snapshot(),
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
    )
    real_only_checks = tuple(
        check
        for check in checks_module.DEFAULT_CHECKS
        if check.name not in {"exchange_status_ok", "pipeline_heartbeat_ok"}
        and check.name != "jurisdiction_product_eligibility"
    )
    # Neither `RiskKernel.evaluate_intent` nor `ApprovalPipeline.approve`
    # exposes a seam to override `DEFAULT_CHECKS`; both call
    # `checks.evaluate_intent(intent, effective)` via a module-attribute
    # lookup (`from hedgekit.riskkernel import checks`), so patching the
    # attribute on the shared `hedgekit.riskkernel.checks` module object
    # affects both call sites identically -- proving the composition end to
    # end (kernel evaluates and ledgers `IntentApproved`, then the pipeline
    # re-evaluates, reserves, and mints) rather than just one half of it.
    original_evaluate_intent = checks_module.evaluate_intent

    def _patched_evaluate_intent(
        intent_arg: object, context_arg: object, checks: object = real_only_checks
    ) -> object:
        return original_evaluate_intent(intent_arg, context_arg, checks)  # type: ignore[arg-type]

    checks_module.evaluate_intent = _patched_evaluate_intent  # type: ignore[assignment]
    try:
        outcome = approval.decide(intent, context)
    finally:
        checks_module.evaluate_intent = original_evaluate_intent  # type: ignore[assignment]

    assert outcome.token is not None
    assert outcome.token.claims.intent_id == intent.intent_id
    assert dataclasses.is_dataclass(outcome.token.claims)


# --- config -> RiskLimits/AccountState mapping ---------------------------------


def test_build_evaluation_context_maps_capital_floor_from_config() -> None:
    """`build_evaluation_context` maps `config.capital.floor_micros` to
    `RiskLimits.floor`, so the composed PAPER context honors the operator's
    configured equity floor rather than some hardcoded value.
    """
    from hedgekit.config.schema import CapitalConfig, HedgekitConfig
    from hedgekit.scheduler.loop import build_evaluation_context

    config = HedgekitConfig(capital=CapitalConfig(floor_micros=42_000_000))

    context = build_evaluation_context(
        config,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        verification=None,
        instrument_whitelist=frozenset({DEFAULT_MARKET_TICKER}),
    )

    assert context.limits.floor == MoneyMicros(42_000_000)


def test_build_evaluation_context_maps_risk_thresholds_from_config() -> None:
    """`build_evaluation_context` maps every `config.risk` ttl/threshold field
    it has a `RiskLimits` counterpart for, not just the floor.
    """
    from hedgekit.config.schema import HedgekitConfig, RiskConfig
    from hedgekit.scheduler.loop import build_evaluation_context

    config = HedgekitConfig(
        risk=RiskConfig(quote_ttl_seconds=17, clock_skew_max_seconds=3)
    )

    context = build_evaluation_context(
        config,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        verification=None,
        instrument_whitelist=frozenset({DEFAULT_MARKET_TICKER}),
    )

    assert context.limits.quote_ttl_seconds == 17
    assert context.limits.clock_skew_max_seconds == 3


def test_build_evaluation_context_fails_closed_on_verification_none() -> None:
    """`verification=None` flows straight through -- the fail-closed default.

    No production default is threaded in its place: a forgotten wiring
    reaching the real checks must fail closed via the three reconciliation
    checks (mirrors `hedgekit.riskkernel.context.EvaluationContext`'s own
    documented "no production default" contract for this field).
    """
    from hedgekit.config.schema import HedgekitConfig
    from hedgekit.scheduler.loop import build_evaluation_context

    context = build_evaluation_context(
        HedgekitConfig(),
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        verification=None,
        instrument_whitelist=frozenset({DEFAULT_MARKET_TICKER}),
    )

    assert context.verification is None


def test_build_evaluation_context_stamps_now_epoch_s_verbatim() -> None:
    """The supplied `now_epoch_s` is stamped verbatim -- never `time.time()`."""
    from hedgekit.config.schema import HedgekitConfig
    from hedgekit.scheduler.loop import build_evaluation_context

    context = build_evaluation_context(
        HedgekitConfig(),
        now_epoch_s=1_234_567,
        verification=None,
        instrument_whitelist=frozenset({DEFAULT_MARKET_TICKER}),
    )

    assert context.now_epoch_s == 1_234_567


# --- equity math (scaled ints only, no float) ----------------------------------


def test_compute_equity_micros_sums_cash_and_positions_value_exactly() -> None:
    """Equity is the exact integer sum of available cash and positions value."""
    from hedgekit.scheduler.loop import compute_equity_micros

    equity = compute_equity_micros(
        available_cash=MoneyMicros(100_000_000),
        positions_value=MoneyMicros(25_000_000),
    )

    assert equity == MoneyMicros(125_000_000)


def test_compute_equity_micros_rejects_a_float_argument() -> None:
    """A float can never enter the equity path (SPEC S6.1): passing one raises.

    `MoneyMicros.__post_init__` already rejects a non-int `.value`, so
    smuggling a float in via a raw (non-`MoneyMicros`) argument must raise
    rather than silently truncate or coerce.
    """
    from hedgekit.scheduler.loop import compute_equity_micros

    with pytest.raises((TypeError, AttributeError)):
        compute_equity_micros(available_cash=1_000_000.5, positions_value=0)  # type: ignore[arg-type]


# --- stale-quote skip via ensure_fresh ------------------------------------------


def test_is_quote_fresh_true_within_ttl() -> None:
    """A quote exactly at the ttl boundary is fresh (inclusive), per
    `hedgekit.connector.freshness.is_fresh`'s own documented boundary.
    """
    from hedgekit.connector.models import OrderBookSnapshot
    from hedgekit.scheduler.loop import is_quote_fresh

    fetched_at = datetime(2026, 1, 1, tzinfo=UTC)
    book = OrderBookSnapshot(
        ticker=DEFAULT_MARKET_TICKER, yes_bids=(), yes_asks=(), fetched_at=fetched_at
    )

    fresh = is_quote_fresh(
        book, ttl_seconds=10, now=datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    )

    assert fresh is True


def test_is_quote_fresh_false_past_ttl() -> None:
    """A quote one second past its ttl is stale, never silently accepted."""
    from hedgekit.connector.models import OrderBookSnapshot
    from hedgekit.scheduler.loop import is_quote_fresh

    fetched_at = datetime(2026, 1, 1, tzinfo=UTC)
    book = OrderBookSnapshot(
        ticker=DEFAULT_MARKET_TICKER, yes_bids=(), yes_asks=(), fetched_at=fetched_at
    )

    fresh = is_quote_fresh(
        book, ttl_seconds=10, now=datetime(2026, 1, 1, 0, 0, 11, tzinfo=UTC)
    )

    assert fresh is False


# --- connector-event -> ledger adapter ------------------------------------------


def test_market_snapshot_event_to_record_carries_best_bid_and_ask() -> None:
    """The adapter projects a market + book into a `MarketSnapshotRecorded`
    carrying the top-of-book best bid/ask, in pips (never a float).
    """
    from hedgekit.connector.models import OrderBookLevel, OrderBookSnapshot
    from hedgekit.ledger.events import MarketSnapshotRecorded
    from hedgekit.numeric import ContractCentis, PricePips
    from hedgekit.scheduler.loop import market_snapshot_event_to_record

    fetched_at = datetime(2026, 1, 1, tzinfo=UTC)
    book = OrderBookSnapshot(
        ticker=DEFAULT_MARKET_TICKER,
        yes_bids=(OrderBookLevel(PricePips(4500), ContractCentis(300)),),
        yes_asks=(OrderBookLevel(PricePips(4600), ContractCentis(200)),),
        fetched_at=fetched_at,
    )

    event = market_snapshot_event_to_record(
        ticker=DEFAULT_MARKET_TICKER, order_book=book, component="scheduler"
    )

    assert isinstance(event, MarketSnapshotRecorded)
    assert event.ticker == DEFAULT_MARKET_TICKER
    assert event.best_bid_pips == 4500
    assert event.best_ask_pips == 4600


def test_market_snapshot_event_to_record_handles_an_empty_book_side() -> None:
    """A one-sided (or empty) book projects `None` for the missing side, never
    a crash or a fabricated zero price.
    """
    from hedgekit.connector.models import OrderBookSnapshot
    from hedgekit.ledger.events import MarketSnapshotRecorded
    from hedgekit.scheduler.loop import market_snapshot_event_to_record

    fetched_at = datetime(2026, 1, 1, tzinfo=UTC)
    book = OrderBookSnapshot(
        ticker=DEFAULT_MARKET_TICKER, yes_bids=(), yes_asks=(), fetched_at=fetched_at
    )

    event = market_snapshot_event_to_record(
        ticker=DEFAULT_MARKET_TICKER, order_book=book, component="scheduler"
    )

    assert isinstance(event, MarketSnapshotRecorded)
    assert event.best_bid_pips is None
    assert event.best_ask_pips is None
