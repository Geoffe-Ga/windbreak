"""Gateway-side approval-token verification (SPEC S5.2, S10.6).

The Order Gateway verifies each single-use approval token before submitting the
order it authorizes. This module wraps the shared, key-isolated
:func:`windbreak.tokens.verify.verify_token` with the two extra guarantees the
Gateway needs:

    * an *intent-to-claims* cross-check, so a token minted for one intent can
      never authorize a *different* presented intent, and
    * a single, flat :class:`VerifyResult` verdict that collapses
      ``verify_token``'s ``(valid, reason)`` pair -- and every fail-closed
      degradation -- into one enum the Gateway state machine branches on.

Step order is load-bearing. :func:`intent_matches_claims` runs *first*: a
mismatch returns :attr:`VerifyResult.INTENT_MISMATCH` **without** consulting the
single-use registry, so presenting a token against the wrong intent can never
burn the legitimate token's one-time use (the shared ``verify_token`` consumes
the registry slot as its own internal last step and cannot be decomposed, so
the only safe place to reject a mismatch is before delegating to it). Only once
the intent matches does verification delegate to ``verify_token`` and map its
``reason`` onto a :class:`VerifyResult` via :data:`_REASON_TO_RESULT`, degrading
any unrecognized reason to :attr:`VerifyResult.REJECTED` -- never
:attr:`VerifyResult.OK`.

``verify_token`` is itself comprehensively fail-closed: it wraps its whole body
in a catch-all that converts *any* raised exception (a malformed-hex signature
included) into an ``invalid`` result carrying a ``"verification error: ..."``
reason, which this module's reason map degrades to
:attr:`VerifyResult.REJECTED`. That single fail-closed authority is therefore
reused rather than re-wrapped here: a second ``try``/``except`` around a call
that provably cannot raise would be an unreachable branch, which the
verification path's 100%-branch-coverage bar forbids. The net guarantee is
identical -- any error, or any reason this module does not explicitly recognize,
yields ``REJECTED`` and never ``OK``.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Final

from windbreak.tokens.verify import verify_token

if TYPE_CHECKING:
    from windbreak.riskkernel.checks import OrderIntent
    from windbreak.tokens.verify import (
        ApprovalTokenClaims,
        SignedApprovalToken,
        SingleUseRegistry,
    )


class VerifyResult(Enum):
    """The flat verdict of :func:`verify_and_consume`.

    Attributes:
        OK: The token matched the intent and passed every verification step.
        INTENT_MISMATCH: The token's claims do not match the presented intent
            (rejected before the single-use registry is ever consulted).
        BAD_SIGNATURE: The recomputed HMAC tag did not match the token's.
        EXPIRED: The token's ``expires_at`` is at or before the current time.
        REPLAYED: The token's single-use slot was already consumed.
        REJECTED: A fail-closed catch-all -- a malformed signature, or any other
            error or unrecognized verification reason. Never confused with
            :attr:`OK`.
    """

    OK = auto()
    INTENT_MISMATCH = auto()
    BAD_SIGNATURE = auto()
    EXPIRED = auto()
    REPLAYED = auto()
    REJECTED = auto()


#: Maps each ``verify_token`` failure ``reason`` this module recognizes onto its
#: :class:`VerifyResult`. A ``valid=True`` result is handled separately (it is
#: :attr:`VerifyResult.OK`); every reason absent from this map -- including
#: ``verify_token``'s fail-closed ``"verification error: ..."`` -- degrades to
#: :attr:`VerifyResult.REJECTED`, never :attr:`VerifyResult.OK`.
_REASON_TO_RESULT: Final[dict[str, VerifyResult]] = {
    "signature mismatch": VerifyResult.BAD_SIGNATURE,
    "token expired": VerifyResult.EXPIRED,
    "token already consumed": VerifyResult.REPLAYED,
}


def intent_matches_claims(intent: OrderIntent, claims: ApprovalTokenClaims) -> bool:
    """Return whether ``claims`` authorize exactly this ``intent``.

    Compares the 7 SPEC S10.6 fields that must agree for a token to authorize a
    presented order: ``intent_id``, ``market_ticker``, ``outcome``, ``action``,
    the limit price (``intent.price.value`` vs ``claims.limit_price_pips.value``),
    the size (``intent.size.value`` vs ``claims.count_centis.value``), and
    ``idempotency_key``.

    Four claim-only fields (``max_fee_micros``, ``expires_at``, ``config_hash``,
    ``kernel_sequence_number``) and two intent-only fields (``max_notional``,
    ``implied_probability``) are deliberately **not** compared: they have no
    counterpart on the other side (the claims carry approval-time risk metadata
    the intent never restates, and the intent carries caller-side caps the
    claims never echo), so requiring them to match would be meaningless. Expiry
    in particular is enforced by ``verify_token`` against the clock, not by this
    structural match.

    Args:
        intent: The order intent being presented for submission.
        claims: The claims carried by the accompanying approval token.

    Returns:
        ``True`` iff all 7 compared fields agree.
    """
    return (
        intent.intent_id == claims.intent_id
        and intent.market_ticker == claims.market_ticker
        and intent.outcome == claims.outcome
        and intent.action == claims.action
        and intent.price.value == claims.limit_price_pips.value
        and intent.size.value == claims.count_centis.value
        and intent.idempotency_key == claims.idempotency_key
    )


def verify_and_consume(
    token: SignedApprovalToken,
    intent: OrderIntent,
    *,
    key: bytes,
    now_epoch_s: int,
    registry: SingleUseRegistry,
) -> VerifyResult:
    """Verify ``token`` authorizes ``intent``, consuming its single use.

    Runs the intent-to-claims cross-check *first*: on a mismatch it returns
    :attr:`VerifyResult.INTENT_MISMATCH` without touching ``registry``, so a
    mismatched presentation can never burn the token's one-time use. Only a
    matching pair is delegated to the shared, key-isolated
    :func:`~windbreak.tokens.verify.verify_token`, whose ``(valid, reason)``
    verdict is then collapsed: ``valid`` is :attr:`VerifyResult.OK`; otherwise
    the ``reason`` is mapped via :data:`_REASON_TO_RESULT`, defaulting any
    unrecognized reason (including ``verify_token``'s fail-closed
    ``"verification error: ..."``) to :attr:`VerifyResult.REJECTED`.

    Args:
        token: The signed approval token to verify.
        intent: The order intent the token must authorize.
        key: The shared HMAC key the token was signed under.
        now_epoch_s: The current time, in epoch seconds.
        registry: The single-use registry gating replay.

    Returns:
        The single :class:`VerifyResult` verdict; never :attr:`VerifyResult.OK`
        unless the token matched and passed every step.
    """
    if not intent_matches_claims(intent, token.claims):
        return VerifyResult.INTENT_MISMATCH
    result = verify_token(token, key=key, now_epoch_s=now_epoch_s, registry=registry)
    if result.valid:
        return VerifyResult.OK
    return _REASON_TO_RESULT.get(result.reason, VerifyResult.REJECTED)
