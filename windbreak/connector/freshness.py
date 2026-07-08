"""Caller-scoped snapshot freshness checks (issue #20, SPEC S3 principle 3).

A cached exchange snapshot is only safe to act on while it is *fresh enough for
the caller asking* -- a lenient consumer may tolerate a thirty-second-old book
where a strict one demands ten. So freshness here is never a module-wide
constant: every check takes the caller's own ``ttl_seconds`` and compares a
snapshot's age (``now - fetched_at``) against it directly, timedelta to
timedelta, never touching :meth:`datetime.timedelta.total_seconds` (a float) --
this module sits on the money path guarded by ``scripts/lint_no_floats.py``.

The boundary is inclusive and fails closed: an age exactly equal to
``ttl_seconds`` is fresh, one microsecond past it is stale, and a ``fetched_at``
in the future (``now < fetched_at``, i.e. clock skew) is *always* stale
regardless of ``ttl_seconds`` -- an anomalous clock is never read as "extra
fresh".
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

#: The zero-length duration a non-negative snapshot age must not fall below.
_NO_AGE = timedelta(0)


class StaleSnapshotError(RuntimeError):
    """Raised when a snapshot is older than the caller's freshness budget.

    Carries both the observed ``age`` and the ``limit`` it exceeded so callers
    (and the ledger) can record exactly how stale the refused snapshot was.

    Attributes:
        age: The snapshot's observed age (``now - fetched_at``); negative when
            ``fetched_at`` is in the future.
        limit: The caller's freshness budget as a timedelta.
    """

    def __init__(self, age: timedelta, limit: timedelta) -> None:
        """Initialize with the observed age and the exceeded limit.

        Args:
            age: The snapshot's observed age (``now - fetched_at``).
            limit: The caller's freshness budget that was exceeded.
        """
        self.age = age
        self.limit = limit
        super().__init__(f"snapshot age {age} is outside the freshness limit {limit}")


def is_fresh(fetched_at: datetime, *, ttl_seconds: int, now: datetime) -> bool:
    """Return whether a snapshot is fresh for the caller's ``ttl_seconds``.

    Freshness is inclusive at the boundary and fails closed on clock skew: an
    age exactly equal to ``ttl_seconds`` is fresh, one microsecond past it is
    stale, and a future ``fetched_at`` (``now < fetched_at``) is always stale.

    Args:
        fetched_at: When the snapshot was taken.
        ttl_seconds: The caller's freshness budget, in whole seconds.
        now: The reference time to measure the snapshot's age against.

    Returns:
        True when the snapshot's age is within ``[0, ttl_seconds]``.
    """
    age = now - fetched_at
    if age < _NO_AGE:
        return False
    return age <= timedelta(seconds=ttl_seconds)


def ensure_fresh(fetched_at: datetime, *, ttl_seconds: int, now: datetime) -> None:
    """Raise :class:`StaleSnapshotError` unless a snapshot is fresh for the caller.

    Args:
        fetched_at: When the snapshot was taken.
        ttl_seconds: The caller's freshness budget, in whole seconds.
        now: The reference time to measure the snapshot's age against.

    Raises:
        StaleSnapshotError: If the snapshot is older than ``ttl_seconds`` or
            was fetched in the future.
    """
    if not is_fresh(fetched_at, ttl_seconds=ttl_seconds, now=now):
        raise StaleSnapshotError(
            age=now - fetched_at, limit=timedelta(seconds=ttl_seconds)
        )
