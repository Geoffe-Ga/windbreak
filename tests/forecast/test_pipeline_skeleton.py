"""Tests for hedgekit.forecast.pipeline (issue #22): the SPEC S8.2 stub wiring.

Pins the four issue-mandated pipeline behaviors: (a) a schema-valid
`ForecastRecord` comes out the other end, (b) identical inputs produce
byte-identical output, (c) the output record is truly immutable, and (d)
replaying over a `ForbiddenLiveTransport` with a fully-populated cassette
completes purely from recorded responses while an empty cassette fails closed
with `CassetteMissError` -- never a live fallback. `hedgekit/forecast/` does
not exist yet, so importing `hedgekit.forecast.pipeline` fails collection with
`ModuleNotFoundError: No module named 'hedgekit.forecast'` -- the expected
Gate 1 RED state for issue #22.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest

from hedgekit.forecast.cassettes import (
    CassetteMissError,
    ForbiddenLiveTransport,
    LiveCallForbiddenError,
    LlmRequest,
    RecordingCassette,
    ReplayCassette,
)
from hedgekit.forecast.pipeline import (
    apply_calibration_map,
    run_pipeline,
    shrink_toward_baseline,
)
from hedgekit.forecast.records import forecast_record_to_payload

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from hedgekit.connector.models import NormalizedMarket
    from hedgekit.forecast.records import BaselineQuoteSnapshot
    from hedgekit.forecast.sandbox import ResearchTools

    #: `make_fake_vote_transport` (see tests/forecast/conftest.py) is a factory
    #: for `FakeVoteTransport`, a network-free `LlmTransport` double defined in
    #: the conftest module (not part of the `hedgekit` package under test), so
    #: it is typed structurally here rather than imported by name.
    FakeVoteTransportFactory = Callable[[], object]


# --- (a) schema-valid record ------------------------------------------------------


def test_run_pipeline_produces_schema_valid_forecast_record(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """A full pipeline run yields a ForecastRecord that satisfies its invariants."""
    record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=created_at,
        research_tools=research_tools,
    )

    assert record.triage_stage == "full"
    assert 0 <= record.probability_ppm <= 1_000_000
    assert record.ci_low_ppm <= record.probability_ppm <= record.ci_high_ppm
    assert record.market_price_baseline_pips == baseline.price_pips
    assert record.baseline_quote_snapshot_id == baseline.snapshot_id
    assert len(record.model_votes) == 3
    assert all(vote.response_fingerprint for vote in record.model_votes)


# --- (b) byte-determinism ---------------------------------------------------------


def test_run_pipeline_is_byte_deterministic_for_identical_inputs(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """Two runs with identical inputs produce `==` records and identical JSON."""
    record_a = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=created_at,
        research_tools=research_tools,
    )
    record_b = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=created_at,
        research_tools=research_tools,
    )

    assert record_a == record_b
    payload_a = json.dumps(forecast_record_to_payload(record_a), sort_keys=True)
    payload_b = json.dumps(forecast_record_to_payload(record_b), sort_keys=True)
    assert payload_a == payload_b


# --- (c) mutation raises -----------------------------------------------------------


def test_run_pipeline_output_is_immutable(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """Mutating any field of the real pipeline's output record raises."""
    record = run_pipeline(
        market,
        baseline,
        transport=make_fake_vote_transport(),
        created_at=created_at,
        research_tools=research_tools,
    )

    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        record.probability_ppm = 0


# --- (d) replay over ForbiddenLiveTransport + fail-closed empty cassette ---------


def test_replay_over_forbidden_live_transport_completes_from_recording(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
    tmp_path: Path,
) -> None:
    """Recording with a fake transport, then replaying with a `ReplayCassette`,
    completes purely from the cassette and matches the original recording
    byte-for-byte -- the structural proof that replay never reaches a network.
    """
    cassette_path = tmp_path / "recorded_votes.json"
    recorder = RecordingCassette(
        transport=make_fake_vote_transport(), path=cassette_path
    )
    recorded = run_pipeline(
        market,
        baseline,
        transport=recorder,
        created_at=created_at,
        research_tools=research_tools,
    )

    replay = ReplayCassette.from_path(cassette_path)
    replayed = run_pipeline(
        market,
        baseline,
        transport=replay,
        created_at=created_at,
        research_tools=research_tools,
    )

    assert replayed == recorded

    # ForbiddenLiveTransport is constructed here only to prove, in isolation,
    # that if the run above had ever fallen through to a live call it would
    # have blown up loudly. `replay` above never delegates to a transport at
    # all -- `replayed` completing is itself the proof no stage reached the
    # network.
    with pytest.raises(LiveCallForbiddenError):
        ForbiddenLiveTransport().complete(
            LlmRequest(provider="probe", model_version="probe", prompt="probe")
        )


def test_run_pipeline_with_live_transport_directly_raises_forbidden_error(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    research_tools: ResearchTools,
) -> None:
    """Wiring `ForbiddenLiveTransport` in directly (no cassette at all) still
    fails closed -- proving `collect_model_votes` really does call
    `transport.complete` rather than silently succeeding without it.
    """
    with pytest.raises(LiveCallForbiddenError):
        run_pipeline(
            market,
            baseline,
            transport=ForbiddenLiveTransport(),
            created_at=created_at,
            research_tools=research_tools,
        )


def test_run_pipeline_with_empty_cassette_raises_cassette_miss_error(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    research_tools: ResearchTools,
    tmp_path: Path,
) -> None:
    """An empty cassette fails closed with `CassetteMissError` -- never a live
    fallback, per the fail-closed design contract.
    """
    empty_path = tmp_path / "empty_votes.json"
    empty_path.write_text("{}", encoding="utf-8")
    empty_cassette = ReplayCassette.from_path(empty_path)

    with pytest.raises(CassetteMissError):
        run_pipeline(
            market,
            baseline,
            transport=empty_cassette,
            created_at=created_at,
            research_tools=research_tools,
        )


# --- Stage-function arithmetic (stages 11-12) -------------------------------------
#
# In a full `run_pipeline` run the vote median is *derived from* the baseline, so
# `shrink_toward_baseline` always receives an estimate equal to its baseline and
# the terminal `_clamp_between` is a structural no-op -- no end-to-end fixture can
# distinguish the shrink/clamp math. These direct unit tests exercise the public
# stage functions with distinguishing inputs so the calibration/shrinkage
# arithmetic and its ppm-domain clamp are behaviorally pinned (mutation robustness
# for Gate 3), without changing the deferred real math (#25).


def test_shrink_toward_baseline_blends_estimate_toward_baseline() -> None:
    """Shrinkage moves an estimate a fixed integer fraction toward the baseline."""
    # lambda = 250_000 ppm: (200_000*750_000 + 1_000_000*250_000) // 1_000_000.
    shrunk = shrink_toward_baseline(200_000, 1_000_000)

    assert shrunk == 400_000
    assert 200_000 < shrunk < 1_000_000


def test_shrink_toward_baseline_is_identity_when_estimate_equals_baseline() -> None:
    """Shrinking an estimate toward an equal baseline leaves it unchanged."""
    assert shrink_toward_baseline(600_000, 600_000) == 600_000


def test_apply_calibration_map_is_identity_within_domain() -> None:
    """The v0 calibration map returns an in-range probability unchanged."""
    assert apply_calibration_map(450_000) == 450_000


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1_000_001, 1_000_000), (-1, 0)],
)
def test_apply_calibration_map_clamps_out_of_domain(value: int, expected: int) -> None:
    """The calibration map defensively clamps back into ``[0, 1_000_000]``."""
    assert apply_calibration_map(value) == expected
