"""Tests for hedgekit.connector.freshness (issue #20): caller-scoped TTL checks.

`is_fresh` / `ensure_fresh` compare a snapshot's age (`now - fetched_at`)
against a *caller-supplied* `ttl_seconds` -- there is no module-level default
TTL, so the same `(fetched_at, now)` pair must be able to read as fresh for a
lenient caller and stale for a strict one (SPEC §3 principle 3: fail closed,
per-caller). The boundary is inclusive: an age exactly equal to `ttl_seconds`
is fresh; one microsecond past it is stale. A `fetched_at` in the future
(`now < fetched_at`, i.e. clock skew) is always stale, regardless of
`ttl_seconds` -- an anomalous clock is never treated as "extra fresh".

`hedgekit.connector.freshness` does not exist yet, so importing it fails
collection with `ModuleNotFoundError: No module named 'hedgekit.connector'`
(or, once `hedgekit.connector` exists from an earlier issue,
`ModuleNotFoundError: No module named 'hedgekit.connector.freshness'`) --
the expected Gate 1 RED state for issue #20.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hedgekit.connector.freshness import StaleSnapshotError, ensure_fresh, is_fresh

#: A fixed "now" every test measures ages against; never wall-clock time.
_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


def _fetched_at(age: timedelta) -> datetime:
    """Return the `fetched_at` that is exactly `age` before `_NOW`.

    Args:
        age: How long ago the snapshot was fetched (negative for the future).

    Returns:
        The corresponding `fetched_at` timestamp.
    """
    return _NOW - age


def test_is_fresh_true_when_age_exactly_equals_the_ttl_boundary() -> None:
    """Age == ttl_seconds is fresh: the boundary is inclusive (`>`, not `>=`)."""
    fetched_at = _fetched_at(timedelta(seconds=30))

    assert is_fresh(fetched_at, ttl_seconds=30, now=_NOW) is True


def test_is_fresh_false_one_microsecond_past_the_ttl_boundary() -> None:
    """One microsecond past the TTL flips fresh to stale, pinning the `>` bound."""
    fetched_at = _fetched_at(timedelta(seconds=30, microseconds=1))

    assert is_fresh(fetched_at, ttl_seconds=30, now=_NOW) is False


def test_ensure_fresh_does_not_raise_at_exactly_the_ttl_boundary() -> None:
    """`ensure_fresh` is silent when age exactly equals the TTL."""
    fetched_at = _fetched_at(timedelta(seconds=30))

    ensure_fresh(fetched_at, ttl_seconds=30, now=_NOW)


def test_ensure_fresh_raises_one_microsecond_past_the_ttl_boundary() -> None:
    """`ensure_fresh` raises `StaleSnapshotError` carrying the exact age and limit."""
    fetched_at = _fetched_at(timedelta(seconds=30, microseconds=1))

    with pytest.raises(StaleSnapshotError) as exc_info:
        ensure_fresh(fetched_at, ttl_seconds=30, now=_NOW)

    assert exc_info.value.age == timedelta(seconds=30, microseconds=1)
    assert exc_info.value.limit == timedelta(seconds=30)


def test_same_snapshot_fresh_for_lenient_caller_stale_for_strict_caller() -> None:
    """TTL is always caller-supplied: no shared 30/10 module constant.

    The identical `(fetched_at, now)` pair must read differently depending
    solely on the caller's own `ttl_seconds` -- proving the limit is never a
    hardcoded module default.
    """
    fetched_at = _fetched_at(timedelta(seconds=15))

    assert is_fresh(fetched_at, ttl_seconds=30, now=_NOW) is True
    assert is_fresh(fetched_at, ttl_seconds=10, now=_NOW) is False


def test_ensure_fresh_silent_for_lenient_caller_raises_for_strict_caller() -> None:
    """`ensure_fresh` mirrors `is_fresh`'s per-caller independence."""
    fetched_at = _fetched_at(timedelta(seconds=15))

    ensure_fresh(fetched_at, ttl_seconds=30, now=fetched_at + timedelta(seconds=15))
    with pytest.raises(StaleSnapshotError):
        ensure_fresh(fetched_at, ttl_seconds=10, now=fetched_at + timedelta(seconds=15))


def test_future_fetched_at_is_never_fresh_regardless_of_ttl() -> None:
    """Clock skew (`now < fetched_at`) is anomalous and always reads as stale."""
    fetched_at = _NOW + timedelta(seconds=5)

    assert is_fresh(fetched_at, ttl_seconds=3_600, now=_NOW) is False


def test_ensure_fresh_raises_for_future_fetched_at_even_with_a_generous_ttl() -> None:
    """A future `fetched_at` fails closed even against a very generous TTL."""
    fetched_at = _NOW + timedelta(seconds=5)

    with pytest.raises(StaleSnapshotError) as exc_info:
        ensure_fresh(fetched_at, ttl_seconds=3_600, now=_NOW)

    assert exc_info.value.age == timedelta(seconds=-5)
    assert exc_info.value.limit == timedelta(seconds=3_600)


def test_is_fresh_true_at_zero_age_regardless_of_ttl() -> None:
    """A snapshot fetched at exactly `now` is fresh even against a zero TTL."""
    assert is_fresh(_NOW, ttl_seconds=0, now=_NOW) is True


def test_stale_snapshot_error_is_a_runtime_error() -> None:
    """`StaleSnapshotError` is catchable as a plain `RuntimeError`."""
    with pytest.raises(RuntimeError):
        ensure_fresh(_NOW + timedelta(seconds=1), ttl_seconds=10, now=_NOW)
