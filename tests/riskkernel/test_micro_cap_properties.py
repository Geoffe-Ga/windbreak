"""Failing-first tests pinning that the LIVE_MICRO cap must include pending
kernel reservations, not just settled exposure (issue #57, RED).

The chief architect's plan for issue #57 flagged an exact, already-shippable
gap: ``windbreak.riskkernel.checks._ModePermissionCeiling`` compares
``context.account.total_exposure + cost`` against ``context.limits.micro_cap``,
but never adds ``context.account.pending_kernel_reservations`` -- the capital
the ``ApprovalPipeline`` has *already* reserved against other in-flight
approvals (stamped onto the effective context by
``ApprovalPipeline._effective_context`` on every call). Two intents, each
individually well under the cap, can therefore both be approved through the
*same* pipeline even though their combined reservation breaches it.

Every symbol imported below already exists (``windbreak.riskkernel.checks``,
``.context``, ``.reservations``, ``.tokens``, ``.signing``, ``.modes``), so
this file collects cleanly; both tests are genuinely RED against *today's*
``_ModePermissionCeiling`` logic via a real assertion failure (the second
intent is wrongly approved), not an import error.

Both tests isolate ``mode_permission_ceiling`` from the other 23 SPEC S10.3
checks -- 1 of which (``jurisdiction_product_eligibility``) is still an
unconditional-veto stub -- by monkeypatching
``windbreak.riskkernel.checks.evaluate_intent`` to run only the named
check(s), mirroring
``tests/riskkernel/test_reservations.py``'s T4 isolation technique exactly. No
production code is touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from tests.riskkernel.conftest import make_context, make_intent
from windbreak.numeric.types import ContractCentis, MoneyMicros, PricePips
from windbreak.riskkernel import checks as checks_module
from windbreak.riskkernel.modes import Mode
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter
from windbreak.riskkernel.reservations import ApprovalPipeline, ReservationLedger
from windbreak.riskkernel.signing import SigningKeyHandle
from windbreak.riskkernel.tokens import TokenIssuer

if TYPE_CHECKING:
    from collections.abc import Sequence

#: A fixed, valid (>=32-byte) signing key shared by every pipeline built below.
_KEY_MATERIAL = b"k" * 32

#: The exact ``_ModePermissionCeiling`` veto reason for a breached LIVE_MICRO
#: cap (``windbreak/riskkernel/checks.py``'s ``_ModePermissionCeiling.__call__``).
_CEILING_VETO_REASON = "live-micro exposure ceiling exceeded"

#: One token TTL (``DEFAULT_TOKEN_TTL_SECONDS`` == 60s) past whatever
#: ``now_epoch_s`` a reservation was made at, so ``expire_due`` at that offset
#: always releases every reservation still outstanding.
_PAST_ANY_RESERVATION_TTL_SECONDS = 61


def _isolate_checks(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Patch ``evaluate_intent`` to run only the named SPEC S10.3 check(s).

    Mirrors ``tests/riskkernel/test_reservations.py``'s T4 isolation
    technique: 1 of the 24 SPEC S10.3 checks is still an unconditional-veto
    stub, which would otherwise mask every cap behavior this file targets.
    Filtering ``DEFAULT_CHECKS`` (rather than hand-building a tuple) preserves
    the checks' pinned SPEC S10.3 evaluation order.

    Args:
        monkeypatch: The active monkeypatch fixture.
        *names: The ``Check.name`` value(s) to keep; every other check is
            dropped from the pipeline this test drives.
    """
    original_evaluate_intent = checks_module.evaluate_intent
    kept = tuple(check for check in checks_module.DEFAULT_CHECKS if check.name in names)

    def _kept_only_evaluate(intent: object, context: object) -> checks_module.Decision:
        return original_evaluate_intent(intent, context, checks=kept)

    monkeypatch.setattr(checks_module, "evaluate_intent", _kept_only_evaluate)


def _build_pipeline() -> tuple[ReservationLedger, ApprovalPipeline]:
    """Build a fresh in-memory ledger and an ``ApprovalPipeline`` over it."""
    ledger = ReservationLedger(InMemoryKernelLedgerWriter())
    issuer = TokenIssuer(SigningKeyHandle(_KEY_MATERIAL))
    pipeline = ApprovalPipeline(ledger, issuer, config_hash="cfg-hash-micro-cap")
    return ledger, pipeline


# --- Deterministic regression: two individually-safe intents, jointly over -----


def test_second_of_two_under_cap_intents_is_vetoed_once_jointly_over_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two LIVE_MICRO intents, each costing 5,000,000 micros (comfortably
    under a 6,000,000-micro cap alone), submitted through the *same*
    ``ApprovalPipeline``: the first reserves and is approved; the second must
    see that reservation via ``_effective_context``'s
    ``pending_kernel_reservations`` stamp and be vetoed, since
    5,000,000 (already reserved) + 5,000,000 (new cost) == 10,000,000, which
    exceeds the 6,000,000 cap. The current ``_ModePermissionCeiling`` omits
    ``pending_kernel_reservations`` from its comparison, so it wrongly
    approves the second intent too -- this assertion fails against today's
    implementation.
    """
    _isolate_checks(monkeypatch, "mode_permission_ceiling")
    ledger, pipeline = _build_pipeline()
    context = make_context(mode=Mode.LIVE_MICRO, micro_cap=MoneyMicros(6_000_000))
    first = make_intent(
        intent_id="intent-a",
        idempotency_key="idem-a",
        price=PricePips(1),
        size=ContractCentis(5_000_000),
    )
    second = make_intent(
        intent_id="intent-b",
        idempotency_key="idem-b",
        price=PricePips(1),
        size=ContractCentis(5_000_000),
    )

    first_outcome = pipeline.approve(first, context)
    assert first_outcome.token is not None, "fixture assumption: first alone fits"
    assert ledger.total_reserved() == MoneyMicros(5_000_000)

    second_outcome = pipeline.approve(second, context)

    assert second_outcome.token is None
    assert second_outcome.decision.vetoed is True
    assert _CEILING_VETO_REASON in second_outcome.decision.reasons


# --- Property: no stream of LIVE_MICRO approvals may jointly breach the cap ----


@dataclass(frozen=True)
class _Submit:
    """Submit a fresh order costing ``units * scale`` micros."""

    units: int


@dataclass(frozen=True)
class _ExpireAll:
    """Advance the clock well past every outstanding reservation's ttl and
    release everything still due -- modeling capital coming back off the
    books, so the invariant must keep holding across releases too, not just
    accumulation.
    """


_ACTION_STRATEGY = st.one_of(
    st.builds(_Submit, units=st.integers(min_value=1, max_value=5)),
    st.just(_ExpireAll()),
)


@given(
    scale=st.sampled_from([1, 1_000_000]),
    cap_units=st.integers(min_value=1, max_value=5),
    baseline_exposure_units=st.integers(min_value=0, max_value=2),
    actions=st.lists(_ACTION_STRATEGY, min_size=1, max_size=8),
)
@example(
    scale=1,
    cap_units=3,
    baseline_exposure_units=0,
    actions=(_Submit(2), _Submit(2)),
)
@settings(deadline=None, max_examples=100)
def test_approved_reservations_never_jointly_breach_the_micro_cap(
    scale: int,
    cap_units: int,
    baseline_exposure_units: int,
    actions: Sequence[_Submit | _ExpireAll],
) -> None:
    """For any integer-drawn stream of LIVE_MICRO submissions and releases
    against a tiny (``scale == 1``) or huge (``scale == 1_000_000``)
    ``micro_cap_micros``, every *approved* reservation must leave
    ``total_exposure + ledger.total_reserved() <= micro_cap`` -- otherwise the
    kernel has approved more live-micro risk than its own configured ceiling
    permits. The pinned ``@example`` above is the minimal two-submission case
    that reproduces the bug deterministically, independent of Hypothesis's
    random search; the free-form ``actions`` stream generalizes it across
    interleaved releases and cap/cost scales.

    Runs entirely offline against in-memory fakes; the
    ``TokenIssuer``/``ReservationLedger``/``ApprovalPipeline`` triad is
    exercised exactly as in ``tests/riskkernel/test_reservations.py``, with
    ``mode_permission_ceiling`` isolated from the 1 still-stubbed SPEC S10.3
    check. Every input is a plain Python ``int`` (SPEC S6.1: no floats
    anywhere in this file).

    Uses ``pytest.MonkeyPatch.context()`` rather than the ``monkeypatch``
    fixture -- mirroring ``tests/riskkernel/test_reservations.py``'s T4
    concurrency test -- so each Hypothesis-generated example gets its own
    scoped patch/undo cycle instead of sharing one fixture instance across
    every example the strategy draws.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        _isolate_checks(monkeypatch, "mode_permission_ceiling")
        micro_cap = MoneyMicros(cap_units * scale)
        baseline_exposure = MoneyMicros(baseline_exposure_units * scale)
        ledger, pipeline = _build_pipeline()
        now = 0

        for index, action in enumerate(actions):
            if isinstance(action, _ExpireAll):
                now += _PAST_ANY_RESERVATION_TTL_SECONDS
                ledger.expire_due(now)
                continue
            cost = action.units * scale
            intent = make_intent(
                intent_id=f"intent-{index}",
                idempotency_key=f"idem-{index}",
                price=PricePips(1),
                size=ContractCentis(cost),
            )
            context = make_context(
                mode=Mode.LIVE_MICRO,
                micro_cap=micro_cap,
                total_exposure=baseline_exposure,
                now_epoch_s=now,
            )

            outcome = pipeline.approve(intent, context)

            if outcome.token is not None:
                assert (
                    baseline_exposure.value + ledger.total_reserved().value
                    <= micro_cap.value
                )
