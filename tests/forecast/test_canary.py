"""Tests for windbreak.forecast.canary (issue #28): weekly canary set + drift gate.

Pins the SPEC S8.4/S8.6/S16 canary contract: pure drift scoring
(`score_canary_run`), a mutation-critical `>` (strict) tolerance boundary on
`CanaryGate.apply_run`, the alert/ledger side effects of a breach, the
`run_pipeline` integration that ANDs a drifted gate into `eligible_for_live`,
and the "ack restores only *new* records" invariant. `windbreak/forecast/canary.py`
does not exist yet, so importing it below fails collection with
`ModuleNotFoundError: No module named 'windbreak.forecast.canary'` -- the
expected Gate 1 RED state for issue #28.

Two deliberate test-design choices, explained here because they shape every
test below:

Transport-reuse choice (`make_fake_vote_transport`)
    `run_canary_set` issues one deterministic completion per canary question on
    a pinned canary model, parsing each response as a bare integer ppm string
    -- exactly the shape `tests/forecast/conftest.py`'s `make_fake_vote_transport`
    factory (itself `FakeVoteTransport`) already produces for `test_triage.py`'s
    Stage-0 prior. Reusing it here (rather than inventing a near-duplicate
    local transport) keeps this module's only local double the one seam that
    genuinely has no conftest analogue: the alert emitter.

Alert-emitter double (`RecordingAlertEmitter`)
    `CanaryAlertEmitter` (the `dispatch(alert_type, message) -> object` seam)
    has no conftest precedent, so a small local double records every call as a
    `(alert_type, message)` tuple -- enough to assert exactly-once dispatch and
    inspect the message, without depending on any real sink's delivery mechanics.
    A separate test proves a *real* `windbreak.alerts.AlertDispatcher` also
    satisfies the seam structurally, so the Protocol boundary itself is pinned.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from windbreak.alerts import AlertDispatcher, AlertType, LoggingLedgerWriter
from windbreak.forecast.canary import (
    CANARY_ACK_EVENT,
    CANARY_DRIFT_EVENT,
    CANARY_OK_EVENT,
    DEFAULT_CANARY_DRIFT_TOLERANCE_PPM,
    CanaryGate,
    CanaryQuestion,
    CanaryRunResult,
    InMemoryCanaryLedger,
    run_canary_set,
    score_canary_run,
)
from windbreak.forecast.pipeline import run_pipeline
from windbreak.forecast.records import forecast_record_to_payload

if TYPE_CHECKING:
    from collections.abc import Callable

    from windbreak.connector.models import NormalizedMarket
    from windbreak.forecast.records import BaselineQuoteSnapshot
    from windbreak.forecast.sandbox import ResearchTools

    FakeVoteTransportFactory = Callable[..., object]


class RecordingAlertEmitter:
    """A `CanaryAlertEmitter` double recording every dispatched call.

    Structurally satisfies `dispatch(alert_type, message) -> object` without
    depending on any real sink's delivery mechanics.
    """

    def __init__(self) -> None:
        """Initialize with an empty call log."""
        self.calls: list[tuple[object, str]] = []

    def dispatch(self, alert_type: object, message: str) -> object:
        """Record the call and return an opaque sentinel.

        Args:
            alert_type: The alert type dispatched.
            message: The alert body.

        Returns:
            A sentinel object; callers never inspect this seam's return value.
        """
        self.calls.append((alert_type, message))
        return object()


def _assert_json_safe_leaves(node: object) -> None:
    """Recursively assert every leaf of `node` is an int, str, or bool.

    Args:
        node: A ledgered payload node (mapping, sequence, or scalar leaf).
    """
    if isinstance(node, dict):
        for value in node.values():
            _assert_json_safe_leaves(value)
    elif isinstance(node, list | tuple):
        for item in node:
            _assert_json_safe_leaves(item)
    else:
        assert isinstance(node, int | str | bool), f"non-leaf payload value: {node!r}"
        assert type(node) is not float, f"float leaf found in payload: {node!r}"


# --- Constants --------------------------------------------------------------


def test_default_canary_drift_tolerance_ppm_constant() -> None:
    """`DEFAULT_CANARY_DRIFT_TOLERANCE_PPM` pins the SPEC S16 default of 50_000 ppm."""
    assert DEFAULT_CANARY_DRIFT_TOLERANCE_PPM == 50_000


def test_canary_event_constants_have_expected_values() -> None:
    """The three canary ledger event-type strings are pinned exactly."""
    assert CANARY_DRIFT_EVENT == "CANARY_DRIFT"
    assert CANARY_OK_EVENT == "CANARY_OK"
    assert CANARY_ACK_EVENT == "CANARY_ACK"


# --- score_canary_run: pure drift scoring ------------------------------------


def test_score_canary_run_exact_match_scores_zero_drift() -> None:
    """Every observed value equal to its reference yields a zero-drift result."""
    questions = (
        CanaryQuestion(question_id="q1", prompt="p1", reference_ppm=500_000),
        CanaryQuestion(question_id="q2", prompt="p2", reference_ppm=250_000),
    )
    observed = {"q1": 500_000, "q2": 250_000}

    result = score_canary_run(questions, observed)

    assert result.distances_ppm == {"q1": 0, "q2": 0}
    assert result.drift_score_ppm == 0


def test_score_canary_run_mixed_distances_picks_max_and_worst_id() -> None:
    """Distances are per-question abs diffs; the score is the max, id is the argmax."""
    questions = (
        CanaryQuestion(question_id="q1", prompt="p1", reference_ppm=500_000),
        CanaryQuestion(question_id="q2", prompt="p2", reference_ppm=250_000),
        CanaryQuestion(question_id="q3", prompt="p3", reference_ppm=100_000),
    )
    observed = {"q1": 510_000, "q2": 250_000, "q3": 40_000}

    result = score_canary_run(questions, observed)

    assert result.distances_ppm == {"q1": 10_000, "q2": 0, "q3": 60_000}
    assert result.drift_score_ppm == 60_000
    assert result.worst_question_id == "q3"


def test_score_canary_run_observed_id_mismatch_raises_value_error() -> None:
    """Observed ids that don't exactly match the question ids raise `ValueError`."""
    questions = (CanaryQuestion(question_id="q1", prompt="p1", reference_ppm=500_000),)
    observed = {"q2": 500_000}

    with pytest.raises(ValueError):
        score_canary_run(questions, observed)


# --- CanaryQuestion: construction validation ---------------------------------


def test_canary_question_empty_question_id_raises_value_error() -> None:
    """An empty `question_id` is rejected at construction."""
    with pytest.raises(ValueError, match="question_id"):
        CanaryQuestion(question_id="", prompt="p1", reference_ppm=500_000)


@pytest.mark.parametrize("reference_ppm", [-1, 1_000_001])
def test_canary_question_out_of_range_reference_ppm_raises_value_error(
    reference_ppm: int,
) -> None:
    """A `reference_ppm` outside `[0, 1_000_000]` is rejected at construction."""
    with pytest.raises(ValueError, match="reference_ppm"):
        CanaryQuestion(question_id="q1", prompt="p1", reference_ppm=reference_ppm)


# --- run_canary_set: transport-driven observation gathering ------------------


def test_run_canary_set_maps_question_ids_to_observed_ppm(
    make_fake_vote_transport: FakeVoteTransportFactory,
) -> None:
    """One deterministic call per question maps its id to the observed ppm."""
    questions = (
        CanaryQuestion(question_id="q1", prompt="p1", reference_ppm=500_000),
        CanaryQuestion(question_id="q2", prompt="p2", reference_ppm=250_000),
    )
    transport = make_fake_vote_transport(("510000", "260000"))

    observed = run_canary_set(questions, transport=transport)

    assert observed == {"q1": 510_000, "q2": 260_000}


@pytest.mark.parametrize("bad_response", ["0.5", "maybe"])
def test_run_canary_set_non_integer_response_raises_value_error(
    make_fake_vote_transport: FakeVoteTransportFactory,
    bad_response: str,
) -> None:
    """A non-integer canary response fails loudly, fail-closed."""
    questions = (CanaryQuestion(question_id="q1", prompt="p1", reference_ppm=500_000),)
    transport = make_fake_vote_transport((bad_response,))

    with pytest.raises(ValueError):
        run_canary_set(questions, transport=transport)


@pytest.mark.parametrize("bad_response", ["1000001", "-1"])
def test_run_canary_set_out_of_range_response_raises_value_error(
    make_fake_vote_transport: FakeVoteTransportFactory,
    bad_response: str,
) -> None:
    """An out-of-range canary response fails loudly, fail-closed."""
    questions = (CanaryQuestion(question_id="q1", prompt="p1", reference_ppm=500_000),)
    transport = make_fake_vote_transport((bad_response,))

    with pytest.raises(ValueError):
        run_canary_set(questions, transport=transport)


# --- CanaryGate.apply_run: mutation-critical strict tolerance boundary -------


def test_apply_run_at_exact_tolerance_is_ok_no_alert_no_state_change(
    created_at: datetime,
) -> None:
    """A drift score exactly at tolerance is within band: OK, no alert, gate open.

    The gate is STRICT (`>`, not `>=`): the boundary value itself must never
    breach, or a `>=` mutant would be invisible.
    """
    gate = CanaryGate(drift_tolerance_ppm=50_000)
    result = CanaryRunResult(
        distances_ppm={"q1": 50_000}, drift_score_ppm=50_000, worst_question_id="q1"
    )
    alerts = RecordingAlertEmitter()
    ledger = InMemoryCanaryLedger()

    breached = gate.apply_run(
        result, checked_at=created_at, alerts=alerts, ledger=ledger
    )

    assert breached is False
    assert alerts.calls == []
    assert len(ledger.events_by_type(CANARY_OK_EVENT)) == 1
    assert ledger.events_by_type(CANARY_DRIFT_EVENT) == ()
    assert gate.is_live_blocked(created_at=created_at) is False


def test_apply_run_one_ppm_over_tolerance_breaches(created_at: datetime) -> None:
    """One ppm over tolerance breaches: alert fires, gate becomes blocked.

    Paired with the exact-tolerance test above, this pins the `>` boundary from
    both sides -- a `>=` mutant on either edge is caught.
    """
    gate = CanaryGate(drift_tolerance_ppm=50_000)
    result = CanaryRunResult(
        distances_ppm={"q1": 50_001}, drift_score_ppm=50_001, worst_question_id="q1"
    )
    alerts = RecordingAlertEmitter()
    ledger = InMemoryCanaryLedger()

    breached = gate.apply_run(
        result, checked_at=created_at, alerts=alerts, ledger=ledger
    )

    assert breached is True
    assert len(alerts.calls) == 1
    assert len(ledger.events_by_type(CANARY_DRIFT_EVENT)) == 1
    assert ledger.events_by_type(CANARY_OK_EVENT) == ()
    assert gate.is_live_blocked(created_at=created_at) is True


def test_apply_run_breach_dispatches_one_alert_naming_worst_id_and_drift_ppm(
    created_at: datetime,
) -> None:
    """A breach dispatches exactly one `CANARY_DRIFT` alert naming the worst id
    and the drift ppm, and ledgers exactly one `CANARY_DRIFT` event whose
    payload leaves are all int/str/bool.
    """
    gate = CanaryGate()
    result = CanaryRunResult(
        distances_ppm={"q1": 10_000, "q2": 90_000},
        drift_score_ppm=90_000,
        worst_question_id="q2",
    )
    alerts = RecordingAlertEmitter()
    ledger = InMemoryCanaryLedger()

    gate.apply_run(result, checked_at=created_at, alerts=alerts, ledger=ledger)

    assert len(alerts.calls) == 1
    alert_type, message = alerts.calls[0]
    assert alert_type is AlertType.CANARY_DRIFT
    assert "q2" in message
    assert str(90_000) in message

    drift_events = ledger.events_by_type(CANARY_DRIFT_EVENT)
    assert len(drift_events) == 1
    _assert_json_safe_leaves(drift_events[0].payload)


def test_acknowledge_with_no_active_drift_raises_value_error(
    created_at: datetime,
) -> None:
    """Acking a gate with no active drift is a usage error."""
    gate = CanaryGate()

    with pytest.raises(ValueError):
        gate.acknowledge(acked_at=created_at, ledger=InMemoryCanaryLedger())


# --- run_pipeline integration: the gate ANDs into eligible_for_live ----------


def test_run_pipeline_with_drifted_canary_gate_is_ineligible_but_otherwise_equal(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """A drifted canary gate forces `eligible_for_live=False` on an otherwise
    fully-eligible run (3 verified citations); every other field is unchanged.
    """
    gate = CanaryGate()
    result = CanaryRunResult(
        distances_ppm={"q1": 100_000}, drift_score_ppm=100_000, worst_question_id="q1"
    )
    gate.apply_run(
        result,
        checked_at=created_at,
        alerts=RecordingAlertEmitter(),
        ledger=InMemoryCanaryLedger(),
    )

    gated_record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=created_at,
        research_tools=research_tools,
        canary_gate=gate,
    )
    ungated_record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=created_at,
        research_tools=research_tools,
    )

    assert gated_record.eligible_for_live is False
    assert ungated_record.eligible_for_live is True
    assert replace(gated_record, eligible_for_live=True) == ungated_record


def test_within_tolerance_apply_run_leaves_pipeline_byte_identical_to_no_gate(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """A within-tolerance `apply_run` changes nothing: byte-identical payload,
    zero alerts, and zero `CANARY_DRIFT` events.
    """
    gate = CanaryGate()
    result = CanaryRunResult(
        distances_ppm={"q1": 10_000}, drift_score_ppm=10_000, worst_question_id="q1"
    )
    alerts = RecordingAlertEmitter()
    ledger = InMemoryCanaryLedger()

    breached = gate.apply_run(
        result, checked_at=created_at, alerts=alerts, ledger=ledger
    )

    gated_record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=created_at,
        research_tools=research_tools,
        canary_gate=gate,
    )
    ungated_record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=created_at,
        research_tools=research_tools,
    )

    assert breached is False
    assert alerts.calls == []
    assert ledger.events_by_type(CANARY_DRIFT_EVENT) == ()
    gated_payload = json.dumps(forecast_record_to_payload(gated_record), sort_keys=True)
    ungated_payload = json.dumps(
        forecast_record_to_payload(ungated_record), sort_keys=True
    )
    assert gated_payload == ungated_payload


def test_ack_restores_eligibility_for_new_records_only(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """Acking a drift restores eligibility only for records created at/after the
    ack instant; a record already produced while blocked stays ineligible.
    """
    drift_at = datetime(2024, 12, 10, 0, 0, tzinfo=UTC)
    blocked_created_at = datetime(2024, 12, 10, 1, 0, tzinfo=UTC)
    ack_at = datetime(2024, 12, 10, 2, 0, tzinfo=UTC)
    restored_created_at = datetime(2024, 12, 10, 3, 0, tzinfo=UTC)
    gate = CanaryGate()
    ledger = InMemoryCanaryLedger()
    result = CanaryRunResult(
        distances_ppm={"q1": 100_000}, drift_score_ppm=100_000, worst_question_id="q1"
    )
    gate.apply_run(
        result, checked_at=drift_at, alerts=RecordingAlertEmitter(), ledger=ledger
    )

    blocked_record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=blocked_created_at,
        research_tools=research_tools,
        canary_gate=gate,
    )
    assert blocked_record.eligible_for_live is False
    assert gate.is_live_blocked(created_at=blocked_created_at) is True

    gate.acknowledge(acked_at=ack_at, ledger=ledger)
    assert len(ledger.events_by_type(CANARY_ACK_EVENT)) == 1

    restored_record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=restored_created_at,
        research_tools=research_tools,
        canary_gate=gate,
    )

    assert restored_record.eligible_for_live is True
    assert gate.is_live_blocked(created_at=blocked_created_at) is True
    assert blocked_record.eligible_for_live is False


# --- CanaryGate.is_live_blocked / acknowledge: re-arm and boundary regressions -


def test_second_unacknowledged_drift_after_ack_reblocks_live_eligibility(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """A fresh, unacknowledged breach after an ack RE-ARMS the block window.

    Regression for issue #28: acking a first drift must not leave the gate
    permanently open to a second, later, still-unacknowledged breach. Both the
    alert/ledger side (which already fire on every breach) and the
    live-eligibility side (which must re-block) are asserted together to pin
    the exact contradiction the bug produces.
    """
    first_drift_at = datetime(2024, 12, 10, 4, 0, tzinfo=UTC)
    ack_at = datetime(2024, 12, 10, 5, 0, tzinfo=UTC)
    second_drift_at = datetime(2024, 12, 10, 6, 0, tzinfo=UTC)
    new_record_at = datetime(2024, 12, 10, 7, 0, tzinfo=UTC)
    gate = CanaryGate()
    alerts = RecordingAlertEmitter()
    ledger = InMemoryCanaryLedger()
    result = CanaryRunResult(
        distances_ppm={"q1": 100_000}, drift_score_ppm=100_000, worst_question_id="q1"
    )

    first_breached = gate.apply_run(
        result, checked_at=first_drift_at, alerts=alerts, ledger=ledger
    )
    gate.acknowledge(acked_at=ack_at, ledger=ledger)
    second_breached = gate.apply_run(
        result, checked_at=second_drift_at, alerts=alerts, ledger=ledger
    )

    assert first_breached is True
    assert second_breached is True
    assert len(ledger.events_by_type(CANARY_DRIFT_EVENT)) == 2
    assert gate.is_live_blocked(created_at=new_record_at) is True

    record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=new_record_at,
        research_tools=research_tools,
        canary_gate=gate,
    )
    assert record.eligible_for_live is False


def test_is_live_blocked_before_drift_instant_is_not_blocked() -> None:
    """A record created strictly before the drift instant is never blocked."""
    before_drift = datetime(2024, 12, 10, 8, 0, tzinfo=UTC)
    drift_at = datetime(2024, 12, 10, 9, 0, tzinfo=UTC)
    gate = CanaryGate()
    result = CanaryRunResult(
        distances_ppm={"q1": 100_000}, drift_score_ppm=100_000, worst_question_id="q1"
    )

    gate.apply_run(
        result,
        checked_at=drift_at,
        alerts=RecordingAlertEmitter(),
        ledger=InMemoryCanaryLedger(),
    )

    assert gate.is_live_blocked(created_at=before_drift) is False


def test_is_live_blocked_at_exact_ack_instant_is_not_blocked() -> None:
    """A record created exactly at the ack instant counts as post-ack, not blocked."""
    drift_at = datetime(2024, 12, 10, 10, 0, tzinfo=UTC)
    ack_at = datetime(2024, 12, 10, 11, 0, tzinfo=UTC)
    gate = CanaryGate()
    ledger = InMemoryCanaryLedger()
    result = CanaryRunResult(
        distances_ppm={"q1": 100_000}, drift_score_ppm=100_000, worst_question_id="q1"
    )

    gate.apply_run(
        result, checked_at=drift_at, alerts=RecordingAlertEmitter(), ledger=ledger
    )
    gate.acknowledge(acked_at=ack_at, ledger=ledger)

    assert gate.is_live_blocked(created_at=ack_at) is False


def test_second_unacknowledged_breach_does_not_move_earliest_drift_window() -> None:
    """A later un-acked breach never pushes the block window forward.

    Kills the mutant that replaces the ``checked_at < self._drifted_at`` guard
    in ``_register_breach`` with an unconditional assignment: without an ack in
    between, the block window must stay pinned at the *earliest* breach instant.
    """
    earliest_drift_at = datetime(2024, 12, 10, 12, 30, tzinfo=UTC)
    later_drift_at = datetime(2024, 12, 10, 13, 0, tzinfo=UTC)
    gate = CanaryGate()
    alerts = RecordingAlertEmitter()
    ledger = InMemoryCanaryLedger()
    result = CanaryRunResult(
        distances_ppm={"q1": 100_000}, drift_score_ppm=100_000, worst_question_id="q1"
    )

    gate.apply_run(result, checked_at=earliest_drift_at, alerts=alerts, ledger=ledger)
    gate.apply_run(result, checked_at=later_drift_at, alerts=alerts, ledger=ledger)

    assert gate.is_live_blocked(created_at=earliest_drift_at) is True


# --- Structural: a real AlertDispatcher satisfies the seam -------------------


def test_real_alert_dispatcher_satisfies_canary_alert_emitter_protocol(
    created_at: datetime,
) -> None:
    """A real `AlertDispatcher(sinks=[], ...)` structurally satisfies the
    gate's `CanaryAlertEmitter` seam -- no fake required.
    """
    dispatcher = AlertDispatcher(sinks=[], ledger_writer=LoggingLedgerWriter())
    gate = CanaryGate()
    result = CanaryRunResult(
        distances_ppm={"q1": 100_000}, drift_score_ppm=100_000, worst_question_id="q1"
    )
    ledger = InMemoryCanaryLedger()

    breached = gate.apply_run(
        result, checked_at=created_at, alerts=dispatcher, ledger=ledger
    )

    assert breached is True
    assert len(ledger.events_by_type(CANARY_DRIFT_EVENT)) == 1
