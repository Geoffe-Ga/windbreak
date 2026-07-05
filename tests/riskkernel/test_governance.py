"""Failing-first tests for hedgekit.riskkernel.governance (issue #34, RED).

Issue #34 gives the Risk Kernel operator-facing floor governance (SPEC S5.1 /
S10.9-ish): an operator may *raise* the equity floor immediately (never a risk
increase), but *lowering* it goes through a two-step, unshortenable 48-hour
(``172_800``-second) cool-off plus a single-use nonce -- lowering can only ever
be requested from the CLI, never the dashboard. Independent of operator
action, the floor also *ratchets* upward automatically as equity makes new
highs (a fixed ppm share of each fresh gain, applied with no delay), and an
advisory (never blocking) alert fires once profit accumulated since the last
high-water mark crosses a configured threshold.

``hedgekit/riskkernel/governance.py`` does not exist yet, so every import
below fails collection with ``ModuleNotFoundError`` -- the expected Gate 1 RED
state for issue #34.

API-shape decisions pinned by this file (the implementation specialist must
build to these exactly, per the architect's instruction to keep the surface
in sync):

* ``FloorGovernance.__init__`` takes ``initial_floor``, ``ratchet_ppm``,
  ``profit_sweep_threshold``, ``mode_machine``, ``dispatcher``, ``writer``,
  ``clock``, and ``cool_off_seconds`` (default
  ``DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS == 172_800``) -- every test below
  passes them as keywords.
* The internal equity high-water mark independent of ``initial_floor`` starts
  at ``MoneyMicros(0)``; ``observe_equity`` only ratchets/advises on a
  strictly-higher observation (a "fresh HWM crossing"), computing
  ``gain = equity - previous_hwm``.
* Events are plain, string-discriminated ``hedgekit.ledger.events.Event``s
  (mirroring ``process.py``/``reservations.py``, not new dataclass
  subclasses): ``"FloorRaised"`` (``previous_floor_micros``,
  ``new_floor_micros``, ``origin``), ``"FloorLowerRequested"`` (``nonce``,
  ``target_floor_micros``, ``requested_at``, ``ready_at``, ``origin``),
  ``"FloorLowerRefused"`` (``reason`` one of ``"forbidden_origin"`` /
  ``"cool_off_active"`` / ``"nonce_mismatch"``, plus context fields),
  ``"FloorLowerConfirmed"`` (``previous_floor_micros``, ``new_floor_micros``),
  and ``"FloorRatchetApplied"`` (``previous_floor_micros``,
  ``new_floor_micros``, ``gain_micros``, ``increment_micros``).
* ``FloorGovernance.from_events`` is a classmethod taking the same
  configuration as ``__init__`` (minus ``initial_floor``, which -- like
  ``pending_lower`` -- is derived by replaying ``events``), plus the
  ``events`` iterable itself as the sole positional argument.
* ``request_lower``/``confirm_lower`` reuse ``AlertType.FLOOR_CHANGE_REQUEST``
  for both the request-time and the confirm-time alert (there is no separate
  SPEC S14 alert type for a *completed* lowering).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hedgekit.alerts import AlertDispatcher, AlertType, LoggingLedgerWriter
from hedgekit.numeric.types import MoneyMicros
from hedgekit.riskkernel.governance import (
    DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS,
    ChangeOrigin,
    CoolOffActiveError,
    FloorGovernance,
    ForbiddenOriginError,
    LoweringAlreadyPendingError,
    NonceMismatchError,
    NoPendingLowerError,
)
from hedgekit.riskkernel.modes import Mode, ModeStateMachine
from hedgekit.riskkernel.process import InMemoryKernelLedgerWriter

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hedgekit.alerts.registry import AlertSeverity
    from hedgekit.ledger.events import Event

#: A very large threshold/floor ceiling so a test that does not care about the
#: profit-sweep alert never accidentally trips it.
_NEVER_CROSSED = MoneyMicros(10**18)

#: The zero-money singleton, held module-level so it can serve as a default
#: argument without a per-call constructor (ruff B008).
_ZERO_MICROS = MoneyMicros(0)

#: `DEFAULT_NOW_EPOCH_S`-style fixed "current instant" every test's default
#: clock starts at, so cool-off arithmetic (`+ 172_800`) is easy to eyeball.
_DEFAULT_NOW = 1_700_000_000


class _Clock:
    """A mutable, injectable epoch-second clock (never `time.time`)."""

    def __init__(self, now: int) -> None:
        """Initialize the clock at `now`.

        Args:
            now: The starting epoch second.
        """
        self.now = now

    def __call__(self) -> int:
        """Return the current injected epoch second."""
        return self.now


@dataclasses.dataclass
class _SpyAlertSink:
    """A fake `AlertSink` that always succeeds and records every call."""

    name: str = "spy"
    calls: list[tuple[AlertType, AlertSeverity, str]] = dataclasses.field(
        default_factory=list
    )

    def send(
        self, alert_type: AlertType, severity: AlertSeverity, message: str
    ) -> None:
        """Record the call without raising.

        Args:
            alert_type: The kind of alert being delivered.
            severity: The alert's severity.
            message: The human-readable alert body.
        """
        self.calls.append((alert_type, severity, message))


@dataclasses.dataclass
class _Harness:
    """A fully-wired `FloorGovernance` plus handles onto its dependencies.

    Attributes:
        governance: The `FloorGovernance` under test.
        writer: The in-memory ledger writer it records events through.
        sink: The spy alert sink its dispatcher fans out to.
        clock: The mutable injected clock it reads.
        mode_machine: The mode state machine it demotes on a completed lower.
    """

    governance: FloorGovernance
    writer: InMemoryKernelLedgerWriter
    sink: _SpyAlertSink
    clock: _Clock
    mode_machine: ModeStateMachine


def _build(
    *,
    initial_floor: MoneyMicros = _ZERO_MICROS,
    ratchet_ppm: int = 0,
    profit_sweep_threshold: MoneyMicros = _NEVER_CROSSED,
    mode: Mode = Mode.LIVE,
    mode_ceiling: Mode = Mode.LIVE,
    now: int = _DEFAULT_NOW,
    cool_off_seconds: int = DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS,
) -> _Harness:
    """Build a fully-wired `FloorGovernance` harness with sane defaults.

    Args:
        initial_floor: The starting floor, in micros.
        ratchet_ppm: The profit-ratchet share, in parts per million.
        profit_sweep_threshold: The profit-sweep advisory threshold, in
            micros. Defaults to a value no test's gain ever reaches.
        mode: The mode machine's starting operating mode.
        mode_ceiling: The mode machine's ceiling.
        now: The clock's starting epoch second.
        cool_off_seconds: The floor-lower cool-off, in seconds.

    Returns:
        A `_Harness` wiring a real `FloorGovernance` to inspectable doubles.
    """
    writer = InMemoryKernelLedgerWriter()
    sink = _SpyAlertSink()
    dispatcher = AlertDispatcher([sink], ledger_writer=LoggingLedgerWriter())
    clock = _Clock(now)
    mode_machine = ModeStateMachine(mode_ceiling=mode_ceiling, mode=mode)
    governance = FloorGovernance(
        initial_floor=initial_floor,
        ratchet_ppm=ratchet_ppm,
        profit_sweep_threshold=profit_sweep_threshold,
        mode_machine=mode_machine,
        dispatcher=dispatcher,
        writer=writer,
        clock=clock,
        cool_off_seconds=cool_off_seconds,
    )
    return _Harness(
        governance=governance,
        writer=writer,
        sink=sink,
        clock=clock,
        mode_machine=mode_machine,
    )


def _replay(
    events: Iterable[Event],
    *,
    ratchet_ppm: int = 0,
    profit_sweep_threshold: MoneyMicros = _NEVER_CROSSED,
    mode: Mode = Mode.LIVE,
    mode_ceiling: Mode = Mode.LIVE,
    now: int = _DEFAULT_NOW,
    cool_off_seconds: int = DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS,
) -> FloorGovernance:
    """Rebuild a `FloorGovernance` from a replayed event history.

    Args:
        events: The event history to replay.
        ratchet_ppm: The profit-ratchet share, in parts per million.
        profit_sweep_threshold: The profit-sweep advisory threshold, in
            micros.
        mode: The rebuilt mode machine's starting operating mode.
        mode_ceiling: The rebuilt mode machine's ceiling.
        now: The rebuilt clock's starting epoch second.
        cool_off_seconds: The floor-lower cool-off, in seconds.

    Returns:
        A freshly rebuilt `FloorGovernance` (writing to a fresh, empty
        in-memory writer distinct from wherever `events` came from).
    """
    return FloorGovernance.from_events(
        events,
        ratchet_ppm=ratchet_ppm,
        profit_sweep_threshold=profit_sweep_threshold,
        mode_machine=ModeStateMachine(mode_ceiling=mode_ceiling, mode=mode),
        dispatcher=AlertDispatcher([], ledger_writer=LoggingLedgerWriter()),
        writer=InMemoryKernelLedgerWriter(),
        clock=_Clock(now),
        cool_off_seconds=cool_off_seconds,
    )


def _events_of_type(writer: InMemoryKernelLedgerWriter, event_type: str) -> list[Event]:
    """Return every recorded event of `event_type`, in recorded order.

    Args:
        writer: The in-memory writer to filter.
        event_type: The exact `Event.event_type` string to match.

    Returns:
        The matching events, in the order they were recorded.
    """
    return [event for event in writer.events if event.event_type == event_type]


# --- Sanity on the fixture constant -----------------------------------------------


def test_default_cool_off_seconds_constant_is_48_hours() -> None:
    """`DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS` is exactly 48 hours."""
    assert DEFAULT_FLOOR_LOWER_COOL_OFF_SECONDS == 172_800


# --- 1. raise_floor: applies immediately -------------------------------------------


def test_raise_floor_updates_current_floor_and_records_floor_raised() -> None:
    """Raising the floor takes effect immediately and records `FloorRaised`."""
    harness = _build(initial_floor=MoneyMicros(1_000_000))

    harness.governance.raise_floor(MoneyMicros(2_000_000))

    assert harness.governance.current_floor_micros == MoneyMicros(2_000_000)
    raised = _events_of_type(harness.writer, "FloorRaised")
    assert len(raised) == 1
    assert raised[0].payload["new_floor_micros"] == 2_000_000
    assert raised[0].payload["previous_floor_micros"] == 1_000_000


def test_raise_floor_to_the_current_floor_is_rejected_toward_request_lower() -> None:
    """Raising to a value equal to the current floor is not a raise at all;
    the error routes the caller toward `request_lower` instead of silently
    no-opping or raising an opaque error.
    """
    harness = _build(initial_floor=MoneyMicros(1_000_000))

    with pytest.raises(ValueError, match="request_lower"):
        harness.governance.raise_floor(MoneyMicros(1_000_000))

    assert harness.governance.current_floor_micros == MoneyMicros(1_000_000)


def test_raise_floor_below_the_current_floor_is_rejected_toward_request_lower() -> None:
    """Raising to a value strictly below the current floor is rejected the
    same way as an exact match."""
    harness = _build(initial_floor=MoneyMicros(1_000_000))

    with pytest.raises(ValueError, match="request_lower"):
        harness.governance.raise_floor(MoneyMicros(999_999))

    assert harness.governance.current_floor_micros == MoneyMicros(1_000_000)


def test_raise_floor_via_dashboard_origin_is_permitted() -> None:
    """The dashboard may raise the floor -- only *lowering* is CLI-only."""
    harness = _build(initial_floor=MoneyMicros(1_000_000))

    harness.governance.raise_floor(
        MoneyMicros(2_000_000), origin=ChangeOrigin.DASHBOARD
    )

    assert harness.governance.current_floor_micros == MoneyMicros(2_000_000)


# --- 2. request_lower: lifecycle start ---------------------------------------------


def test_request_lower_records_nonce_and_ready_at_172800_seconds_out() -> None:
    """`request_lower` records `FloorLowerRequested` whose `ready_at` is
    exactly `requested_at + 172_800`, and exposes the same via `pending_lower`.
    """
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)

    pending = harness.governance.request_lower(MoneyMicros(1_000_000))

    assert pending.target_floor_micros == MoneyMicros(1_000_000)
    assert pending.ready_at == _DEFAULT_NOW + 172_800
    requested = _events_of_type(harness.writer, "FloorLowerRequested")
    assert len(requested) == 1
    assert requested[0].payload["nonce"] == pending.nonce
    assert requested[0].payload["ready_at"] == pending.ready_at
    assert requested[0].payload["target_floor_micros"] == 1_000_000
    assert harness.governance.pending_lower == pending


def test_request_lower_dispatches_a_floor_change_request_alert_at_request_time() -> (
    None
):
    """A `FLOOR_CHANGE_REQUEST` alert fires at request time, not confirm
    time."""
    harness = _build(initial_floor=MoneyMicros(5_000_000))

    harness.governance.request_lower(MoneyMicros(1_000_000))

    assert [call[0] for call in harness.sink.calls] == [AlertType.FLOOR_CHANGE_REQUEST]


# --- 3. Cool-off is un-shortenable --------------------------------------------------


def test_confirm_lower_before_ready_at_raises_cool_off_active() -> None:
    """Confirming one second before `ready_at` raises `CoolOffActiveError`,
    records a refusal, and leaves the floor and pending lower untouched."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = pending.ready_at - 1

    with pytest.raises(CoolOffActiveError):
        harness.governance.confirm_lower(nonce=pending.nonce)

    assert harness.governance.current_floor_micros == MoneyMicros(5_000_000)
    assert harness.governance.pending_lower == pending
    refused = _events_of_type(harness.writer, "FloorLowerRefused")
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "cool_off_active"


def test_moving_the_clock_backward_after_request_cannot_shorten_the_cool_off() -> None:
    """Once requested, `ready_at` is fixed: moving the injected clock
    backward relative to the request cannot shorten the cool-off."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = 0

    with pytest.raises(CoolOffActiveError):
        harness.governance.confirm_lower(nonce=pending.nonce)

    assert harness.governance.pending_lower is not None
    assert harness.governance.pending_lower.ready_at == pending.ready_at


def test_second_request_lower_while_pending_keeps_original_ready_at() -> None:
    """A second `request_lower` while one is already pending raises
    `LoweringAlreadyPendingError` and does not reset `ready_at`, `nonce`, or
    the pending target floor."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    first = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now += 10

    with pytest.raises(LoweringAlreadyPendingError):
        harness.governance.request_lower(MoneyMicros(2_000_000))

    pending = harness.governance.pending_lower
    assert pending is not None
    assert pending.ready_at == first.ready_at
    assert pending.nonce == first.nonce
    assert pending.target_floor_micros == first.target_floor_micros


def test_confirm_lower_at_exactly_ready_at_succeeds() -> None:
    """`now == ready_at` is the inclusive boundary at which confirmation
    succeeds."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = pending.ready_at

    harness.governance.confirm_lower(nonce=pending.nonce)

    assert harness.governance.current_floor_micros == MoneyMicros(1_000_000)


# --- 4. Wrong nonce ------------------------------------------------------------------


def test_confirm_lower_with_a_mutated_nonce_raises_and_survives() -> None:
    """A mutated nonce raises `NonceMismatchError`, records a refusal, and
    leaves the floor and pending lower untouched; the correct nonce still
    works afterward."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = pending.ready_at

    with pytest.raises(NonceMismatchError):
        harness.governance.confirm_lower(nonce=pending.nonce + "-mutated")

    assert harness.governance.current_floor_micros == MoneyMicros(5_000_000)
    assert harness.governance.pending_lower == pending
    refused = _events_of_type(harness.writer, "FloorLowerRefused")
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "nonce_mismatch"

    harness.governance.confirm_lower(nonce=pending.nonce)

    assert harness.governance.current_floor_micros == MoneyMicros(1_000_000)


# --- 5. Completed lowering -----------------------------------------------------------


def test_confirm_lower_updates_floor_records_event_and_fires_alert() -> None:
    """A successful confirmation updates the floor, records
    `FloorLowerConfirmed`, fires a `FLOOR_CHANGE_REQUEST` alert, and clears
    `pending_lower`."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = pending.ready_at

    harness.governance.confirm_lower(nonce=pending.nonce)

    assert harness.governance.current_floor_micros == MoneyMicros(1_000_000)
    confirmed = _events_of_type(harness.writer, "FloorLowerConfirmed")
    assert len(confirmed) == 1
    assert confirmed[0].payload["new_floor_micros"] == 1_000_000
    assert confirmed[0].payload["previous_floor_micros"] == 5_000_000
    assert AlertType.FLOOR_CHANGE_REQUEST in [call[0] for call in harness.sink.calls]
    assert harness.governance.pending_lower is None


@pytest.mark.parametrize("starting_mode", [Mode.LIVE, Mode.LIVE_MICRO])
def test_confirm_lower_demotes_live_and_live_micro_to_paper(
    starting_mode: Mode,
) -> None:
    """A completed lowering demotes LIVE or LIVE_MICRO down to PAPER."""
    harness = _build(
        initial_floor=MoneyMicros(5_000_000),
        mode=starting_mode,
        mode_ceiling=Mode.LIVE,
        now=_DEFAULT_NOW,
    )
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = pending.ready_at

    harness.governance.confirm_lower(nonce=pending.nonce)

    assert harness.mode_machine.mode == Mode.PAPER


@pytest.mark.parametrize("starting_mode", [Mode.PAPER, Mode.RESEARCH])
def test_confirm_lower_leaves_paper_and_research_mode_unchanged(
    starting_mode: Mode,
) -> None:
    """A completed lowering does not touch the mode when already at or below
    PAPER."""
    harness = _build(
        initial_floor=MoneyMicros(5_000_000),
        mode=starting_mode,
        mode_ceiling=Mode.LIVE,
        now=_DEFAULT_NOW,
    )
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = pending.ready_at

    harness.governance.confirm_lower(nonce=pending.nonce)

    assert harness.mode_machine.mode == starting_mode


def test_confirm_lower_with_no_pending_lower_raises() -> None:
    """Confirming with no lowering ever requested raises `NoPendingLowerError`."""
    harness = _build(initial_floor=MoneyMicros(5_000_000))

    with pytest.raises(NoPendingLowerError):
        harness.governance.confirm_lower(nonce="whatever")


# --- 6. Dashboard is refused for lowering -------------------------------------------


def test_request_lower_via_dashboard_is_refused_and_records_refusal_first() -> None:
    """`request_lower(origin=DASHBOARD)` raises `ForbiddenOriginError`,
    recording the `FloorLowerRefused` event before the exception propagates,
    and leaves the floor and pending lower untouched."""
    harness = _build(initial_floor=MoneyMicros(5_000_000))

    with pytest.raises(ForbiddenOriginError):
        harness.governance.request_lower(
            MoneyMicros(1_000_000), origin=ChangeOrigin.DASHBOARD
        )

    assert harness.governance.current_floor_micros == MoneyMicros(5_000_000)
    assert harness.governance.pending_lower is None
    refused = _events_of_type(harness.writer, "FloorLowerRefused")
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "forbidden_origin"


# --- 7. Ratchet: known values --------------------------------------------------------


def test_ratchet_known_value_increment_is_floored() -> None:
    """`gain=1_000_001`, `ppm=500_000` -> floor increment `500_000`
    (`1_000_001 * 500_000 // 1_000_000 == 500_000.5` floored)."""
    harness = _build(initial_floor=MoneyMicros(0), ratchet_ppm=500_000)

    harness.governance.observe_equity(MoneyMicros(1_000_001))

    assert harness.governance.current_floor_micros == MoneyMicros(500_000)
    applied = _events_of_type(harness.writer, "FloorRatchetApplied")
    assert len(applied) == 1
    assert applied[0].payload["new_floor_micros"] == 500_000


def test_ratchet_is_idempotent_for_a_repeated_equity_observation() -> None:
    """Observing the same (already-peak) equity twice records exactly one
    `FloorRatchetApplied`."""
    harness = _build(initial_floor=MoneyMicros(0), ratchet_ppm=500_000)
    harness.governance.observe_equity(MoneyMicros(1_000_000))

    harness.governance.observe_equity(MoneyMicros(1_000_000))

    assert len(_events_of_type(harness.writer, "FloorRatchetApplied")) == 1


def test_ratchet_never_lowers_the_floor_on_a_declining_equity_observation() -> None:
    """A subsequent equity observation below the high-water mark is a
    no-op: no floor change, no event."""
    harness = _build(initial_floor=MoneyMicros(0), ratchet_ppm=500_000)
    harness.governance.observe_equity(MoneyMicros(2_000_000))
    floor_after_peak = harness.governance.current_floor_micros

    harness.governance.observe_equity(MoneyMicros(1_000_000))

    assert harness.governance.current_floor_micros == floor_after_peak
    assert len(_events_of_type(harness.writer, "FloorRatchetApplied")) == 1


def test_ratchet_applies_with_no_governance_delay() -> None:
    """The ratchet floor increase applies synchronously within
    `observe_equity` -- unlike the cool-off-gated lowering path, there is no
    pending/confirm step."""
    harness = _build(initial_floor=MoneyMicros(0), ratchet_ppm=1_000_000)

    harness.governance.observe_equity(MoneyMicros(1_000_000))

    assert harness.governance.current_floor_micros == MoneyMicros(1_000_000)


# --- 8. Ratchet: Hypothesis properties -----------------------------------------------

_equity_strategy = st.integers(min_value=0, max_value=10**12)
_ppm_strategy = st.integers(min_value=0, max_value=1_000_000)


@given(equities=st.lists(_equity_strategy, min_size=1, max_size=20), ppm=_ppm_strategy)
def test_ratchet_floor_is_monotonically_non_decreasing(
    equities: list[int], ppm: int
) -> None:
    """For any sequence of non-negative equity observations, the floor never
    decreases."""
    harness = _build(initial_floor=MoneyMicros(0), ratchet_ppm=ppm)
    previous = harness.governance.current_floor_micros.value

    for equity in equities:
        harness.governance.observe_equity(MoneyMicros(equity))
        current = harness.governance.current_floor_micros.value
        assert current >= previous
        previous = current


@given(gain=_equity_strategy, ppm=_ppm_strategy)
def test_ratchet_increment_equals_gain_times_ppm_floor_divided(
    gain: int, ppm: int
) -> None:
    """A single fresh-high observation's increment equals
    `gain * ppm // 1_000_000` exactly."""
    harness = _build(initial_floor=MoneyMicros(0), ratchet_ppm=ppm)

    harness.governance.observe_equity(MoneyMicros(gain))

    expected_increment = gain * ppm // 1_000_000
    assert harness.governance.current_floor_micros.value == expected_increment


@given(gain=_equity_strategy)
def test_ratchet_increment_never_exceeds_the_gain_even_at_full_ppm(gain: int) -> None:
    """Even at `ppm == 1_000_000` (100%), the increment never exceeds the
    gain it is computed from."""
    harness = _build(initial_floor=MoneyMicros(0), ratchet_ppm=1_000_000)

    harness.governance.observe_equity(MoneyMicros(gain))

    assert harness.governance.current_floor_micros.value <= gain


@given(equities=st.lists(_equity_strategy, min_size=1, max_size=20), ppm=_ppm_strategy)
def test_replaying_the_same_equity_sequence_yields_an_identical_final_floor(
    equities: list[int], ppm: int
) -> None:
    """Replaying the identical sequence of equity observations against a
    fresh governance instance yields the identical final floor."""
    first = _build(initial_floor=MoneyMicros(0), ratchet_ppm=ppm)
    second = _build(initial_floor=MoneyMicros(0), ratchet_ppm=ppm)

    for equity in equities:
        first.governance.observe_equity(MoneyMicros(equity))
    for equity in equities:
        second.governance.observe_equity(MoneyMicros(equity))

    assert (
        first.governance.current_floor_micros == second.governance.current_floor_micros
    )


# --- 9. Profit-sweep advisory ---------------------------------------------------------


def test_profit_sweep_advisory_fires_once_when_gain_exceeds_threshold() -> None:
    """A gain strictly above the threshold fires exactly one
    `PROFIT_SWEEP_ADVISORY` alert."""
    harness = _build(
        initial_floor=MoneyMicros(0),
        ratchet_ppm=0,
        profit_sweep_threshold=MoneyMicros(1_000_000),
    )

    harness.governance.observe_equity(MoneyMicros(2_000_000))

    advisories = [
        call
        for call in harness.sink.calls
        if call[0] == AlertType.PROFIT_SWEEP_ADVISORY
    ]
    assert len(advisories) == 1


def test_profit_sweep_advisory_refires_only_on_a_fresh_high_water_mark_crossing() -> (
    None
):
    """Re-observing the same (already-peak) equity does not refire the
    advisory; a later, genuinely fresh crossing does."""
    harness = _build(
        initial_floor=MoneyMicros(0),
        ratchet_ppm=0,
        profit_sweep_threshold=MoneyMicros(1_000_000),
    )

    harness.governance.observe_equity(MoneyMicros(2_000_000))
    harness.governance.observe_equity(MoneyMicros(2_000_000))
    harness.governance.observe_equity(MoneyMicros(4_000_000))

    advisories = [
        call
        for call in harness.sink.calls
        if call[0] == AlertType.PROFIT_SWEEP_ADVISORY
    ]
    assert len(advisories) == 2


def test_profit_sweep_advisory_is_silent_when_gain_does_not_exceed_threshold() -> None:
    """A gain exactly at (not above) the threshold stays silent."""
    harness = _build(
        initial_floor=MoneyMicros(0),
        ratchet_ppm=0,
        profit_sweep_threshold=MoneyMicros(1_000_000),
    )

    harness.governance.observe_equity(MoneyMicros(1_000_000))

    advisories = [
        call
        for call in harness.sink.calls
        if call[0] == AlertType.PROFIT_SWEEP_ADVISORY
    ]
    assert advisories == []


# --- 10. from_events replay -----------------------------------------------------------


def test_from_events_replay_with_no_events_starts_at_a_zero_floor_with_no_pending() -> (
    None
):
    """Replaying an empty history starts a fresh governance at a zero floor,
    with nothing pending."""
    rebuilt = _replay([])

    assert rebuilt.current_floor_micros == MoneyMicros(0)
    assert rebuilt.pending_lower is None


def test_from_events_replay_preserves_a_ratcheted_floor() -> None:
    """A floor increase applied purely by the ratchet survives a rebuild
    from the recorded events."""
    harness = _build(initial_floor=MoneyMicros(0), ratchet_ppm=500_000)
    harness.governance.observe_equity(MoneyMicros(2_000_000))
    expected_floor = harness.governance.current_floor_micros

    rebuilt = _replay(harness.writer.events, ratchet_ppm=500_000)

    assert rebuilt.current_floor_micros == expected_floor


def test_from_events_replay_preserves_a_live_pending_lower() -> None:
    """A still-pending lowering -- with its original nonce and `ready_at` --
    survives a rebuild from the recorded events."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))

    rebuilt = _replay(harness.writer.events, now=_DEFAULT_NOW)

    rebuilt_pending = rebuilt.pending_lower
    assert rebuilt_pending is not None
    assert rebuilt_pending.nonce == pending.nonce
    assert rebuilt_pending.ready_at == pending.ready_at
    assert rebuilt_pending.target_floor_micros == pending.target_floor_micros


def test_from_events_rebuild_cannot_shorten_the_cool_off() -> None:
    """A rebuilt governance still enforces the original `ready_at`: confirming
    one second early still raises `CoolOffActiveError`."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))

    rebuilt = _replay(harness.writer.events, now=pending.ready_at - 1)

    with pytest.raises(CoolOffActiveError):
        rebuilt.confirm_lower(nonce=pending.nonce)


def test_from_events_replay_preserves_a_floor_raised_by_the_operator() -> None:
    """A floor raised via `raise_floor` survives a rebuild from the recorded
    events, exercising the `FloorRaised` replay path."""
    harness = _build(initial_floor=MoneyMicros(1_000_000))
    harness.governance.raise_floor(MoneyMicros(3_000_000))

    rebuilt = _replay(harness.writer.events)

    assert rebuilt.current_floor_micros == MoneyMicros(3_000_000)
    assert rebuilt.pending_lower is None


def test_from_events_replay_ignores_a_recorded_refusal_event() -> None:
    """A `FloorLowerRefused` event in the history is inert on replay: the
    still-live pending lowering it was recorded against is preserved."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = pending.ready_at
    with pytest.raises(NonceMismatchError):
        harness.governance.confirm_lower(nonce=pending.nonce + "-mutated")

    rebuilt = _replay(harness.writer.events, now=_DEFAULT_NOW)

    rebuilt_pending = rebuilt.pending_lower
    assert rebuilt_pending is not None
    assert rebuilt_pending.nonce == pending.nonce
    assert rebuilt_pending.ready_at == pending.ready_at


def test_from_events_replay_does_not_resurrect_an_already_confirmed_lowering() -> None:
    """A lowering that was requested *and* confirmed before the replay is not
    resurrected as still-pending -- the rebuilt floor instead reflects the
    confirmed target."""
    harness = _build(initial_floor=MoneyMicros(5_000_000), now=_DEFAULT_NOW)
    pending = harness.governance.request_lower(MoneyMicros(1_000_000))
    harness.clock.now = pending.ready_at
    harness.governance.confirm_lower(nonce=pending.nonce)

    rebuilt = _replay(harness.writer.events, now=pending.ready_at)

    assert rebuilt.pending_lower is None
    assert rebuilt.current_floor_micros == MoneyMicros(1_000_000)
