"""End-to-end failing-first tests for the always-on PAPER loop (issue #48, RED).

`windbreak.scheduler.loop` does not exist yet, so every test below fails at
collection or call time with `ModuleNotFoundError: No module named
'windbreak.scheduler'` -- the expected Gate 1 RED state for issue #48.

Three scenarios, per the issue's own test-writing brief:

1. `test_real_kernel_tick_...` -- a full tick, wired with the *real*,
   unmodified `RiskKernel`/`ApprovalPipeline` (via `KernelApproval`), ledgers
   the full per-stage event sequence and never mints a token when the
   selector emits an intent (issue #110's three hard-veto stubs plus the
   `verification=None` reconciliation fail-closed). This test's hard,
   unconditional assertions are the ledger *structure* (every stage event
   fires, the chain verifies, and two runs are content-identical); its
   `IntentVetoed`-carries-the-stub-reasons assertion is *conditional* on the
   selector having emitted an intent at all. That condition is intentionally
   soft: whether the stock, unmodified forecast pipeline's fixed
   `research_cost_micros` (amortized against the selector's fixed 1-contract
   probe fill, `windbreak/selector/__init__.py::_PROBE_SIZE_CENTIS`) ever
   clears `net_edge_min` for *any* market/forecast combination is a real,
   open economic-modeling question this issue does not resolve -- orthogonal
   to what #48 composes. `tests/scheduler/test_loop.py`'s
   `test_kernel_approval_vetoes_before_minting_any_token` is this suite's
   *unconditional* proof of the load-bearing constraint (issue #110), built
   directly on the real kernel/pipeline with a hand-supplied intent, so the
   guarantee does not depend on this uncertainty.
2. `test_two_real_kernel_ticks_are_content_deterministic` -- the same tick,
   run twice over two independent `PaperTickDeps` (separate ledger paths,
   identical inputs and injected clock), ledgers byte-for-byte identical
   `(event_type, payload)` sequences.
3. `test_fill_leg_via_doubled_approval_seam_reaches_a_terminal_gateway_state`
   -- a test-level seam double standing in for `ApprovalSeam`, minting a
   *genuinely signed* `SignedApprovalToken` against `deps.verification_key`
   (the exact token-mint idiom `tests/order_gateway/conftest.py` already
   ships), proving the real `OrderGateway` -> `PaperExchange` -> `Reconciler`
   wiring `build_paper_deps` assembles -- not a production bypass, since the
   real Gateway still verifies the token's signature and the real exchange
   still fills the order.
4. `test_tracer_invariant_research_ceiling_produces_zero_paper_events` -- with
   `mode_ceiling: research` (even with every PAPER flag supplied), `windbreak
   run` never wires the PAPER loop at all: no ledger file is ever created,
   and the RESEARCH heartbeat output is unaffected.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.integration.conftest import (
    FIXED_NOW_EPOCH_S,
    ledger_path_for,
    read_event_type_payload_pairs,
)
from tests.order_gateway.conftest import issue_matching_token, make_intent

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from windbreak.config.schema import WindbreakConfig

#: The two `#110` stub veto reasons that must appear whenever the real kernel
#: vetoes an intent this loop's selector emits (see the module docstring's
#: "Load-bearing constraint" discussion).
_STUB_110_REASONS = (
    "blocked on #110 (exchange status feed)",
    "blocked on #110 (pipeline heartbeat)",
)

#: The always-present per-tick stage events, regardless of whether the
#: selector emitted a tradeable intent.
_ALWAYS_PRESENT_EVENT_TYPES = (
    "MarketSnapshotRecorded",
    "ForecastCreated",
    "SelectorDecisionRecorded",
    "ModeHeartbeat",
    "EquitySampled",
    "PositionsSnapshotRecorded",
)


def _fixed_clock() -> int:
    """Return the fixed, non-advancing epoch second every test in this module
    builds `PaperTickDeps` against, for cross-run determinism.
    """
    return FIXED_NOW_EPOCH_S


def _build_deps(
    *,
    books_dir: Path,
    cassette_path: Path,
    ledger_path: Path,
    report_dir: Path,
    config: WindbreakConfig,
    research_tools_factory,
):
    """Build one `PaperTickDeps` over the shared offline fixtures.

    Args:
        books_dir: The `deep_walk` books-fixture directory.
        cassette_path: The (empty) recorded-cassette path.
        ledger_path: Where the tick's `SqliteLedgerStore` is created.
        report_dir: Where weekly-report stubs would be written.
        config: The PAPER-ceilinged configuration.
        research_tools_factory: Builds the offline, no-candidate research
            tools double (`NullSearchTransport`).

    Returns:
        A fully wired `PaperTickDeps`.
    """
    from windbreak.scheduler.loop import build_paper_deps

    return build_paper_deps(
        books_dir=books_dir,
        cassette_path=cassette_path,
        ledger_path=ledger_path,
        report_dir=report_dir,
        config=config,
        research_tools=research_tools_factory(),
        clock=_fixed_clock,
    )


def test_real_kernel_tick_ledgers_full_stage_sequence(
    books_dir: Path,
    cassette_path: Path,
    report_dir: Path,
    paper_config: WindbreakConfig,
    research_tools_factory,
    tmp_path: Path,
) -> None:
    """One real-kernel tick ledgers every stage and its chain verifies.

    See the module docstring for why the `IntentVetoed`/stub-reasons
    assertion below is conditional.
    """
    deps = _build_deps(
        books_dir=books_dir,
        cassette_path=cassette_path,
        ledger_path=ledger_path_for(tmp_path),
        report_dir=report_dir,
        config=paper_config,
        research_tools_factory=research_tools_factory,
    )

    from windbreak.scheduler.loop import run_single_tick

    outcome = run_single_tick(deps, beat=1)

    assert outcome is not None
    deps.store.verify_chain()
    records = deps.store.read_all()
    event_types = [record.event_type for record in records]
    for expected in _ALWAYS_PRESENT_EVENT_TYPES:
        assert expected in event_types, f"missing {expected} in {event_types}"

    selector_record = next(
        record for record in records if record.event_type == "SelectorDecisionRecorded"
    )
    selector_payload = json.loads(selector_record.payload_json)["data"]
    if selector_payload["intent_count"] > 0:
        following = event_types[event_types.index("SelectorDecisionRecorded") + 1 :]
        assert "IntentVetoed" in following
        vetoed_record = next(
            record for record in records if record.event_type == "IntentVetoed"
        )
        reasons = json.loads(vetoed_record.payload_json)["data"]["reasons"]
        for stub_reason in _STUB_110_REASONS:
            assert stub_reason in reasons


def test_two_real_kernel_ticks_are_content_deterministic(
    books_dir: Path,
    cassette_path: Path,
    report_dir: Path,
    paper_config: WindbreakConfig,
    research_tools_factory,
    tmp_path: Path,
) -> None:
    """Two independent ticks over identical inputs ledger identical content."""
    from windbreak.scheduler.loop import run_single_tick

    deps_a = _build_deps(
        books_dir=books_dir,
        cassette_path=cassette_path,
        ledger_path=ledger_path_for(tmp_path, "ledger_a.db"),
        report_dir=report_dir,
        config=paper_config,
        research_tools_factory=research_tools_factory,
    )
    deps_b = _build_deps(
        books_dir=books_dir,
        cassette_path=cassette_path,
        ledger_path=ledger_path_for(tmp_path, "ledger_b.db"),
        report_dir=report_dir,
        config=paper_config,
        research_tools_factory=research_tools_factory,
    )

    run_single_tick(deps_a, beat=1)
    run_single_tick(deps_b, beat=1)

    pairs_a = read_event_type_payload_pairs(deps_a.store.read_all())
    pairs_b = read_event_type_payload_pairs(deps_b.store.read_all())
    assert pairs_a == pairs_b


def test_fill_leg_via_doubled_approval_seam_reaches_a_terminal_gateway_state(
    books_dir: Path,
    cassette_path: Path,
    report_dir: Path,
    paper_config: WindbreakConfig,
    research_tools_factory,
    tmp_path: Path,
) -> None:
    """A doubled `ApprovalSeam` proves Gateway -> PaperExchange -> Reconciler.

    Drives the real `deps.gateway`/`deps.reconciler`/`deps.exchange` directly
    with a hand-built intent (mirroring `tests/order_gateway/conftest.py`'s
    `make_intent`, sized to fully cross the `deep_walk` fixture's sole
    4600-pip/200-centis ask) and a *genuinely signed* token minted against
    `deps.verification_key`, rather than depending on `run_single_tick`'s own
    selector to organically emit a tradeable intent (see the module
    docstring's "Load-bearing constraint" discussion for why that would be a
    fragile, economics-dependent precondition for a wiring test that has
    nothing to do with selector economics).
    """
    import dataclasses

    from windbreak.order_gateway.gateway import SubmitOutcome
    from windbreak.riskkernel.checks import Decision
    from windbreak.riskkernel.reservations import ApprovalOutcome

    deps = _build_deps(
        books_dir=books_dir,
        cassette_path=cassette_path,
        ledger_path=ledger_path_for(tmp_path),
        report_dir=report_dir,
        config=paper_config,
        research_tools_factory=research_tools_factory,
    )

    # MKT-DEEP, price=4600, size=200 (centis): crosses the ask and partially
    # fills 50 centis off the resting top of the `deep_walk` book -- the exact
    # fill every Gateway-suite test over this fixture asserts (`ContractCentis(50)`
    # in e.g. `tests/order_gateway/test_gateway.py`).
    intent = make_intent()
    # Mint the token's `expires_at` against this module's fixed clock
    # (`FIXED_NOW_EPOCH_S`, the same instant `build_paper_deps` wires into the
    # gateway) rather than `issue_matching_token`'s Gateway-suite default
    # (`DEFAULT_EXPIRES_AT`, a different, earlier instant) -- otherwise the
    # gateway's clock is past the token's expiry and correctly rejects it EXPIRED.
    token = issue_matching_token(
        intent,
        key_material=deps.verification_key,
        expires_at=FIXED_NOW_EPOCH_S + 60,
    )

    class _FixedTokenApprovalSeam:
        """A test-only `ApprovalSeam` double minting a fixed, real token."""

        def decide(self, decided_intent: object, context: object) -> ApprovalOutcome:
            """Return an always-approving outcome carrying the fixed token.

            Args:
                decided_intent: Ignored; the fixed intent is used regardless.
                context: Ignored.

            Returns:
                An `ApprovalOutcome` with an empty-reasons, non-vetoing
                `Decision` and the pre-minted `token`.
            """
            del decided_intent, context
            return ApprovalOutcome(
                decision=Decision(vetoed=False, reasons=()), token=token
            )

    doubled_deps = dataclasses.replace(deps, approval=_FixedTokenApprovalSeam())

    outcome = doubled_deps.approval.decide(intent, object())
    assert outcome.token is not None
    gateway_result = doubled_deps.gateway.process_intent(intent, outcome.token)

    assert gateway_result.outcome is SubmitOutcome.ACKED
    assert gateway_result.ack is not None
    assert gateway_result.ack.filled.value == 50

    records = doubled_deps.store.read_all()
    transitions = [
        json.loads(record.payload_json)["data"]
        for record in records
        if record.event_type == "OrderTransitionLedgered"
    ]
    assert any(transition["to_state"] == "ACKED" for transition in transitions)

    reconcile_outcome = doubled_deps.reconciler.run_once()
    assert reconcile_outcome.halted is False

    from windbreak.scheduler.loop import run_single_tick

    run_single_tick(doubled_deps, beat=2)
    later_records = doubled_deps.store.read_all()
    positions_records = [
        json.loads(record.payload_json)["data"]
        for record in later_records
        if record.event_type == "PositionsSnapshotRecorded"
    ]
    assert positions_records, "expected at least one PositionsSnapshotRecorded"
    latest_positions = positions_records[-1]["positions"]
    matching = [
        position
        for position in latest_positions
        if position["ticker"] == intent.market_ticker
    ]
    assert matching, f"expected a MKT-DEEP position, got {latest_positions}"
    assert matching[0]["quantity_centis"] == 50


def test_tracer_invariant_research_ceiling_produces_zero_paper_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    books_dir: Path,
    cassette_path: Path,
    report_dir: Path,
) -> None:
    """`mode_ceiling: research` never wires the PAPER loop, even with every
    PAPER flag supplied: no ledger file is ever created and the RESEARCH
    heartbeat output is byte-for-byte unaffected by the four new flags.
    """
    from windbreak.main import main

    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode_ceiling: research\n", encoding="utf-8")
    ledger_path = ledger_path_for(tmp_path)

    exit_code = main(
        [
            "run",
            "--heartbeat-interval",
            "0",
            "--max-beats",
            "1",
            "--config",
            str(config_path),
            "--paper-books-dir",
            str(books_dir),
            "--cassette-path",
            str(cassette_path),
            "--ledger-path",
            str(ledger_path),
            "--report-dir",
            str(report_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "seq=1" in captured.err
    assert not ledger_path.exists(), (
        "PAPER loop must never open a ledger under RESEARCH"
    )
