"""Tests for hedgekit.connector.resilience (issue #20): rate limit / retry / breaker.

Three cooperating, independently-testable pieces:

* `TokenBucket` -- integer-refill rate limiting. A full bucket bursts with
  zero sleeps; an empty bucket calls the injected sleeper *exactly once*
  (never a poll loop) with the exact number of whole seconds until the next
  token is available.
* `CircuitBreaker` -- a `CLOSED` / `OPEN` / `HALF_OPEN` state machine driven
  by an injected integer clock: `before_call()` raises `ConnectorHaltError` while
  `OPEN` and the cooldown has not elapsed; once it has, the *next*
  `before_call()` transitions to `HALF_OPEN` and admits a single probe.
* `ResilientCaller` -- composes retry/backoff/classification around one
  `fn` call: a Kalshi 5xx/429 or any non-`KalshiApiError` exception (a
  malformed/truncated response, a connection error) is retried with
  `min(max_backoff_seconds, base_backoff_seconds << attempt) +
  rng.randint(0, max_jitter_seconds)` backoff; a non-retryable 4xx surfaces
  immediately with no retry and no breaker increment; exhausting every
  attempt re-raises the last error and counts as exactly *one* consecutive
  breaker failure for the whole `call()`, not one per attempt.

All timing is whole integer seconds through an injected fake clock and a
recording no-op sleeper -- this suite never calls `time.sleep` and never
depends on wall-clock time. All jitter comes from a seeded `random.Random`
so expected sleeper arguments are computed exactly, not merely
range-checked.

`hedgekit.connector.resilience` does not exist yet, so importing it fails
collection with `ModuleNotFoundError` -- the expected Gate 1 RED state for
issue #20.

Pinned contract details not fully specified by the architect's design:

* `TokenBucket`'s exact refill/deficit arithmetic: `last_refill` only ever
  advances by *whole* elapsed intervals (never partially), so the remainder
  `now - last_refill` is always in `[0, refill_interval_seconds)` right
  after a refill; when the bucket is then still empty, the deficit slept is
  `refill_interval_seconds - remainder` (which equals the full interval
  when the remainder is exactly 0, e.g. immediately after a fresh burst).
  After sleeping, the implementation is assumed to advance `last_refill` by
  exactly one interval and grant+immediately-consume that one token, with
  no second clock read.
* `CircuitBreaker`'s and `ResilientCaller`'s constructor parameter names
  (`failure_threshold`, `cooldown_seconds`, `clock` for `CircuitBreaker`)
  are not spelled out beyond `ResilientCaller`'s; this suite pins them as
  the natural, minimal standalone seam.
* `TokenBucket` is tested fully standalone here, *not* wired through
  `ResilientCaller.call()`: the design doesn't specify whether rate
  limiting happens inside `ResilientCaller` per attempt or is wired
  elsewhere, so no test asserts that integration -- every `ResilientCaller`
  test below sets `bucket_capacity` generously high so no rate-limit sleep
  can appear alongside the backoff sleeps being pinned.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import UTC, datetime

import pytest

from hedgekit.connector.kalshi.client import KalshiApiError
from hedgekit.connector.resilience import (
    CONNECTOR_HALT_EVENT,
    CircuitBreaker,
    CircuitState,
    ConnectorHaltError,
    MaintenanceHaltError,
    ResiliencePolicy,
    ResilientCaller,
    TokenBucket,
)
from hedgekit.connector.snapshot import InMemoryEventLedgerWriter

#: The fixed wall-clock datetime every `ResilientCaller` in this module uses;
#: never wall-clock time, so `CONNECTOR_HALT` event timestamps are exact.
_WALL_CLOCK_DATETIME = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)

#: The ISO-Z rendering `CONNECTOR_HALT` events must stamp, matching the
#: `%Y-%m-%dT%H:%M:%S.%f` + `"Z"` convention already used throughout
#: `hedgekit.connector` (see `snapshot.utc_now_iso` / `adapter._iso_timestamp`).
_WALL_CLOCK_ISO = "2026-07-04T12:00:00.000000Z"


def _wall_clock() -> datetime:
    """Return the fixed wall-clock datetime this module's tests pin against."""
    return _WALL_CLOCK_DATETIME


class _RecordingSleeper:
    """A no-op sleeper that records every requested duration."""

    def __init__(self) -> None:
        """Initialize with no recorded calls yet."""
        self.calls: list[int] = []

    def __call__(self, seconds: int) -> None:
        """Record a requested duration instead of sleeping.

        Args:
            seconds: The whole-second duration that would have been slept.
        """
        self.calls.append(seconds)


class _FakeIntClock:
    """A mutable, manually-advanceable integer clock; never wall-clock time."""

    def __init__(self, start: int = 0) -> None:
        """Initialize the clock at a fixed starting value.

        Args:
            start: The initial integer "now" the clock reports.
        """
        self._now = start

    def __call__(self) -> int:
        """Return the current fake integer time."""
        return self._now

    def advance(self, seconds: int) -> None:
        """Move the fake clock forward.

        Args:
            seconds: The whole number of seconds to advance by.
        """
        self._now += seconds


@pytest.fixture
def fake_int_clock() -> _FakeIntClock:
    """Provide a fresh fake integer clock starting at zero."""
    return _FakeIntClock()


@pytest.fixture
def recording_sleeper() -> _RecordingSleeper:
    """Provide a fresh recording no-op sleeper."""
    return _RecordingSleeper()


@pytest.fixture
def ledger() -> InMemoryEventLedgerWriter:
    """Provide a fresh in-memory event ledger writer."""
    return InMemoryEventLedgerWriter()


def _policy(**overrides: int) -> ResiliencePolicy:
    """Build a `ResiliencePolicy` with generous defaults, overridable per test.

    Args:
        **overrides: Field values overriding the defaults below.

    Returns:
        The constructed policy.
    """
    fields: dict[str, int] = {
        "bucket_capacity": 1_000,
        "refill_interval_seconds": 10,
        "max_attempts": 3,
        "base_backoff_seconds": 1,
        "max_backoff_seconds": 60,
        "max_jitter_seconds": 0,
        "failure_threshold": 3,
        "cooldown_seconds": 30,
    }
    fields.update(overrides)
    return ResiliencePolicy(**fields)


# =============================================================================
# ResiliencePolicy
# =============================================================================


def test_resilience_policy_is_frozen() -> None:
    """`ResiliencePolicy` is a frozen dataclass; mutation raises."""
    policy = _policy()

    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.max_attempts = 99  # type: ignore[misc]


def test_resilience_policy_accepts_sane_minimal_values() -> None:
    """The smallest sensible values for every field construct without error."""
    policy = _policy(
        bucket_capacity=1,
        refill_interval_seconds=1,
        max_attempts=1,
        base_backoff_seconds=0,
        max_backoff_seconds=0,
        max_jitter_seconds=0,
        failure_threshold=1,
        cooldown_seconds=1,
    )

    assert policy.max_attempts == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"bucket_capacity": 0},
        {"bucket_capacity": -1},
        {"refill_interval_seconds": 0},
        {"refill_interval_seconds": -1},
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"base_backoff_seconds": -1},
        {"max_backoff_seconds": -1},
        {"max_jitter_seconds": -1},
        {"failure_threshold": 0},
        {"failure_threshold": -1},
        {"cooldown_seconds": -1},
    ],
)
def test_resilience_policy_rejects_unambiguously_invalid_values(
    overrides: dict[str, int],
) -> None:
    """A non-positive count or a negative duration is rejected at construction."""
    with pytest.raises(ValueError, match=r".+"):
        _policy(**overrides)


# =============================================================================
# TokenBucket
# =============================================================================


def test_full_bucket_bursts_capacity_times_with_zero_sleeps(
    fake_int_clock: _FakeIntClock, recording_sleeper: _RecordingSleeper
) -> None:
    """A full bucket grants every one of its `capacity` tokens without sleeping."""
    bucket = TokenBucket(3, 10, clock=fake_int_clock, sleeper=recording_sleeper)

    for _ in range(3):
        bucket.acquire()

    assert recording_sleeper.calls == []


def test_acquire_on_an_empty_bucket_sleeps_exactly_once(
    fake_int_clock: _FakeIntClock, recording_sleeper: _RecordingSleeper
) -> None:
    """An empty bucket calls the sleeper exactly once, never a poll loop."""
    bucket = TokenBucket(1, 10, clock=fake_int_clock, sleeper=recording_sleeper)
    bucket.acquire()  # consumes the sole starting token; zero sleep

    bucket.acquire()  # now empty; must sleep for exactly one full interval

    assert recording_sleeper.calls == [10]


def test_integer_refill_grants_floor_of_elapsed_over_interval_tokens(
    fake_int_clock: _FakeIntClock, recording_sleeper: _RecordingSleeper
) -> None:
    """Refill grants `elapsed // interval` tokens -- integer division, no more."""
    bucket = TokenBucket(2, 5, clock=fake_int_clock, sleeper=recording_sleeper)
    bucket.acquire()
    bucket.acquire()  # empty at clock=0

    fake_int_clock.advance(12)  # two full 5s intervals elapsed (10), remainder 2
    bucket.acquire()  # refills to 2 tokens, consumes one -> no sleep needed

    assert recording_sleeper.calls == []


def test_integer_refill_caps_at_capacity_never_bursting_unboundedly(
    fake_int_clock: _FakeIntClock, recording_sleeper: _RecordingSleeper
) -> None:
    """A very long elapsed gap still grants at most `capacity` tokens."""
    bucket = TokenBucket(2, 5, clock=fake_int_clock, sleeper=recording_sleeper)
    bucket.acquire()
    bucket.acquire()  # empty at clock=0

    fake_int_clock.advance(1_000)  # far more elapsed than needed for many tokens
    bucket.acquire()
    bucket.acquire()  # only 2 tokens granted (capacity cap), both consumed

    assert recording_sleeper.calls == []

    bucket.acquire()  # a third immediate acquire finds the bucket empty again

    assert recording_sleeper.calls == [5]


def test_acquire_sleeps_for_the_full_interval_when_the_remainder_is_zero(
    fake_int_clock: _FakeIntClock, recording_sleeper: _RecordingSleeper
) -> None:
    """Zero elapsed time since the last refill still needs a full interval's wait."""
    bucket = TokenBucket(1, 7, clock=fake_int_clock, sleeper=recording_sleeper)
    bucket.acquire()  # consumes the sole token at clock=0, zero sleep

    bucket.acquire()  # elapsed=0 since last_refill=0 -> deficit is the full interval

    assert recording_sleeper.calls == [7]


# =============================================================================
# CircuitBreaker
# =============================================================================


def test_circuit_state_has_exactly_the_three_documented_members() -> None:
    """`CircuitState` is exactly `CLOSED` / `OPEN` / `HALF_OPEN`."""
    assert {member.name for member in CircuitState} == {
        "CLOSED",
        "OPEN",
        "HALF_OPEN",
    }


def test_circuit_breaker_starts_closed_and_before_call_never_raises(
    fake_int_clock: _FakeIntClock,
) -> None:
    """A fresh breaker starts `CLOSED`; `before_call()` is a no-op."""
    breaker = CircuitBreaker(3, 30, clock=fake_int_clock)

    breaker.before_call()

    assert breaker.state is CircuitState.CLOSED


def test_circuit_breaker_trips_open_only_at_the_failure_threshold(
    fake_int_clock: _FakeIntClock,
) -> None:
    """Fewer than `failure_threshold` failures never trips the breaker open."""
    breaker = CircuitBreaker(3, 30, clock=fake_int_clock)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    breaker.before_call()  # still fine, no raise

    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN


def test_before_call_raises_connector_halt_while_open_and_cooldown_not_elapsed(
    fake_int_clock: _FakeIntClock,
) -> None:
    """`before_call()` raises `ConnectorHaltError` for the whole cooldown window."""
    breaker = CircuitBreaker(1, 30, clock=fake_int_clock)
    breaker.record_failure()  # threshold=1 -> OPEN immediately at clock=0

    fake_int_clock.advance(29)  # one second short of the cooldown

    with pytest.raises(ConnectorHaltError):
        breaker.before_call()
    assert breaker.state is CircuitState.OPEN


def test_before_call_transitions_to_half_open_once_cooldown_elapses(
    fake_int_clock: _FakeIntClock,
) -> None:
    """`before_call()` admits a single probe once the cooldown has elapsed."""
    breaker = CircuitBreaker(1, 30, clock=fake_int_clock)
    breaker.record_failure()  # OPEN at clock=0

    fake_int_clock.advance(30)  # cooldown elapsed exactly (inclusive boundary)
    breaker.before_call()  # must not raise

    assert breaker.state is CircuitState.HALF_OPEN


def test_record_success_while_half_open_closes_breaker_and_resets_counter(
    fake_int_clock: _FakeIntClock,
) -> None:
    """A successful probe closes the breaker and resets the failure counter."""
    breaker = CircuitBreaker(2, 30, clock=fake_int_clock)
    breaker.record_failure()
    breaker.record_failure()  # 2nd failure -> OPEN
    assert breaker.state is CircuitState.OPEN

    fake_int_clock.advance(30)
    breaker.before_call()  # -> HALF_OPEN
    breaker.record_success()

    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()  # only the 1st failure since the reset
    assert breaker.state is CircuitState.CLOSED
    breaker.before_call()  # still fine -- proves the counter reset, not carried over


def test_record_failure_while_half_open_reopens_and_ledgers_a_fresh_cooldown(
    fake_int_clock: _FakeIntClock,
) -> None:
    """A failed probe re-opens the breaker and restarts its own cooldown clock."""
    breaker = CircuitBreaker(1, 30, clock=fake_int_clock)
    breaker.record_failure()  # OPEN at clock=0

    fake_int_clock.advance(30)
    breaker.before_call()  # -> HALF_OPEN
    breaker.record_failure()  # probe failed -> re-OPEN, cooldown resets to clock=30

    assert breaker.state is CircuitState.OPEN
    with pytest.raises(ConnectorHaltError):
        breaker.before_call()  # still within the NEW cooldown window

    fake_int_clock.advance(30)  # a full cooldown period from the reset point
    breaker.before_call()  # transitions to HALF_OPEN again -- proves the reset

    assert breaker.state is CircuitState.HALF_OPEN


# =============================================================================
# ResilientCaller: backoff / classification
# =============================================================================


def test_call_retries_5xx_with_exact_seeded_backoff_and_jitter_then_succeeds(
    fake_int_clock: _FakeIntClock,
    recording_sleeper: _RecordingSleeper,
    ledger: InMemoryEventLedgerWriter,
) -> None:
    """Backoff between retries equals the documented formula, exactly.

    `min(max_backoff_seconds, base_backoff_seconds << attempt) +
    rng.randint(0, max_jitter_seconds)`, computed independently here against
    an identically-seeded `random.Random` consuming one `randint` call per
    backoff, in attempt order.
    """
    policy = _policy(
        max_attempts=3,
        base_backoff_seconds=2,
        max_backoff_seconds=100,
        max_jitter_seconds=5,
    )
    seed = 20260704
    caller = ResilientCaller(
        policy,
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=random.Random(seed),
        wall_clock=_wall_clock,
    )
    expected_rng = random.Random(seed)
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise KalshiApiError(500)
        return "ok"

    result = caller.call(fn)

    expected = [
        min(100, 2 << 0) + expected_rng.randint(0, 5),
        min(100, 2 << 1) + expected_rng.randint(0, 5),
    ]
    assert result == "ok"
    assert recording_sleeper.calls == expected
    assert attempts["n"] == 3


def test_call_retries_429_the_same_as_5xx(
    fake_int_clock: _FakeIntClock,
    recording_sleeper: _RecordingSleeper,
    ledger: InMemoryEventLedgerWriter,
) -> None:
    """A `429` is retried exactly like a `5xx` (rate-limit backpressure)."""
    policy = _policy(max_attempts=2, base_backoff_seconds=1, max_jitter_seconds=0)
    caller = ResilientCaller(
        policy,
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=random.Random(1),
        wall_clock=_wall_clock,
    )
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise KalshiApiError(429)
        return "ok"

    assert caller.call(fn) == "ok"
    assert attempts["n"] == 2
    assert recording_sleeper.calls == [1]


@pytest.mark.parametrize("status_code", [400, 404])
def test_non_retryable_4xx_surfaces_immediately_with_no_retry_or_breaker_hit(
    fake_int_clock: _FakeIntClock,
    recording_sleeper: _RecordingSleeper,
    ledger: InMemoryEventLedgerWriter,
    status_code: int,
) -> None:
    """A non-retryable 4xx surfaces on the first attempt: no retry, no breaker hit."""
    policy = _policy(max_attempts=3, failure_threshold=2)
    caller = ResilientCaller(
        policy,
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=random.Random(1),
        wall_clock=_wall_clock,
    )
    call_count = {"n": 0}

    def fn() -> None:
        call_count["n"] += 1
        raise KalshiApiError(status_code)

    for _ in range(5):  # far more than failure_threshold=2
        with pytest.raises(KalshiApiError):
            caller.call(fn)

    assert call_count["n"] == 5  # one fn() call per call() -- never retried
    assert recording_sleeper.calls == []
    assert ledger.events_by_type(CONNECTOR_HALT_EVENT) == ()  # breaker never tripped


def test_call_retries_a_non_kalshi_api_error_as_a_transport_failure(
    fake_int_clock: _FakeIntClock,
    recording_sleeper: _RecordingSleeper,
    ledger: InMemoryEventLedgerWriter,
) -> None:
    """Any non-`KalshiApiError` (e.g. malformed JSON, a connection error) retries."""
    policy = _policy(max_attempts=3, base_backoff_seconds=1, max_jitter_seconds=0)
    caller = ResilientCaller(
        policy,
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=random.Random(1),
        wall_clock=_wall_clock,
    )
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return "ok"

    assert caller.call(fn) == "ok"
    assert attempts["n"] == 2
    assert recording_sleeper.calls == [1]


def test_exhausted_retries_reraise_the_last_error(
    fake_int_clock: _FakeIntClock,
    recording_sleeper: _RecordingSleeper,
    ledger: InMemoryEventLedgerWriter,
) -> None:
    """Exhausting every attempt re-raises the *last* error, not the first."""
    policy = _policy(max_attempts=2, base_backoff_seconds=1, max_jitter_seconds=0)
    caller = ResilientCaller(
        policy,
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=random.Random(1),
        wall_clock=_wall_clock,
    )
    codes = iter([500, 503])

    def fn() -> None:
        raise KalshiApiError(next(codes))

    with pytest.raises(KalshiApiError) as exc_info:
        caller.call(fn)

    assert exc_info.value.status_code == 503


# =============================================================================
# ResilientCaller: breaker integration
# =============================================================================


def test_second_consecutive_call_failure_trips_the_breaker_and_ledgers_once(
    fake_int_clock: _FakeIntClock,
    recording_sleeper: _RecordingSleeper,
    ledger: InMemoryEventLedgerWriter,
) -> None:
    """Exhaustion counts as ONE failure per `call()`, not per attempt.

    `max_attempts=2` means each failing `call()` makes two transport
    attempts internally, yet still counts as exactly one breaker failure --
    it takes two whole `call()`s (matching `failure_threshold=2`) to trip.
    """
    policy = _policy(
        max_attempts=2,
        failure_threshold=2,
        base_backoff_seconds=1,
        max_jitter_seconds=0,
        cooldown_seconds=60,
    )
    caller = ResilientCaller(
        policy,
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=random.Random(1),
        wall_clock=_wall_clock,
    )
    call_count = {"n": 0}

    def always_fails() -> None:
        call_count["n"] += 1
        raise KalshiApiError(500)

    with pytest.raises(KalshiApiError):
        caller.call(always_fails)  # call #1: 2 attempts exhausted, 1st breaker failure

    assert ledger.events_by_type(CONNECTOR_HALT_EVENT) == ()

    with pytest.raises(KalshiApiError):
        caller.call(always_fails)  # call #2: 2nd breaker failure -> trips OPEN

    halts = ledger.events_by_type(CONNECTOR_HALT_EVENT)
    assert len(halts) == 1
    assert halts[0].ts == _WALL_CLOCK_ISO

    calls_before_third = call_count["n"]
    with pytest.raises(ConnectorHaltError):
        caller.call(always_fails)  # call #3: breaker OPEN, cooldown not elapsed

    assert call_count["n"] == calls_before_third  # fn() never invoked: no transport
    assert ledger.events_by_type(CONNECTOR_HALT_EVENT) == halts  # still exactly one


def test_cooldown_elapsed_half_open_probe_success_closes_the_breaker(
    fake_int_clock: _FakeIntClock,
    recording_sleeper: _RecordingSleeper,
    ledger: InMemoryEventLedgerWriter,
) -> None:
    """A successful probe after cooldown closes the breaker and resets its counter."""
    policy = _policy(
        max_attempts=1,
        failure_threshold=1,
        cooldown_seconds=30,
        base_backoff_seconds=1,
        max_jitter_seconds=0,
    )
    caller = ResilientCaller(
        policy,
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=random.Random(1),
        wall_clock=_wall_clock,
    )

    def fails_once() -> None:
        raise KalshiApiError(500)

    with pytest.raises(KalshiApiError):
        caller.call(fails_once)  # trips OPEN immediately (threshold=1)

    with pytest.raises(ConnectorHaltError):
        caller.call(fails_once)  # still within cooldown

    fake_int_clock.advance(30)  # cooldown elapsed exactly

    assert caller.call(lambda: "recovered") == "recovered"  # HALF_OPEN probe succeeds

    with pytest.raises(KalshiApiError):
        caller.call(fails_once)  # a fresh single failure trips OPEN again

    # Two distinct OPEN transitions -> two CONNECTOR_HALT events, proving the
    # successful probe reset the counter rather than leaving it primed.
    assert len(ledger.events_by_type(CONNECTOR_HALT_EVENT)) == 2


def test_cooldown_elapsed_half_open_probe_failure_reopens_the_breaker(
    fake_int_clock: _FakeIntClock,
    recording_sleeper: _RecordingSleeper,
    ledger: InMemoryEventLedgerWriter,
) -> None:
    """A failed probe after cooldown re-opens the breaker and ledgers again."""
    policy = _policy(
        max_attempts=1,
        failure_threshold=1,
        cooldown_seconds=30,
        base_backoff_seconds=1,
        max_jitter_seconds=0,
    )
    caller = ResilientCaller(
        policy,
        ledger,
        clock=fake_int_clock,
        sleeper=recording_sleeper,
        rng=random.Random(1),
        wall_clock=_wall_clock,
    )

    def always_fails() -> None:
        raise KalshiApiError(500)

    with pytest.raises(KalshiApiError):
        caller.call(always_fails)  # trips OPEN

    fake_int_clock.advance(30)  # cooldown elapsed

    with pytest.raises(KalshiApiError):
        caller.call(always_fails)  # HALF_OPEN probe fails -> reopens

    assert len(ledger.events_by_type(CONNECTOR_HALT_EVENT)) == 2

    with pytest.raises(ConnectorHaltError):
        caller.call(always_fails)  # immediately OPEN again (fresh cooldown)


# =============================================================================
# MaintenanceHaltError / ConnectorHaltError: basic shape
# =============================================================================


def test_connector_halt_and_maintenance_halt_are_runtime_errors() -> None:
    """Both halt exceptions are catchable as plain `RuntimeError`."""
    assert issubclass(ConnectorHaltError, RuntimeError)
    assert issubclass(MaintenanceHaltError, RuntimeError)


def test_connector_halt_event_constant_value() -> None:
    """`CONNECTOR_HALT_EVENT` is the documented literal string."""
    assert CONNECTOR_HALT_EVENT == "CONNECTOR_HALT"
