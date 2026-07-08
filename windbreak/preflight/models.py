"""Preflight report models (SPEC S3.3 production-readiness checklist).

The ``windbreak preflight`` command grades a fixed set of go-live checks and
renders the result. This module carries the two immutable value types that
result flows through:

:class:`CheckStatus` is the three-valued verdict a single check lands on;
:class:`PreflightCheck` is one graded check (frozen and slotted, like every
other windbreak value model); and :class:`PreflightReport` bundles the ordered
checks, derives a fail-closed :attr:`~PreflightReport.exit_code`, looks a check
up by id, and projects the whole report into a JSON-safe payload.

The payload projection mirrors :func:`windbreak.connector.models.market_to_payload`:
every leaf is an ``int``, ``str``, or ``bool`` and the enum status is emitted as
its ``.name`` string, so there is never a float leaf anywhere (SPEC S6.1).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class CheckStatus(enum.Enum):
    """The three-valued verdict a single preflight check lands on.

    Attributes:
        PASS: The check ran and its go-live invariant held.
        FAIL: The check ran and its invariant was violated, or the check
            failed closed (SPEC S3.3) because a collaborator raised.
        SKIP: The check could not be judged (its precondition is absent), which
            never blocks the run.
    """

    PASS = enum.auto()
    FAIL = enum.auto()
    SKIP = enum.auto()


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One graded production-readiness check (SPEC S3.3).

    Attributes:
        check_id: The check's stable dotted identifier (e.g.
            ``"exchange.reachable_readonly"``).
        description: A short human-readable label for what the check verifies.
        status: The check's :class:`CheckStatus` verdict.
        detail: A result-specific, human-readable explanation of the verdict.
            For security-sensitive checks this never echoes secret material.
        spec_ref: The SPEC section the check enforces (e.g. ``"§7.2"``).
    """

    check_id: str
    description: str
    status: CheckStatus
    detail: str
    spec_ref: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """An ordered bundle of graded checks with a fail-closed exit code.

    Attributes:
        checks: The graded checks, in the order they were run.
    """

    checks: tuple[PreflightCheck, ...]

    @property
    def exit_code(self) -> int:
        """Return ``0`` iff every non-SKIP check passed, else ``1``.

        A SKIP never blocks (its precondition was simply absent), so an
        all-SKIP report still exits cleanly; any single FAIL fails the whole
        run closed (SPEC S3.3).

        Returns:
            ``1`` if any check is :attr:`CheckStatus.FAIL`, else ``0``.
        """
        return (
            1 if any(check.status is CheckStatus.FAIL for check in self.checks) else 0
        )

    def __getitem__(self, check_id: str) -> PreflightCheck:
        """Return the check whose ``check_id`` matches exactly.

        Args:
            check_id: The dotted identifier to look up.

        Returns:
            The matching :class:`PreflightCheck`.

        Raises:
            KeyError: If no check carries ``check_id``.
        """
        for check in self.checks:
            if check.check_id == check_id:
                return check
        raise KeyError(check_id)

    def to_payload(self) -> dict[str, object]:
        """Project the report into a JSON-safe, float-free mapping.

        Each check renders its status as the enum member ``.name`` string; the
        top-level ``exit_code`` is the derived integer. Every leaf is an
        ``int`` or ``str`` (SPEC S6.1), so ``json.dumps`` succeeds and the
        payload round-trips unchanged.

        Returns:
            A JSON-serializable mapping shaped as
            ``{"exit_code": int, "checks": [{...}, ...]}``.
        """
        return {
            "exit_code": self.exit_code,
            "checks": [
                {
                    "check_id": check.check_id,
                    "description": check.description,
                    "status": check.status.name,
                    "detail": check.detail,
                    "spec_ref": check.spec_ref,
                }
                for check in self.checks
            ],
        }
