"""Rate limiting, retry/backoff, and a circuit breaker for connector calls.

Three cooperating, independently-testable pieces back issue #20's resilience
deliverables, all driven by injected integer timing so nothing here touches
wall-clock time or floats (this module sits on the money path guarded by
``scripts/lint_no_floats.py``):

* :class:`TokenBucket` -- integer-refill rate limiting. A full bucket bursts
  with zero sleeps; an empty bucket calls the injected sleeper *exactly once*
  (never a poll loop) for the whole number of seconds until the next token.
* :class:`CircuitBreaker` -- a CLOSED / OPEN / HALF_OPEN state machine on an
  injected integer clock: it refuses calls for a cooldown after tripping, then
  admits a single probe whose outcome closes or re-opens it.
* :class:`ResilientCaller` -- composes per-attempt token acquisition, a bounded
  retry loop with exponential backoff plus seeded jitter, and the breaker around
  one ``fn`` call. One token is spent per *physical* attempt, so a retry storm
  cannot outrun the configured request budget. A Kalshi ``5xx``/``429`` or any
  non-API transport exception is retried; a non-retryable ``4xx`` surfaces
  immediately without a breaker hit; exhausting every attempt re-raises the last
  error and counts as exactly one consecutive breaker failure for the whole
  ``call()``.

:func:`build_default_resilient_caller` is the production seam that wires a caller
on real ``time``/``secrets`` timing, so a ``KalshiClient`` built with no explicit
caller still gets live rate limiting, retries, and breaker protection by default.

An API error is recognized structurally -- by carrying an integer
``status_code`` attribute (as ``KalshiApiError`` does) -- so this generic
resilience layer stays decoupled from any one exchange's client module.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from typing import TYPE_CHECKING, Final, TypeVar, cast

from hedgekit.connector.snapshot import (
    ConnectorEvent,
    LoggingEventLedgerWriter,
    utc_now_iso,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from random import Random

    from hedgekit.connector.snapshot import EventLedgerWriter

#: Ledger event type recorded once each time the circuit breaker trips OPEN.
CONNECTOR_HALT_EVENT: Final = "CONNECTOR_HALT"

#: The lowest HTTP status of the retryable server-error (5xx) range.
_MIN_SERVER_ERROR: Final = 500

#: The highest HTTP status of the retryable server-error (5xx) range.
_MAX_SERVER_ERROR: Final = 599

#: The rate-limit status Kalshi returns under backpressure; retried like a 5xx.
_TOO_MANY_REQUESTS: Final = 429

#: Cap on the exponential-backoff left shift, bounding ``base << shift`` so a
#: high attempt index can never explode the shift into an unbounded integer.
_MAX_BACKOFF_SHIFT: Final = 30

#: Policy count fields that must be strictly positive (``>= 1``).
_POSITIVE_FIELDS: Final = (
    "bucket_capacity",
    "refill_interval_seconds",
    "max_attempts",
    "failure_threshold",
)

#: Policy duration fields that must merely be non-negative (``>= 0``).
_NON_NEGATIVE_FIELDS: Final = (
    "base_backoff_seconds",
    "max_backoff_seconds",
    "max_jitter_seconds",
    "cooldown_seconds",
)

_LOGGER = logging.getLogger("hedgekit.connector.resilience")

T = TypeVar("T")


class ConnectorHaltError(RuntimeError):
    """Raised when the circuit breaker is OPEN and refusing calls."""


class MaintenanceHaltError(RuntimeError):
    """Raised when the exchange is not open for trading (maintenance/paused)."""


def _require_at_least(name: str, value: int, minimum: int) -> None:
    """Raise :class:`ValueError` unless ``value`` meets ``minimum``.

    Args:
        name: The field name, surfaced in the failure message.
        value: The value under validation.
        minimum: The inclusive lower bound the value must satisfy.

    Raises:
        ValueError: If ``value`` is below ``minimum``.
    """
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")


@dataclass(frozen=True, slots=True)
class ResiliencePolicy:
    """Tunables for one connector's rate limiting, retries, and breaker.

    Attributes:
        bucket_capacity: Token-bucket capacity (burst size); ``>= 1``.
        refill_interval_seconds: Seconds between single-token refills; ``>= 1``.
        max_attempts: Transport attempts per logical call; ``>= 1``.
        base_backoff_seconds: Base backoff before the exponential shift; ``>= 0``.
        max_backoff_seconds: Ceiling on the pre-jitter backoff; ``>= 0``.
        max_jitter_seconds: Inclusive upper bound on added random jitter; ``>= 0``.
        failure_threshold: Consecutive call failures that trip the breaker; ``>= 1``.
        cooldown_seconds: Seconds the breaker stays OPEN before a probe; ``>= 0``.
    """

    bucket_capacity: int
    refill_interval_seconds: int
    max_attempts: int
    base_backoff_seconds: int
    max_backoff_seconds: int
    max_jitter_seconds: int
    failure_threshold: int
    cooldown_seconds: int

    def __post_init__(self) -> None:
        """Validate every field's sign and minimum.

        Raises:
            ValueError: If a count field is non-positive or a duration field is
                negative.
        """
        for name in _POSITIVE_FIELDS:
            _require_at_least(name, getattr(self, name), 1)
        for name in _NON_NEGATIVE_FIELDS:
            _require_at_least(name, getattr(self, name), 0)


#: Production defaults for a Kalshi connector's resilient caller: a modest
#: burst with a steady one-token-per-second refill, three transport attempts
#: per logical call, bounded exponential backoff with a little jitter, and a
#: five-strike breaker with a thirty-second cooldown. Wired on by default by
#: :func:`build_default_resilient_caller`; tunable per client via
#: ``KalshiClient``'s ``resilience_policy`` argument.
DEFAULT_RESILIENCE_POLICY: Final = ResiliencePolicy(
    bucket_capacity=10,
    refill_interval_seconds=1,
    max_attempts=3,
    base_backoff_seconds=1,
    max_backoff_seconds=30,
    max_jitter_seconds=1,
    failure_threshold=5,
    cooldown_seconds=30,
)


class CircuitState(Enum):
    """The three states of a :class:`CircuitBreaker`."""

    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class TokenBucket:
    """Integer-refill token bucket that sleeps once when empty, never polling."""

    def __init__(
        self,
        capacity: int,
        refill_interval_seconds: int,
        *,
        clock: Callable[[], int],
        sleeper: Callable[[int], None],
    ) -> None:
        """Initialize a full bucket anchored to the current clock reading.

        Args:
            capacity: Maximum tokens held (the burst size).
            refill_interval_seconds: Seconds between single-token refills.
            clock: Returns the current integer time.
            sleeper: Called once, with a whole-second duration, when empty.
        """
        self._capacity = capacity
        self._interval = refill_interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._tokens = capacity
        self._last_refill = clock()

    def _refill(self) -> None:
        """Grant ``elapsed // interval`` tokens, advancing by whole intervals."""
        elapsed = self._clock() - self._last_refill
        granted = elapsed // self._interval
        if granted > 0:
            self._tokens = min(self._capacity, self._tokens + granted)
            self._last_refill += granted * self._interval

    def acquire(self) -> None:
        """Consume one token, sleeping exactly once if the bucket is empty."""
        self._refill()
        if self._tokens == 0:
            remainder = self._clock() - self._last_refill
            self._sleeper(self._interval - remainder)
            self._last_refill += self._interval
            self._tokens += 1
        self._tokens -= 1


class CircuitBreaker:
    """A CLOSED / OPEN / HALF_OPEN breaker on an injected integer clock."""

    def __init__(
        self,
        failure_threshold: int,
        cooldown_seconds: int,
        *,
        clock: Callable[[], int],
    ) -> None:
        """Initialize a fresh, CLOSED breaker.

        Args:
            failure_threshold: Consecutive failures that trip the breaker OPEN.
            cooldown_seconds: Seconds OPEN before a probe is admitted.
            clock: Returns the current integer time.
        """
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0
        self._just_opened = False

    @property
    def state(self) -> CircuitState:
        """Return the breaker's current state."""
        return self._state

    @property
    def failure_count(self) -> int:
        """Return the current consecutive-failure count."""
        return self._failures

    def before_call(self) -> None:
        """Gate a call, admitting exactly one HALF_OPEN probe at a time.

        A CLOSED breaker admits every call. An OPEN breaker refuses until its
        cooldown elapses, at which point the *first* caller transitions it to
        HALF_OPEN and is admitted as the single recovery probe. While that
        probe is still in flight (state HALF_OPEN, not yet resolved by
        :meth:`record_success` / :meth:`record_failure`), every *other* caller
        is refused -- so a burst of callers arriving together after the cooldown
        cannot all stampede the recovering venue at once (the "half-open breaker
        race" this guard closes).

        Raises:
            ConnectorHaltError: If the breaker is OPEN and its cooldown has not
                yet elapsed, or if a HALF_OPEN probe is already in flight.
        """
        if self._state is CircuitState.CLOSED:
            return
        if self._state is CircuitState.HALF_OPEN:
            raise ConnectorHaltError(
                "connector circuit breaker is half-open; a probe is already in flight"
            )
        if self._clock() - self._opened_at >= self._cooldown:
            self._state = CircuitState.HALF_OPEN
            return
        raise ConnectorHaltError("connector circuit breaker is open; refusing the call")

    def record_success(self) -> None:
        """Close the breaker and reset the failure counter after a good call."""
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._just_opened = False

    def record_failure(self) -> None:
        """Count a failure, tripping OPEN at the threshold or from a failed probe.

        ``failure_count`` is a running tally of consecutive failures, reset only
        by a success: a HALF_OPEN probe failure increments it too, so a re-trip's
        ledgered ``consecutive_failures`` reflects the true unbroken streak
        (threshold + the failed probe) rather than a stale snapshot of the first
        trip's count.
        """
        self._just_opened = False
        self._failures += 1
        if self._state is CircuitState.HALF_OPEN or self._failures >= self._threshold:
            self._trip_open()

    def just_opened(self) -> bool:
        """Return whether the most recent failure freshly tripped the breaker.

        Returns:
            True only when the last :meth:`record_failure` transitioned the
            breaker into OPEN from a non-OPEN state, so the caller ledgers a
            :data:`CONNECTOR_HALT_EVENT` exactly once per OPEN transition.
        """
        return self._just_opened

    def _trip_open(self) -> None:
        """Enter OPEN, restarting the cooldown and flagging a fresh transition."""
        self._just_opened = self._state is not CircuitState.OPEN
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()


def _is_retryable_status(status_code: int) -> bool:
    """Return whether an HTTP status is a retryable failure.

    Args:
        status_code: The status carried by an API error.

    Returns:
        True for a ``429`` or any ``5xx``; False for every other status (a
        non-retryable ``4xx`` that must surface immediately).
    """
    if status_code == _TOO_MANY_REQUESTS:
        return True
    return _MIN_SERVER_ERROR <= status_code <= _MAX_SERVER_ERROR


def _is_retryable_exception(exc: BaseException) -> bool:
    """Return whether a failed attempt's exception should be retried.

    An API error is identified structurally by an integer ``status_code``
    attribute (as ``KalshiApiError`` carries): a ``429`` or ``5xx`` is a
    transient venue failure worth retrying, while any other ``4xx`` is a caller
    error that must surface immediately. Any exception *without* such a status
    (a malformed/truncated body, a connection error) is a retryable transport
    failure.

    Args:
        exc: The exception raised by a transport attempt.

    Returns:
        True when the attempt should be retried.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return _is_retryable_status(status)
    return True


class ResilientCaller:
    """Composes rate limiting, retry/backoff, and the breaker around one call."""

    def __init__(
        self,
        policy: ResiliencePolicy,
        ledger_writer: EventLedgerWriter,
        *,
        clock: Callable[[], int],
        sleeper: Callable[[int], None],
        rng: Random,
        wall_clock: Callable[[], datetime],
    ) -> None:
        """Initialize the caller and its owned bucket and breaker.

        Args:
            policy: The rate-limit, retry, and breaker tunables.
            ledger_writer: The seam that records :data:`CONNECTOR_HALT_EVENT`s.
            clock: Returns the current integer time (bucket refill / cooldown).
            sleeper: Called with whole-second backoff and rate-limit waits.
            rng: The seeded source of backoff jitter.
            wall_clock: Returns "now" as a datetime, stamped on ledgered events.
        """
        self._policy = policy
        self._ledger_writer = ledger_writer
        self._sleeper = sleeper
        self._rng = rng
        self._wall_clock = wall_clock
        self._breaker = CircuitBreaker(
            policy.failure_threshold, policy.cooldown_seconds, clock=clock
        )
        self._bucket = TokenBucket(
            policy.bucket_capacity,
            policy.refill_interval_seconds,
            clock=clock,
            sleeper=sleeper,
        )

    def call(self, fn: Callable[[], T]) -> T:
        """Invoke ``fn`` with rate limiting, retries, and breaker protection.

        Args:
            fn: The zero-argument transport call to run.

        Returns:
            ``fn``'s result once an attempt succeeds.

        Raises:
            ConnectorHaltError: If the breaker is OPEN and refusing calls.
            Exception: A non-retryable ``4xx`` API error (immediately), or the
                last error after exhausting every retryable attempt.
        """
        self._breaker.before_call()
        try:
            result = self._run_attempts(fn)
        except Exception as exc:
            self._handle_failure(exc, retryable=_is_retryable_exception(exc))
            raise
        self._breaker.record_success()
        return result

    def _run_attempts(self, fn: Callable[[], T]) -> T:
        """Run the bounded attempt loop, backing off between retryable failures.

        A token is acquired from the bucket before *every* physical attempt, so
        the rate limiter bounds real request rate per transport attempt (a
        retried call consumes one token per attempt, not one per logical call) --
        a retry storm can never outrun the configured request budget.

        Args:
            fn: The zero-argument transport call to run.

        Returns:
            ``fn``'s result on the first successful attempt.

        Raises:
            Exception: Immediately for a non-retryable ``4xx`` API error;
                otherwise the last error once every attempt is exhausted.
        """
        last_error: Exception | None = None
        for attempt in range(self._policy.max_attempts):
            self._bucket.acquire()
            try:
                return fn()
            except Exception as exc:  # classified structurally, then retried
                if not _is_retryable_exception(exc):
                    raise
                last_error = exc
            if attempt + 1 < self._policy.max_attempts:
                self._backoff(attempt)
        # The loop only reaches here after a retryable failure set last_error,
        # so it is never None; cast narrows it for the bare re-raise.
        raise cast("Exception", last_error)

    def _backoff(self, attempt: int) -> None:
        """Sleep the exponential backoff plus seeded jitter for ``attempt``.

        Args:
            attempt: The zero-based index of the failed attempt just made.
        """
        shift = min(attempt, _MAX_BACKOFF_SHIFT)
        capped = min(
            self._policy.max_backoff_seconds, self._policy.base_backoff_seconds << shift
        )
        jitter = self._rng.randint(0, self._policy.max_jitter_seconds)
        self._sleeper(capped + jitter)

    def _handle_failure(self, exc: Exception, *, retryable: bool) -> None:
        """Count one breaker failure per failed call, ledgering a fresh trip.

        A non-retryable failure never touches the breaker; a retryable
        exhaustion counts as exactly one consecutive failure and, if it trips
        the breaker OPEN, ledgers one :data:`CONNECTOR_HALT_EVENT`.

        Args:
            exc: The error that ended the call.
            retryable: Whether the error was a retryable class of failure.
        """
        if not retryable:
            return
        self._breaker.record_failure()
        if self._breaker.just_opened():
            self._ledger_halt(exc)

    def _ledger_halt(self, exc: Exception) -> None:
        """Ledger one :data:`CONNECTOR_HALT_EVENT`, isolating a raising writer.

        Args:
            exc: The error whose ``repr`` is recorded as the last cause.
        """
        event = ConnectorEvent(
            event_type=CONNECTOR_HALT_EVENT,
            payload={
                "consecutive_failures": self._breaker.failure_count,
                "cooldown_seconds": self._policy.cooldown_seconds,
                "last_error": repr(exc),
            },
            ts=utc_now_iso(self._wall_clock()),
        )
        try:
            self._ledger_writer.record(event)
        except Exception as ledger_exc:  # a broken ledger writer is isolated
            _LOGGER.warning(
                "event ledger writer failed to record %s event: %s",
                event.event_type,
                ledger_exc,
                extra={"component": "connector.resilience"},
            )


def _monotonic_seconds() -> int:
    """Return a whole-second monotonic reading for bucket refill / breaker timing."""
    return int(time.monotonic())


def _utc_now() -> datetime:
    """Return the current UTC time, the default caller's event-stamp clock."""
    return datetime.now(UTC)


def build_default_resilient_caller(
    ledger_writer: EventLedgerWriter | None = None,
    *,
    policy: ResiliencePolicy = DEFAULT_RESILIENCE_POLICY,
    clock: Callable[[], int] = _monotonic_seconds,
    sleeper: Callable[[int], None] = time.sleep,
    rng: Random | None = None,
    wall_clock: Callable[[], datetime] = _utc_now,
) -> ResilientCaller:
    """Build a :class:`ResilientCaller` wired to real runtime timing by default.

    This is the production composition seam :class:`~hedgekit.connector.kalshi.\
client.KalshiClient` uses to turn rate limiting, retries, and the circuit
    breaker *on by default*: a client built with no explicit caller still gets
    live protection. The timing seams default to real ``time`` / ``secrets``
    sources but stay injectable so a test can drive this exact same default
    construction deterministically (fake clock, recording sleeper, seeded RNG).

    Args:
        ledger_writer: Sink for ``CONNECTOR_HALT`` events; a
            :class:`~hedgekit.connector.snapshot.LoggingEventLedgerWriter` when
            None.
        policy: The rate-limit / retry / breaker tunables; the shared
            :data:`DEFAULT_RESILIENCE_POLICY` when unspecified.
        clock: Integer time source for bucket refill and breaker cooldown;
            whole-second :func:`time.monotonic` by default.
        sleeper: Whole-second sleep for backoff and rate-limit waits;
            :func:`time.sleep` by default.
        rng: Backoff-jitter source; a fresh :class:`secrets.SystemRandom` (an
            unpredictable :class:`random.Random`) when None.
        wall_clock: "Now" as a datetime, stamped on ledgered events.

    Returns:
        A caller owning a fresh token bucket and circuit breaker for the policy.
    """
    return ResilientCaller(
        policy,
        ledger_writer if ledger_writer is not None else LoggingEventLedgerWriter(),
        clock=clock,
        sleeper=sleeper,
        rng=rng if rng is not None else secrets.SystemRandom(),
        wall_clock=wall_clock,
    )
