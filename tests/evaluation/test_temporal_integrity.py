"""Failing-first tests for temporal-integrity enforcement (issue #52, RED).

`windbreak.evaluation.temporal` does not exist yet. Every test below imports
its new symbols from that module as the FIRST statement inside the test body
(rather than at module scope) -- matching this package's established RED
convention in `test_resolution.py` and `test_baselines.py` -- so every test
collects independently and each fails on its own
`ModuleNotFoundError: No module named 'windbreak.evaluation.temporal'` rather
than one collection-time explosion that would hide which behaviors are
covered. The handful of tests that instead exercise only already-existing
production code (`run_evaluation`, `FixtureForecast`, `MetricSpec`) fail for a
different, equally legitimate reason: the current implementation does not yet
gate, ledger, or surface anything -- see each test's docstring.

Pins SPEC-EPIC_07's temporal-integrity choke point (issue #52): the
evaluation package must reject, at ingestion, any forecast whose creation
postdates its market's resolution (BACKDATED) or predates system deployment
(PRE_DEPLOYMENT), and no unresolved market may enter a headline metric
(UNRESOLVED). Every rejection is LEDGERED as an immutable `RejectionEvent`,
never silently dropped. The temporal coordinate is exclusively the monotonic
integer `sequence_number` on the append-only ledger -- no wall-clock, no
floats, exact integer comparisons throughout.

Gate rules (exact, integer, fail-closed; checked in this order so exactly one
reason is ledgered per rejected record):

    1. `inputs.temporal is None` and forecasts non-empty -> raise `ValueError`
       (loud, never a silent skip); empty forecasts -> empty result, no raise.
    2. PRE_DEPLOYMENT: `created_sequence is None` OR
       `created_sequence <= deployment_sequence`.
    3. BACKDATED: the ticker has a resolution sequence `r` and
       `created_sequence >= r`.
    4. UNRESOLVED: the ticker has no entry in `inputs.resolutions`.

Test groups, matching the architect's numbering:

    1.  `resolution_sequences_from_events` folding semantics.
    2.  `deployment_sequence_from_fixture` folding semantics.
    3.  Gate happy path (all clean forecasts admitted).
    4.  BACKDATED boundary (`>=` vs `>`).
    5.  PRE_DEPLOYMENT boundary (`<=` vs `<`).
    6.  UNRESOLVED for an unlisted ticker.
    7.  Rejection-reason precedence and fixture-order preservation.
    8.  `created_sequence=None` fail-closed.
    9.  Idempotence of re-gating an already-admitted set.
    10. No skip/bypass flag; `RejectionReason`'s exact member set.
    11. `temporal=None` behavior, both directions.
    12. No `MetricSpec` (real or fake) can receive a leaked record.
    13. End-to-end `run_evaluation` over the leakage fixture.
    14. Tracer regression: the pre-#52 synthetic fixture is unaffected.
    15. `run_evaluation` on fixtures missing `mode_transitions` /
        `settlement_events`.

A short "bonus" section at the end directly pins `RejectionEvent`'s
`__post_init__` coherence guards, for extra mutation resistance beyond what
the architect's 15 groups exercise indirectly through the gate's own behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from windbreak.evaluation import (
    EvaluationInputs,
    FixtureForecast,
    MetricSpec,
    ObservationWindow,
    ResolutionOutcome,
    SettlementEvent,
    SettlementEventType,
    Track,
    run_evaluation,
)
from windbreak.numeric.types import ProbabilityPpm

if TYPE_CHECKING:
    from windbreak.evaluation.registry import EvaluationInputs as _EvaluationInputs

#: The epic-wide known-answer fixture shared by issues #49-#55; issue #52
#: additively pins its `created_sequence` / `mode_transitions` regression.
SYNTHETIC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_known_answer.json"
)
#: The issue #52 leakage fixture: five forecasts, three deliberately
#: temporally-invalid, over three settled tickers plus one never-resolved
#: ticker; see the fixture's own `description` key for the full hand
#: computation this suite pins against.
TEMPORAL_LEAKAGE_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "temporal_leakage.json"
)


def _forecast(
    forecast_id: str,
    market_ticker: str,
    *,
    created_sequence: int | None,
    probability_ppm: int = 500_000,
    baseline_executable_price_pips: int = 5_000,
) -> FixtureForecast:
    """Build a minimal `FixtureForecast` for a temporal-gate unit test.

    Every field irrelevant to temporal gating is pinned to an arbitrary but
    valid constant, so a test's construction reads as "only
    `created_sequence` (and `market_ticker`) matter here".

    Args:
        forecast_id: The forecast's identifier.
        market_ticker: The market the forecast names.
        created_sequence: The forecast's creation sequence number, or `None`
            for a forecast with no recorded provenance.
        probability_ppm: The forecast probability, in ppm.
        baseline_executable_price_pips: The baseline executable price, pips.

    Returns:
        The constructed `FixtureForecast`, carrying `created_sequence`.
    """
    return FixtureForecast(
        forecast_id=forecast_id,
        market_ticker=market_ticker,
        probability_ppm=ProbabilityPpm(probability_ppm),
        eligible_for_live=True,
        abstention_reason=None,
        traded=True,
        baseline_executable_price_pips=baseline_executable_price_pips,
        created_sequence=created_sequence,
    )


def _write_fixture(tmp_path: Path, payload: dict[str, object], *, name: str) -> Path:
    """Write a JSON fixture payload to a temp file and return its path.

    Args:
        tmp_path: pytest's per-test temporary directory.
        payload: The JSON-serializable fixture body.
        name: The file name to write within `tmp_path`.

    Returns:
        The path to the written fixture file.
    """
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


#: A minimal valid forecast entry shared by the group-15 malformed fixtures.
_MINIMAL_FORECAST_ENTRY = {
    "forecast_id": "fc-1",
    "market_ticker": "MKT-X",
    "probability_ppm": 500_000,
    "eligible_for_live": True,
    "abstention_reason": None,
    "traded": True,
    "baseline_executable_price_pips": 5_000,
    "created_sequence": 5,
}


# ---------------------------------------------------------------------------
# 1. resolution_sequences_from_events: first-ever-SETTLEMENT folding.
# ---------------------------------------------------------------------------


def test_resolution_sequences_from_events_pins_first_settlement_not_latest() -> None:
    """A ticker settled, reversed, and resettled to a DIFFERENT outcome keeps
    its FIRST settlement's `sequence_number` -- a later reversal/resettlement
    must never move the position, because the temporal answer ("when could
    this have been known") was fixed at first settlement, not at whichever
    settlement happens to be the final one.
    """
    from windbreak.evaluation.temporal import resolution_sequences_from_events

    events = (
        SettlementEvent(
            sequence_number=5,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-A",
            outcome=ResolutionOutcome.YES,
        ),
        SettlementEvent(
            sequence_number=6,
            event_type=SettlementEventType.SETTLEMENT_REVERSED,
            market_ticker="MKT-A",
            outcome=None,
        ),
        SettlementEvent(
            sequence_number=7,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-A",
            outcome=ResolutionOutcome.NO,
        ),
        SettlementEvent(
            sequence_number=8,
            event_type=SettlementEventType.SETTLEMENT,
            market_ticker="MKT-B",
            outcome=ResolutionOutcome.YES,
        ),
    )

    resolved = resolution_sequences_from_events(events)

    assert resolved == {"MKT-A": 5, "MKT-B": 8}


def test_resolution_sequences_from_events_empty_stream_yields_empty_mapping() -> None:
    """An empty settlement stream returns `{}` -- no ticker has a resolution
    sequence, and this is not an error condition.
    """
    from windbreak.evaluation.temporal import resolution_sequences_from_events

    assert resolution_sequences_from_events(()) == {}


# ---------------------------------------------------------------------------
# 2. deployment_sequence_from_fixture: mode_transitions folding.
# ---------------------------------------------------------------------------


def test_deployment_sequence_from_fixture_takes_the_min_of_multiple_transitions() -> (
    None
):
    """Multiple `mode_transitions` -> the deployment sequence is their MIN,
    not the first-listed or the max -- deployment is the earliest transition
    ever recorded, regardless of list order.
    """
    from windbreak.evaluation.temporal import deployment_sequence_from_fixture

    fixture = {
        "mode_transitions": [
            {"sequence_number": 25, "event_type": "mode_transition", "mode": "live"},
            {"sequence_number": 7, "event_type": "mode_transition", "mode": "paper"},
            {"sequence_number": 40, "event_type": "mode_transition", "mode": "shadow"},
        ]
    }

    assert deployment_sequence_from_fixture(fixture) == 7


def test_deployment_sequence_from_fixture_missing_key_raises_value_error() -> None:
    """A fixture with no `mode_transitions` key raises `ValueError` naming
    it -- there is no default deployment point to fall back to.
    """
    from windbreak.evaluation.temporal import deployment_sequence_from_fixture

    with pytest.raises(ValueError, match="mode_transitions"):
        deployment_sequence_from_fixture({})


def test_deployment_sequence_from_fixture_empty_list_raises_value_error() -> None:
    """An empty `mode_transitions` list raises `ValueError` naming it -- an
    evaluation run with no known deployment point can never gate anything.
    """
    from windbreak.evaluation.temporal import deployment_sequence_from_fixture

    with pytest.raises(ValueError, match="mode_transitions"):
        deployment_sequence_from_fixture({"mode_transitions": []})


def test_deployment_sequence_from_fixture_rejects_bool_sequence_number() -> None:
    """A `bool` `sequence_number` (an `int` subclass) raises `TypeError`, per
    the repo-wide "no bool-as-int" rule (see `windbreak.numeric.types._IntUnit`
    and `SettlementEvent`'s identical guard).
    """
    from windbreak.evaluation.temporal import deployment_sequence_from_fixture

    fixture = {
        "mode_transitions": [
            {"sequence_number": True, "event_type": "mode_transition", "mode": "paper"}
        ]
    }

    with pytest.raises(TypeError):
        deployment_sequence_from_fixture(fixture)


# ---------------------------------------------------------------------------
# 3. Gate happy path: every forecast is clean -> nothing rejected.
# ---------------------------------------------------------------------------


def test_enforce_temporal_integrity_admits_all_clean_forecasts() -> None:
    """When every forecast is created after deployment and before its
    market's resolution, and every market resolves, all forecasts are
    admitted (in fixture order) and the rejection ledger is empty.
    """
    from windbreak.evaluation.temporal import (
        TemporalContext,
        enforce_temporal_integrity,
    )

    forecasts = (
        _forecast("fc-1", "MKT-A", created_sequence=11),
        _forecast("fc-2", "MKT-B", created_sequence=12),
    )
    inputs = EvaluationInputs(
        forecasts=forecasts,
        resolutions={"MKT-A": ResolutionOutcome.YES, "MKT-B": ResolutionOutcome.NO},
        temporal=TemporalContext(
            deployment_sequence=10,
            resolution_sequences={"MKT-A": 100, "MKT-B": 101},
        ),
    )

    result = enforce_temporal_integrity(inputs)

    assert result.admitted_forecasts == forecasts
    assert result.rejections == ()


# ---------------------------------------------------------------------------
# 4. BACKDATED boundary: >= rejects (tie and after), one-before admits.
# ---------------------------------------------------------------------------


def test_backdated_boundary_tie_and_after_reject_one_before_admits() -> None:
    """`created_sequence == resolution_sequence` (a tie) and
    `created_sequence > resolution_sequence` both reject BACKDATED;
    `created_sequence == resolution_sequence - 1` admits -- this kills a
    `>=`-to-`>` mutant on the BACKDATED comparison.
    """
    from windbreak.evaluation.temporal import (
        RejectionReason,
        TemporalContext,
        enforce_temporal_integrity,
    )

    context = TemporalContext(deployment_sequence=1, resolution_sequences={"MKT-A": 50})

    def _run(created_sequence: int) -> object:
        forecast = _forecast("fc-x", "MKT-A", created_sequence=created_sequence)
        return enforce_temporal_integrity(
            EvaluationInputs(
                forecasts=(forecast,),
                resolutions={"MKT-A": ResolutionOutcome.YES},
                temporal=context,
            )
        )

    tie_result = _run(50)
    after_result = _run(51)
    before_result = _run(49)

    assert tie_result.admitted_forecasts == ()
    assert tie_result.rejections[0].reason is RejectionReason.BACKDATED
    assert after_result.admitted_forecasts == ()
    assert after_result.rejections[0].reason is RejectionReason.BACKDATED
    assert len(before_result.admitted_forecasts) == 1
    assert before_result.admitted_forecasts[0].forecast_id == "fc-x"
    assert before_result.rejections == ()


# ---------------------------------------------------------------------------
# 5. PRE_DEPLOYMENT boundary: <= rejects (tie and before), one-after admits.
# ---------------------------------------------------------------------------


def test_pre_deployment_boundary_tie_and_before_reject_one_after_admits() -> None:
    """`created_sequence == deployment_sequence` (a tie) and
    `created_sequence < deployment_sequence` both reject PRE_DEPLOYMENT;
    `created_sequence == deployment_sequence + 1` admits -- this kills a
    `<=`-to-`<` mutant on the PRE_DEPLOYMENT comparison.
    """
    from windbreak.evaluation.temporal import (
        RejectionReason,
        TemporalContext,
        enforce_temporal_integrity,
    )

    context = TemporalContext(
        deployment_sequence=100, resolution_sequences={"MKT-A": 500}
    )

    def _run(created_sequence: int) -> object:
        forecast = _forecast("fc-x", "MKT-A", created_sequence=created_sequence)
        return enforce_temporal_integrity(
            EvaluationInputs(
                forecasts=(forecast,),
                resolutions={"MKT-A": ResolutionOutcome.YES},
                temporal=context,
            )
        )

    tie_result = _run(100)
    before_result = _run(99)
    after_result = _run(101)

    assert tie_result.admitted_forecasts == ()
    assert tie_result.rejections[0].reason is RejectionReason.PRE_DEPLOYMENT
    assert before_result.admitted_forecasts == ()
    assert before_result.rejections[0].reason is RejectionReason.PRE_DEPLOYMENT
    assert len(after_result.admitted_forecasts) == 1
    assert after_result.admitted_forecasts[0].forecast_id == "fc-x"
    assert after_result.rejections == ()


# ---------------------------------------------------------------------------
# 6. UNRESOLVED: ticker absent from inputs.resolutions.
# ---------------------------------------------------------------------------


def test_unresolved_ticker_not_in_resolutions_is_rejected() -> None:
    """A forecast whose `market_ticker` has no entry in `inputs.resolutions`
    is rejected UNRESOLVED, even though its `created_sequence` is otherwise
    perfectly clean relative to deployment.
    """
    from windbreak.evaluation.temporal import (
        RejectionReason,
        TemporalContext,
        enforce_temporal_integrity,
    )

    forecast = _forecast("fc-1", "MKT-GHOST", created_sequence=50)
    inputs = EvaluationInputs(
        forecasts=(forecast,),
        resolutions={},
        temporal=TemporalContext(deployment_sequence=10, resolution_sequences={}),
    )

    result = enforce_temporal_integrity(inputs)

    assert result.admitted_forecasts == ()
    assert len(result.rejections) == 1
    event = result.rejections[0]
    assert event.reason is RejectionReason.UNRESOLVED
    assert event.resolution_sequence is None


def test_ticker_in_resolution_sequences_but_not_resolutions_ledgers_unresolved() -> (
    None
):
    """A ticker present in the settlement-derived `resolution_sequences` map
    but ABSENT from the static `inputs.resolutions` block, with a forecast
    created strictly between deployment and that settlement sequence, must
    still be ledgered as an ordinary UNRESOLVED rejection -- not crash.

    `_classify` decides UNRESOLVED purely from `inputs.resolutions` (this
    ticker is absent there, and the forecast is neither PRE_DEPLOYMENT nor
    BACKDATED relative to `deployment_sequence=10` and the would-be
    `resolution_sequence=100`), while `_build_rejection` unconditionally
    looks the SAME ticker up in the independent `resolution_sequences` map
    and finds a non-`None` value. `RejectionEvent.__post_init__` correctly
    refuses to construct an UNRESOLVED event that carries a
    `resolution_sequence` -- so today this choke point raises `ValueError`
    instead of ledgering the rejection, violating the invariant that every
    rejection is ledgered, never thrown. This pins that the gate must
    ledger a plain UNRESOLVED event with `resolution_sequence=None`
    regardless of what the settlement-derived map happens to separately
    know about this ticker.
    """
    from windbreak.evaluation.temporal import (
        RejectionReason,
        TemporalContext,
        enforce_temporal_integrity,
    )

    forecast = _forecast("fc-divergent", "MKT-GHOST", created_sequence=50)
    inputs = EvaluationInputs(
        forecasts=(forecast,),
        resolutions={},  # MKT-GHOST deliberately absent from static resolutions.
        temporal=TemporalContext(
            deployment_sequence=10,
            resolution_sequences={"MKT-GHOST": 100},  # known to the settlement map.
        ),
    )

    result = enforce_temporal_integrity(inputs)

    assert result.admitted_forecasts == ()
    assert len(result.rejections) == 1
    event = result.rejections[0]
    assert event.reason is RejectionReason.UNRESOLVED
    assert event.resolution_sequence is None
    assert event.forecast_id == "fc-divergent"
    assert event.market_ticker == "MKT-GHOST"
    assert event.created_sequence == 50
    assert event.deployment_sequence == 10


# ---------------------------------------------------------------------------
# 7. Precedence: PRE_DEPLOYMENT > BACKDATED > UNRESOLVED; fixture order kept.
# ---------------------------------------------------------------------------


def test_precedence_pre_deployment_wins_when_all_three_reasons_would_apply() -> None:
    """A pathological record simultaneously satisfies PRE_DEPLOYMENT
    (`created_sequence <= deployment_sequence`), would-be BACKDATED (its
    ticker's known `resolution_sequence <= created_sequence`), and would-be
    UNRESOLVED (its ticker is absent from `inputs.resolutions`, even though a
    `resolution_sequence` is known for it from the settlement stream) --
    exactly ONE event is ledgered, and it is PRE_DEPLOYMENT, the
    first-checked reason.
    """
    from windbreak.evaluation.temporal import (
        RejectionReason,
        TemporalContext,
        enforce_temporal_integrity,
    )

    forecast = _forecast("fc-pathological", "MKT-GHOST", created_sequence=5)
    inputs = EvaluationInputs(
        forecasts=(forecast,),
        resolutions={},  # MKT-GHOST absent -> would-be UNRESOLVED.
        temporal=TemporalContext(
            deployment_sequence=10,  # created(5) <= deployment(10) -> PRE_DEPLOYMENT.
            resolution_sequences={"MKT-GHOST": 3},  # created(5) >= r(3) -> would-be
            # BACKDATED.
        ),
    )

    result = enforce_temporal_integrity(inputs)

    assert result.admitted_forecasts == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].reason is RejectionReason.PRE_DEPLOYMENT


def test_precedence_preserves_fixture_order_across_multiple_rejected_records() -> None:
    """Multiple rejected records land in the ledger in the same order they
    appear in `inputs.forecasts`, regardless of which reason each triggers.
    """
    from windbreak.evaluation.temporal import (
        RejectionReason,
        TemporalContext,
        enforce_temporal_integrity,
    )

    context = TemporalContext(
        deployment_sequence=10, resolution_sequences={"MKT-A": 20}
    )
    first = _forecast("fc-first-unresolved", "MKT-GHOST", created_sequence=15)
    second = _forecast("fc-second-backdated", "MKT-A", created_sequence=25)
    third = _forecast("fc-third-predeploy", "MKT-A", created_sequence=5)

    inputs = EvaluationInputs(
        forecasts=(first, second, third),
        resolutions={"MKT-A": ResolutionOutcome.YES},
        temporal=context,
    )

    result = enforce_temporal_integrity(inputs)

    assert result.admitted_forecasts == ()
    assert [event.forecast_id for event in result.rejections] == [
        "fc-first-unresolved",
        "fc-second-backdated",
        "fc-third-predeploy",
    ]
    assert [event.reason for event in result.rejections] == [
        RejectionReason.UNRESOLVED,
        RejectionReason.BACKDATED,
        RejectionReason.PRE_DEPLOYMENT,
    ]


# ---------------------------------------------------------------------------
# 8. created_sequence=None fails closed as PRE_DEPLOYMENT.
# ---------------------------------------------------------------------------


def test_none_created_sequence_is_rejected_pre_deployment_fail_closed() -> None:
    """A forecast with `created_sequence=None` under a real temporal context
    is rejected PRE_DEPLOYMENT -- missing provenance fails closed and is
    never silently admitted, matching the gate rule's explicit `is None OR`
    clause.
    """
    from windbreak.evaluation.temporal import (
        RejectionReason,
        TemporalContext,
        enforce_temporal_integrity,
    )

    forecast = _forecast("fc-no-provenance", "MKT-A", created_sequence=None)
    inputs = EvaluationInputs(
        forecasts=(forecast,),
        resolutions={"MKT-A": ResolutionOutcome.YES},
        temporal=TemporalContext(
            deployment_sequence=10, resolution_sequences={"MKT-A": 50}
        ),
    )

    result = enforce_temporal_integrity(inputs)

    assert result.admitted_forecasts == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].reason is RejectionReason.PRE_DEPLOYMENT
    assert result.rejections[0].created_sequence is None


# ---------------------------------------------------------------------------
# 9. Idempotence: re-gating an already-admitted set changes nothing.
# ---------------------------------------------------------------------------


def test_gating_the_admitted_output_again_is_idempotent() -> None:
    """Feeding the gate's own admitted output back through the gate a second
    time yields the identical admitted set and zero further rejections --
    the gate is a pure, idempotent filter, not a one-shot consuming
    transform.
    """
    from windbreak.evaluation.temporal import (
        TemporalContext,
        enforce_temporal_integrity,
    )

    context = TemporalContext(
        deployment_sequence=10, resolution_sequences={"MKT-A": 100, "MKT-B": 100}
    )
    forecasts = (
        _forecast("fc-1", "MKT-A", created_sequence=11),
        _forecast("fc-2", "MKT-B", created_sequence=12),
    )
    resolutions = {"MKT-A": ResolutionOutcome.YES, "MKT-B": ResolutionOutcome.NO}
    inputs = EvaluationInputs(
        forecasts=forecasts, resolutions=resolutions, temporal=context
    )

    first_pass = enforce_temporal_integrity(inputs)
    second_inputs = EvaluationInputs(
        forecasts=first_pass.admitted_forecasts,
        resolutions=resolutions,
        temporal=context,
    )
    second_pass = enforce_temporal_integrity(second_inputs)

    assert second_pass.admitted_forecasts == first_pass.admitted_forecasts
    assert second_pass.rejections == ()


# ---------------------------------------------------------------------------
# 10. No skip/bypass flag; RejectionReason's exact member set.
# ---------------------------------------------------------------------------


def test_enforce_temporal_integrity_has_exactly_one_required_no_default_parameter() -> (
    None
):
    """`enforce_temporal_integrity` takes exactly one parameter, with no
    default -- there is no skip/bypass flag anywhere in its signature; a
    caller cannot opt out of the gate.
    """
    import inspect

    from windbreak.evaluation.temporal import enforce_temporal_integrity

    signature = inspect.signature(enforce_temporal_integrity)
    parameters = list(signature.parameters.values())

    assert len(parameters) == 1
    assert parameters[0].default is inspect.Parameter.empty


def test_rejection_reason_has_exactly_the_three_documented_members() -> None:
    """`RejectionReason` has exactly `BACKDATED`, `PRE_DEPLOYMENT`, and
    `UNRESOLVED` -- no extra escape-hatch member (e.g. no `SKIPPED` or
    `IGNORED`) could ever exist to be silently selected instead.
    """
    from windbreak.evaluation.temporal import RejectionReason

    assert {member.name for member in RejectionReason} == {
        "BACKDATED",
        "PRE_DEPLOYMENT",
        "UNRESOLVED",
    }


# ---------------------------------------------------------------------------
# 11. inputs.temporal is None: raise on non-empty forecasts, empty is fine.
# ---------------------------------------------------------------------------


def test_temporal_none_with_forecasts_raises_value_error_never_silently_skips() -> None:
    """`inputs.temporal is None` together with a non-empty forecast tuple
    raises `ValueError` -- there is no silent skip of the gate when the
    caller simply forgot to supply a temporal context.
    """
    from windbreak.evaluation.temporal import enforce_temporal_integrity

    forecast = _forecast("fc-1", "MKT-A", created_sequence=11)
    inputs = EvaluationInputs(
        forecasts=(forecast,),
        resolutions={"MKT-A": ResolutionOutcome.YES},
        temporal=None,
    )

    with pytest.raises(ValueError):
        enforce_temporal_integrity(inputs)


def test_temporal_none_with_empty_forecasts_returns_empty_result_no_raise() -> None:
    """`inputs.temporal is None` together with an EMPTY forecast tuple
    returns an empty result rather than raising -- there is nothing to gate,
    so there is nothing to be loud about.
    """
    from windbreak.evaluation.temporal import enforce_temporal_integrity

    inputs = EvaluationInputs(forecasts=(), resolutions={}, temporal=None)

    result = enforce_temporal_integrity(inputs)

    assert result.admitted_forecasts == ()
    assert result.rejections == ()


# ---------------------------------------------------------------------------
# 12. No MetricSpec -- real or fake -- can ever receive a leaked record.
# ---------------------------------------------------------------------------


def test_no_metricspec_including_a_fake_one_can_receive_a_leaked_record() -> None:
    """A fake `MetricSpec` whose `compute` spies on every `forecast_id` it
    sees is wired through `MetricSpec.__post_init__`'s own choke point (not
    anything the test constructs itself): calling `spec.compute` on leaky
    inputs proves the spy NEVER observes a rejected `forecast_id`. There is no
    ungated call path any `MetricSpec` -- real registry metric or a fake one
    built fresh in a test -- can reach around.
    """
    from windbreak.evaluation.temporal import TemporalContext

    context = TemporalContext(
        deployment_sequence=10,
        resolution_sequences={"LEAK-A": 30, "LEAK-B": 31, "LEAK-C": 32},
    )
    forecasts = (
        _forecast("fc-clean-1", "LEAK-A", created_sequence=11, probability_ppm=900_000),
        _forecast("fc-clean-2", "LEAK-B", created_sequence=12, probability_ppm=100_000),
        _forecast(
            "fc-backdated", "LEAK-C", created_sequence=32, probability_ppm=1_000_000
        ),
        _forecast(
            "fc-predeploy", "LEAK-A", created_sequence=10, probability_ppm=800_000
        ),
        _forecast(
            "fc-unresolved", "LEAK-D", created_sequence=13, probability_ppm=700_000
        ),
    )
    leaky_inputs = EvaluationInputs(
        forecasts=forecasts,
        resolutions={
            "LEAK-A": ResolutionOutcome.YES,
            "LEAK-B": ResolutionOutcome.NO,
            "LEAK-C": ResolutionOutcome.YES,
        },
        temporal=context,
    )

    seen_forecast_ids: list[str] = []
    call_count = 0

    def _spy(spied_inputs: _EvaluationInputs) -> int:
        """Record every forecast_id the (fake) metric was handed.

        Args:
            spied_inputs: Whatever `EvaluationInputs` actually reached this
                callable -- the very thing under test.

        Returns:
            A dummy, valid `MetricValue` (`0`) so the wrapper's return-type
            contract is satisfied regardless of the wrapping's own logic.
        """
        nonlocal call_count
        call_count += 1
        seen_forecast_ids.extend(
            forecast.forecast_id for forecast in spied_inputs.forecasts
        )
        return 0

    spec = MetricSpec(
        name="fake_leak_probe",
        track=Track.FORECAST,
        window=ObservationWindow.LATEST_BEFORE_CLOSE,
        compute=_spy,
    )

    result = spec.compute(leaky_inputs)

    assert result == 0
    assert call_count == 1
    assert seen_forecast_ids == ["fc-clean-1", "fc-clean-2"]
    assert "fc-backdated" not in seen_forecast_ids
    assert "fc-predeploy" not in seen_forecast_ids
    assert "fc-unresolved" not in seen_forecast_ids


# ---------------------------------------------------------------------------
# 13. End-to-end: run_evaluation over the leakage fixture.
# ---------------------------------------------------------------------------


def test_run_evaluation_on_temporal_leakage_fixture_admits_only_clean_forecasts() -> (
    None
):
    """`run_evaluation` over `temporal_leakage.json` admits only the two
    clean forecasts into the headline `brier` metric (`10_000` ppm -- see
    the derivation below), ledgers exactly the three expected rejection
    events in fixture order with the exact sequence numbers the architect
    hand-derived, and renders the ledger's event-type token in the report
    text.

    Hand computation (mirrors the fixture's own `description` key): only
    fc-clean-1 (LEAK-A, p=900_000 ppm, outcome=yes) and fc-clean-2 (LEAK-B,
    p=100_000 ppm, outcome=no) are admitted.

        term1 = (900_000 - 1_000_000) ** 2 = (-100_000) ** 2 = 10_000_000_000
        term2 = (100_000 - 0) ** 2         = 100_000 ** 2    = 10_000_000_000
        sum   = 20_000_000_000
        mean_ppm = sum // (2 * 1_000_000) = 20_000_000_000 // 2_000_000 = 10_000
    """
    from windbreak.evaluation.temporal import (
        EVALUATION_RECORD_REJECTED,
        RejectionReason,
    )

    report = run_evaluation(fixture_path=TEMPORAL_LEAKAGE_FIXTURE)

    forecast_track = next(
        track for track in report.tracks if track.name == Track.FORECAST.value
    )
    brier_result = next(
        metric for metric in forecast_track.metrics if metric.name == "brier"
    )
    assert brier_result.value == 10_000

    assert len(report.rejections) == 3
    backdated, predeploy, unresolved = report.rejections

    assert backdated.forecast_id == "fc-backdated"
    assert backdated.reason is RejectionReason.BACKDATED
    assert backdated.created_sequence == 32
    assert backdated.resolution_sequence == 32
    assert backdated.deployment_sequence == 10

    assert predeploy.forecast_id == "fc-predeploy"
    assert predeploy.reason is RejectionReason.PRE_DEPLOYMENT
    assert predeploy.created_sequence == 10
    assert predeploy.deployment_sequence == 10
    # LEAK-A's market IS resolved (at sequence 30); fc-predeploy is rejected
    # for pre-deployment, not for being unresolved, so this field is still
    # populated with the known resolution sequence.
    assert predeploy.resolution_sequence == 30

    assert unresolved.forecast_id == "fc-unresolved"
    assert unresolved.reason is RejectionReason.UNRESOLVED
    assert unresolved.created_sequence == 13
    assert unresolved.resolution_sequence is None

    text = report.render_text()
    assert EVALUATION_RECORD_REJECTED in text


# ---------------------------------------------------------------------------
# 14. Tracer regression: the pre-#52 synthetic fixture is unaffected.
# ---------------------------------------------------------------------------


def test_run_evaluation_on_synthetic_fixture_still_yields_zero_rejections() -> None:
    """The pre-existing synthetic known-answer fixture, now additively
    carrying `created_sequence` (8..17) and a `mode_transitions` deployment
    at sequence 7, still admits every forecast: no MKT-* market ever appears
    in `settlement_events` (only KXEXAMPLE-26-T1..T3 do), so BACKDATED is
    vacuous for all ten; every `created_sequence` (8..17) exceeds the
    deployment sequence (7), so none is PRE_DEPLOYMENT; every MKT-* ticker is
    present in the static `resolutions` block, so none is UNRESOLVED. The
    headline Brier stays at the pre-#52 hand-computed `78_000` and the
    render carries no rejections section.
    """
    report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)

    forecast_track = next(
        track for track in report.tracks if track.name == Track.FORECAST.value
    )
    brier_result = next(
        metric for metric in forecast_track.metrics if metric.name == "brier"
    )

    assert brier_result.value == 78_000
    assert report.rejections == ()
    assert "== rejections ==" not in report.render_text()


# ---------------------------------------------------------------------------
# 15. run_evaluation on fixtures missing mode_transitions / settlement_events.
# ---------------------------------------------------------------------------


def test_run_evaluation_missing_mode_transitions_raises_value_error(
    tmp_path: Path,
) -> None:
    """A fixture carrying `settlement_events` but no `mode_transitions`
    block raises `ValueError` naming it -- there is no default deployment
    point to gate against.
    """
    payload = {
        "forecasts": [_MINIMAL_FORECAST_ENTRY],
        "resolutions": [{"market_ticker": "MKT-X", "outcome": "yes"}],
        "settlement_events": [
            {
                "sequence_number": 1,
                "event_type": "settlement",
                "market_ticker": "MKT-X",
                "outcome": "yes",
            }
        ],
    }
    path = _write_fixture(tmp_path, payload, name="missing_mode_transitions.json")

    with pytest.raises(ValueError, match="mode_transitions"):
        run_evaluation(fixture_path=path)


def test_run_evaluation_missing_settlement_events_raises_value_error(
    tmp_path: Path,
) -> None:
    """A fixture carrying `mode_transitions` but no `settlement_events`
    block raises `ValueError` naming it -- there is no default
    resolution-sequence mapping to gate BACKDATED against.
    """
    payload = {
        "forecasts": [_MINIMAL_FORECAST_ENTRY],
        "resolutions": [{"market_ticker": "MKT-X", "outcome": "yes"}],
        "mode_transitions": [
            {"sequence_number": 1, "event_type": "mode_transition", "mode": "paper"}
        ],
    }
    path = _write_fixture(tmp_path, payload, name="missing_settlement_events.json")

    with pytest.raises(ValueError, match="settlement_events"):
        run_evaluation(fixture_path=path)


# ---------------------------------------------------------------------------
# Bonus: RejectionEvent's own __post_init__ coherence guards, pinned
# directly (beyond what the gate's behavior exercises indirectly above).
# ---------------------------------------------------------------------------


def test_rejection_event_requires_resolution_sequence_for_backdated() -> None:
    """Constructing a `RejectionEvent` with `reason=BACKDATED` and
    `resolution_sequence=None` raises `ValueError` -- BACKDATED is only
    coherent when a resolution sequence is actually known (that is the
    entire premise of the reason).
    """
    from windbreak.evaluation.temporal import RejectionEvent, RejectionReason

    with pytest.raises(ValueError, match="resolution_sequence"):
        RejectionEvent(
            forecast_id="fc-1",
            market_ticker="MKT-A",
            reason=RejectionReason.BACKDATED,
            created_sequence=50,
            deployment_sequence=10,
            resolution_sequence=None,
        )


def test_rejection_event_requires_no_resolution_sequence_for_unresolved() -> None:
    """Constructing a `RejectionEvent` with `reason=UNRESOLVED` and a
    non-`None` `resolution_sequence` raises `ValueError` -- an unresolved
    ticker cannot simultaneously carry a known resolution sequence.
    """
    from windbreak.evaluation.temporal import RejectionEvent, RejectionReason

    with pytest.raises(ValueError, match="resolution_sequence"):
        RejectionEvent(
            forecast_id="fc-1",
            market_ticker="MKT-A",
            reason=RejectionReason.UNRESOLVED,
            created_sequence=5,
            deployment_sequence=10,
            resolution_sequence=999,
        )


def test_rejection_event_type_field_is_not_a_constructor_parameter() -> None:
    """`RejectionEvent.event_type` always defaults to
    `EVALUATION_RECORD_REJECTED` and cannot be overridden at construction
    time (`init=False`) -- a caller can never forge a different event-type
    token onto a rejection record.
    """
    from windbreak.evaluation.temporal import (
        EVALUATION_RECORD_REJECTED,
        RejectionEvent,
        RejectionReason,
    )

    event = RejectionEvent(
        forecast_id="fc-1",
        market_ticker="MKT-A",
        reason=RejectionReason.UNRESOLVED,
        created_sequence=5,
        deployment_sequence=10,
        resolution_sequence=None,
    )

    assert event.event_type == EVALUATION_RECORD_REJECTED

    with pytest.raises(TypeError):
        RejectionEvent(
            forecast_id="fc-1",
            market_ticker="MKT-A",
            reason=RejectionReason.UNRESOLVED,
            created_sequence=5,
            deployment_sequence=10,
            resolution_sequence=None,
            event_type="something-else",
        )
