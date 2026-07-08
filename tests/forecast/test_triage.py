"""Tests for windbreak.forecast.triage (issue #23): two-stage triage + cost ledgering.

Pins the SPEC S8.4 two-stage triage contract: a cheap Stage-0 prior (`>=`
threshold, mutation-critical at the exact boundary), the STOP path that never
touches the expensive full pipeline, the PROCEED path where both stages'
costs accumulate, and a `TriageLedgerWriter` event trail that is
deterministic and carries exact-int payload leaves (never a float, per the
package-wide no-float convention `scripts/lint_no_floats.py` enforces).
`windbreak/forecast/triage.py` does not exist yet, so importing
`windbreak.forecast.triage` fails collection with `ModuleNotFoundError` --
the expected Gate 1 RED state for issue #23.

Three deliberate test-design choices, explained here because they shape every
test below:

Transport-reuse choice (`make_fake_vote_transport`)
    `tests/forecast/conftest.py`'s `make_fake_vote_transport` fixture is
    literally `FakeVoteTransport` itself (the fixture body is
    `return FakeVoteTransport`), so calling the injected factory with a
    custom `responses` tuple -- e.g. `make_fake_vote_transport(("520000",))`
    -- constructs a `FakeVoteTransport` seeded with that response, exactly as
    calling `FakeVoteTransport(("520000",))` directly would. This lets every
    triage test reuse the one conftest-provided double (a cheap-model Stage-0
    response is modeled as a bare integer ppm string) without inventing a new
    transport class or reaching into `tests.forecast.conftest` via a
    cross-module import that the project's rootdir-relative pytest import
    mode does not support.

Call-counting choice (`_CountingTransport`)
    `FakeVoteTransport` tracks its own call count on a private attribute, so
    a thin, local `_CountingTransport` wrapper (mirroring `test_cassettes.py`'s
    `_FakeTransport` pattern) is used wherever a test must assert Stage-0
    calls `transport.complete` *exactly once* -- a public, typed seam rather
    than reaching into the fake's internals.

Boundary-testing choice (the `>=` gate)
    `should_run_full_pipeline` gates on `abs(prior_ppm - baseline_ppm) >=
    triage_threshold_ppm`. A prior exactly `triage_threshold_ppm` away from
    baseline must proceed (not stop) -- the single ppm below that must not.
    Both edges are asserted explicitly below because a `>` vs `>=` mutant is
    otherwise invisible to any test that only exercises the interior of each
    region.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from windbreak.forecast.cassettes import (
    ForbiddenLiveTransport,
    RecordingCassette,
    ReplayCassette,
)
from windbreak.forecast.records import forecast_record_to_payload
from windbreak.forecast.triage import (
    PER_FORECAST_BUDGET_MICROS,
    TRIAGE_PROCEED_EVENT,
    TRIAGE_STOP_EVENT,
    TRIAGE_THRESHOLD_PPM,
    InMemoryTriageLedger,
    TriagePrior,
    run_stage0_prior,
    run_triaged_pipeline,
    should_run_full_pipeline,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from windbreak.connector.models import NormalizedMarket
    from windbreak.forecast.cassettes import LlmRequest, LlmTransport
    from windbreak.forecast.records import BaselineQuoteSnapshot
    from windbreak.forecast.sandbox import ResearchTools

    #: See the module docstring's "Transport-reuse choice" note: the
    #: conftest-provided factory *is* `FakeVoteTransport`, callable with an
    #: optional custom `responses` tuple. Typed by its structural result
    #: (`LlmTransport`) rather than `object` so it composes with helpers here
    #: (e.g. `_CountingTransport`) that are themselves typed against the seam.
    FakeVoteTransportFactory = Callable[..., LlmTransport]

#: The `baseline` fixture's price (4500 pips) converted at the package's
#: fixed 100x pips-to-ppm factor (see `windbreak/forecast/pipeline.py`'s
#: `_baseline_probability_ppm`) -- named here so every gating assertion below
#: reads against the same, single source of truth.
_BASELINE_PPM = 450_000


class _CountingTransport:
    """A thin `LlmTransport` wrapper counting `complete` calls, for assertion.

    See the module docstring's "Call-counting choice" note for why this
    exists instead of reaching into `FakeVoteTransport`'s private counter.
    """

    def __init__(self, transport: LlmTransport) -> None:
        """Store the delegate transport and reset the call counter.

        Args:
            transport: The underlying transport to delegate every call to.
        """
        self._transport = transport
        self.call_count = 0

    def complete(self, request: LlmRequest) -> str:
        """Record one call, then delegate to the wrapped transport.

        Args:
            request: The completion request to forward unchanged.

        Returns:
            The wrapped transport's response.
        """
        self.call_count += 1
        return self._transport.complete(request)


# --- Constants: SPEC-mandated exact values ---------------------------------------


def test_triage_threshold_and_budget_constants_have_expected_values() -> None:
    """Pin the SPEC S8.4 Stage-0 threshold and per-forecast budget constants."""
    assert TRIAGE_THRESHOLD_PPM == 50_000
    assert PER_FORECAST_BUDGET_MICROS == 3_000_000


# --- Stage-0 prior: parsing, cost, and the single-call contract ------------------


def test_stage0_prior_parses_integer_response_into_prior(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    make_fake_vote_transport: FakeVoteTransportFactory,
) -> None:
    """A bare-integer Stage-0 response becomes an exact, cheap `TriagePrior`."""
    transport = _CountingTransport(make_fake_vote_transport(("520000",)))

    prior = run_stage0_prior(market, baseline, transport=transport)

    assert prior == TriagePrior(prior_ppm=520_000, cost_micros=60_000)
    assert 0 < prior.cost_micros <= PER_FORECAST_BUDGET_MICROS // 50
    assert transport.call_count == 1


@pytest.mark.parametrize("bad_response", ["0.52", "maybe"])
def test_stage0_prior_non_integer_response_raises_value_error(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    make_fake_vote_transport: FakeVoteTransportFactory,
    bad_response: str,
) -> None:
    """A non-integer Stage-0 response fails loudly rather than silently defaulting."""
    transport = make_fake_vote_transport((bad_response,))

    with pytest.raises(ValueError):
        run_stage0_prior(market, baseline, transport=transport)


@pytest.mark.parametrize("bad_response", ["1000001", "-1"])
def test_stage0_prior_out_of_range_response_raises_value_error(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    make_fake_vote_transport: FakeVoteTransportFactory,
    bad_response: str,
) -> None:
    """A Stage-0 response outside `[0, 1_000_000]` fails loudly."""
    transport = make_fake_vote_transport((bad_response,))

    with pytest.raises(ValueError):
        run_stage0_prior(market, baseline, transport=transport)


@pytest.mark.parametrize("response", ["0", "1000000"])
def test_stage0_prior_accepts_inclusive_range_edges(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    make_fake_vote_transport: FakeVoteTransportFactory,
    response: str,
) -> None:
    """The inclusive `[0, 1_000_000]` range edges both parse successfully.

    A `_MIN_PRIOR_PPM <= value` or `value <= _MAX_PRIOR_PPM` mutant that
    tightens either bound to `<` would reject one of these two edges.
    """
    transport = make_fake_vote_transport((response,))

    prior = run_stage0_prior(market, baseline, transport=transport)

    assert prior.prior_ppm == int(response)


# --- should_run_full_pipeline: gating boundaries (mutation-critical `>=`) --------


@pytest.mark.parametrize(
    ("prior_ppm", "operator_flagged", "refresh_triggered", "expected"),
    [
        (499_999, False, False, False),  # diff 49_999 < threshold: stop.
        (500_000, False, False, True),  # diff exactly 50_000: the `>=` edge.
        (400_000, False, False, True),  # symmetric high-side diff of 50_000.
        (460_000, True, False, True),  # below threshold, but operator-flagged.
        (460_000, False, True, True),  # below threshold, but refresh-triggered.
    ],
)
def test_should_run_full_pipeline_boundaries(
    prior_ppm: int,
    operator_flagged: bool,
    refresh_triggered: bool,
    expected: bool,
) -> None:
    """The pure gate matches `>=` threshold, operator-flag, and refresh cases."""
    result = should_run_full_pipeline(
        prior_ppm,
        _BASELINE_PPM,
        triage_threshold_ppm=TRIAGE_THRESHOLD_PPM,
        operator_flagged=operator_flagged,
        refresh_triggered=refresh_triggered,
    )

    assert result is expected


# --- STOP path: end-to-end, never touching the full pipeline --------------------


def test_stop_path_never_runs_full_pipeline_and_records_triage_only(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """A within-band prior stops before the full pipeline and ledgers a STOP.

    `full_transport=ForbiddenLiveTransport()` completing *without* raising
    `LiveCallForbiddenError` is itself the structural proof the expensive
    pipeline never ran.
    """
    ledger = InMemoryTriageLedger()

    record = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("460000",)),
        full_transport=ForbiddenLiveTransport(),
        ledger=ledger,
        created_at=created_at,
        research_tools=research_tools,
    )

    assert record.triage_stage == "triage_only"
    assert record.eligible_for_live is False
    assert record.research_cost_micros == 60_000
    assert record.model_votes == ()
    assert record.citations == ()
    assert record.probability_ppm == 460_000
    assert record.ci_low_ppm == 460_000
    assert record.ci_high_ppm == 460_000

    stop_events = ledger.events_by_type(TRIAGE_STOP_EVENT)
    assert len(stop_events) == 1
    assert ledger.events_by_type(TRIAGE_PROCEED_EVENT) == ()
    payload = stop_events[0].payload
    assert payload["market_ticker"] == market.ticker
    assert payload["prior_ppm"] == 460_000
    assert payload["baseline_ppm"] == 450_000
    assert payload["triage_threshold_ppm"] == 50_000
    assert payload["operator_flagged"] is False
    assert payload["refresh_triggered"] is False
    assert payload["triage_cost_micros"] == 60_000
    assert json.dumps(payload)
    assert all(isinstance(v, int | str | bool) for v in payload.values())


# --- PROCEED path: end-to-end, both stages' costs accumulate ---------------------


def test_proceed_path_runs_full_pipeline_and_accumulates_both_costs(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """A far-from-baseline prior proceeds to the full pipeline and ledgers a PROCEED."""
    ledger = InMemoryTriageLedger()

    record = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("600000",)),
        full_transport=make_fake_vote_transport(),
        ledger=ledger,
        created_at=created_at,
        research_tools=research_tools,
    )

    assert record.triage_stage == "full"
    assert record.eligible_for_live is True
    assert record.research_cost_micros == 3_000_000 + 60_000
    assert len(record.model_votes) == 3

    proceed_events = ledger.events_by_type(TRIAGE_PROCEED_EVENT)
    assert len(proceed_events) == 1
    assert ledger.events_by_type(TRIAGE_STOP_EVENT) == ()
    payload = proceed_events[0].payload
    assert payload["market_ticker"] == market.ticker
    assert payload["operator_flagged"] is False
    assert payload["refresh_triggered"] is False
    assert payload["triage_cost_micros"] == 60_000
    assert payload["full_cost_micros"] == 3_000_000
    assert payload["total_research_cost_micros"] == 3_060_000
    assert json.dumps(payload)
    assert all(isinstance(v, int | str | bool) for v in payload.values())


def test_operator_flagged_forces_full_pipeline_despite_below_threshold_prior(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """`operator_flagged=True` forces the full pipeline inside the triage band."""
    ledger = InMemoryTriageLedger()

    record = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("460000",)),
        full_transport=make_fake_vote_transport(),
        ledger=ledger,
        created_at=created_at,
        operator_flagged=True,
        research_tools=research_tools,
    )

    assert record.triage_stage == "full"
    assert record.eligible_for_live is True
    proceed_events = ledger.events_by_type(TRIAGE_PROCEED_EVENT)
    assert proceed_events[0].payload["operator_flagged"] is True


def test_refresh_triggered_forces_full_pipeline_despite_below_threshold_prior(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """`refresh_triggered=True` forces the full pipeline inside the triage band."""
    ledger = InMemoryTriageLedger()

    record = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("460000",)),
        full_transport=make_fake_vote_transport(),
        ledger=ledger,
        created_at=created_at,
        refresh_triggered=True,
        research_tools=research_tools,
    )

    assert record.triage_stage == "full"
    assert record.eligible_for_live is True
    proceed_events = ledger.events_by_type(TRIAGE_PROCEED_EVENT)
    assert proceed_events[0].payload["refresh_triggered"] is True


# --- Determinism: identical inputs, fresh fakes and ledgers ----------------------


def test_run_triaged_pipeline_is_byte_deterministic_for_identical_inputs(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """Two proceed-path runs with fresh fakes/ledgers produce identical output."""
    ledger_a = InMemoryTriageLedger()
    ledger_b = InMemoryTriageLedger()

    record_a = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("600000",)),
        full_transport=make_fake_vote_transport(),
        ledger=ledger_a,
        created_at=created_at,
        research_tools=research_tools,
    )
    record_b = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("600000",)),
        full_transport=make_fake_vote_transport(),
        ledger=ledger_b,
        created_at=created_at,
        research_tools=research_tools,
    )

    assert record_a == record_b
    payload_a = json.dumps(forecast_record_to_payload(record_a), sort_keys=True)
    payload_b = json.dumps(forecast_record_to_payload(record_b), sort_keys=True)
    assert payload_a == payload_b

    for event_type in (TRIAGE_STOP_EVENT, TRIAGE_PROCEED_EVENT):
        assert ledger_a.events_by_type(event_type) == ledger_b.events_by_type(
            event_type
        )


def test_run_triaged_pipeline_stop_path_is_byte_deterministic_for_identical_inputs(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
) -> None:
    """Two stop-path runs with fresh fakes/ledgers produce identical output.

    The PROCEED-path determinism test above exercises `run_pipeline`'s own id
    derivation; `_triage_forecast_id`'s sha256-over-canonical-JSON id
    derivation is unique to the STOP path and would otherwise never be
    determinism-checked.
    """
    ledger_a = InMemoryTriageLedger()
    ledger_b = InMemoryTriageLedger()

    record_a = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("460000",)),
        full_transport=ForbiddenLiveTransport(),
        ledger=ledger_a,
        created_at=created_at,
        research_tools=research_tools,
    )
    record_b = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("460000",)),
        full_transport=ForbiddenLiveTransport(),
        ledger=ledger_b,
        created_at=created_at,
        research_tools=research_tools,
    )

    assert record_a == record_b
    payload_a = json.dumps(forecast_record_to_payload(record_a), sort_keys=True)
    payload_b = json.dumps(forecast_record_to_payload(record_b), sort_keys=True)
    assert payload_a == payload_b
    assert ledger_a.events_by_type(TRIAGE_STOP_EVENT) == ledger_b.events_by_type(
        TRIAGE_STOP_EVENT
    )


# --- Tracer invariant: triage composes with the cassette contract ---------------


def test_proceed_path_cassette_replay_matches_recording(
    market: NormalizedMarket,
    baseline: BaselineQuoteSnapshot,
    created_at: datetime,
    make_fake_vote_transport: FakeVoteTransportFactory,
    research_tools: ResearchTools,
    tmp_path: Path,
) -> None:
    """Recording `full_transport`, then replaying it, reproduces the same record.

    Proves triage's Stage-0 short-circuit does not break the full pipeline's
    existing record/replay cassette contract on the proceed path.
    """
    cassette_path = tmp_path / "votes.json"
    recorder = RecordingCassette(
        transport=make_fake_vote_transport(), path=cassette_path
    )
    recorded = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("600000",)),
        full_transport=recorder,
        ledger=InMemoryTriageLedger(),
        created_at=created_at,
        research_tools=research_tools,
    )

    replay = ReplayCassette.from_path(cassette_path)
    replayed = run_triaged_pipeline(
        market,
        baseline,
        triage_transport=make_fake_vote_transport(("600000",)),
        full_transport=replay,
        ledger=InMemoryTriageLedger(),
        created_at=created_at,
        research_tools=research_tools,
    )

    assert replayed == recorded
