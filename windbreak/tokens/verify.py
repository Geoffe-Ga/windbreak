"""Shared approval-token claims, canonical encoding, and verification (SPEC S10.6).

This module is *Gateway-consumable*: the Order Gateway verifies approval tokens
here without ever importing the Risk Kernel's private signing key handle
(:mod:`windbreak.riskkernel.signing`), preserving the SPEC S5.3 key-isolation
boundary. It defines the claims a token asserts, the *exact* canonical byte
encoding those claims are signed over, and the shapes
(:class:`SignedApprovalToken`, :class:`VerificationResult`,
:class:`SingleUseRegistry`) :func:`verify_token` operates on.

Verification recomputes the HMAC-SHA256 tag directly from stdlib
:mod:`hmac`/:mod:`hashlib` -- it never imports the issuer-side handle -- and is
fail-closed at every step: a malformed signature, a tag mismatch, an expired
token, or any raised exception yields an invalid result, and the single-use
registry is consulted *last* so a forged or expired token can never burn a
legitimate token's one-time use.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from windbreak.ledger.events import canonical_json

if TYPE_CHECKING:
    from windbreak.numeric.types import ContractCentis, MoneyMicros, PricePips

#: The domain-separation prefix prepended to every canonical claims encoding, so
#: an approval-token signature can never be replayed as a signature over some
#: other windbreak-signed artifact (SPEC S10.6).
_DOMAIN_PREFIX = b"windbreak.approval-token.v1\x00"

#: Schema version embedded in the signed claims dict, so a future field-set
#: change is distinguishable in the signed bytes themselves.
_TOKEN_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ApprovalTokenClaims:
    """The claims a signed approval token asserts (SPEC S10.6).

    Field order is load-bearing: it is the exact SPEC S10.6 sequence the
    canonical encoding is authored from. Every scaled-unit field is a
    :mod:`windbreak.numeric` type serialized as its integer ``.value``.

    Attributes:
        intent_id: The approved intent's unique identifier.
        market_ticker: The exchange ticker the intent targets.
        outcome: The market outcome the intent trades (e.g. ``"yes"``).
        action: The trade action (e.g. ``"buy"``).
        limit_price_pips: The limit price, in pips.
        count_centis: The contract count, in centis.
        max_fee_micros: The combined worst-case trading + settlement fee cap.
        expires_at: The token's expiry, in epoch seconds.
        idempotency_key: The caller-supplied idempotency key.
        config_hash: The configuration revision hash active at approval time.
        kernel_sequence_number: The reservation-ledger sequence number.
    """

    intent_id: str
    market_ticker: str
    outcome: str
    action: str
    limit_price_pips: PricePips
    count_centis: ContractCentis
    max_fee_micros: MoneyMicros
    expires_at: int
    idempotency_key: str
    config_hash: str
    kernel_sequence_number: int


def canonical_claims_bytes(claims: ApprovalTokenClaims) -> bytes:
    """Return the exact bytes an approval token's signature is computed over.

    The encoding is the fixed domain prefix followed by the canonical JSON of a
    dict carrying the schema version and all 11 claims -- each scaled-unit field
    as its bare integer ``.value``, ``expires_at`` as an int, and every string
    verbatim. It reuses :func:`~windbreak.ledger.events.canonical_json` (sorted
    keys, no whitespace) so an issuer and a verifier serialize identically.

    Args:
        claims: The claims to encode.

    Returns:
        The domain-separated canonical byte encoding.
    """
    payload: dict[str, object] = {
        "token_schema_version": _TOKEN_SCHEMA_VERSION,
        "intent_id": claims.intent_id,
        "market_ticker": claims.market_ticker,
        "outcome": claims.outcome,
        "action": claims.action,
        "limit_price_pips": claims.limit_price_pips.value,
        "count_centis": claims.count_centis.value,
        "max_fee_micros": claims.max_fee_micros.value,
        "expires_at": claims.expires_at,
        "idempotency_key": claims.idempotency_key,
        "config_hash": claims.config_hash,
        "kernel_sequence_number": claims.kernel_sequence_number,
    }
    return _DOMAIN_PREFIX + canonical_json(payload).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SignedApprovalToken:
    """A set of approval-token claims paired with its hex-encoded signature.

    Attributes:
        claims: The signed claims.
        signature_hex: The HMAC-SHA256 signature over
            :func:`canonical_claims_bytes` of ``claims``, hex-encoded.
    """

    claims: ApprovalTokenClaims
    signature_hex: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The outcome of verifying a signed approval token.

    Attributes:
        valid: Whether the token passed every verification step.
        reason: A short human-readable explanation of the verdict.
    """

    valid: bool
    reason: str


class SingleUseRegistry(Protocol):
    """A registry enforcing that each token signature is consumed at most once."""

    def consume(self, token_signature_hex: str) -> bool:
        """Attempt to consume a signature, reporting whether it was new.

        Args:
            token_signature_hex: The canonicalized signature key: the decoded
                signature bytes re-encoded via ``bytes.hex()``, so it is always
                lowercase and free of whitespace. Callers must canonicalize
                before keying, so hex re-spellings collapse to one slot.

        Returns:
            ``True`` the first time a given signature is seen, ``False`` on
            every subsequent attempt.
        """
        ...


class InMemorySingleUseRegistry:
    """A thread-safe, in-memory :class:`SingleUseRegistry` backed by a set."""

    def __init__(self) -> None:
        """Initialize with an empty consumed-signature set and a lock."""
        self._consumed: set[str] = set()
        self._lock = threading.Lock()

    def consume(self, token_signature_hex: str) -> bool:
        """Consume ``token_signature_hex`` once, returning whether it was new.

        Args:
            token_signature_hex: The canonicalized signature key: the decoded
                signature bytes re-encoded via ``bytes.hex()``, so it is always
                lowercase and free of whitespace. Hex re-spellings of one
                authentic signature therefore collapse to a single set entry.

        Returns:
            ``True`` if this signature had not been consumed before, else
            ``False``.
        """
        with self._lock:
            if token_signature_hex in self._consumed:
                return False
            self._consumed.add(token_signature_hex)
            return True


def verify_token(
    token: SignedApprovalToken,
    *,
    key: bytes,
    now_epoch_s: int,
    registry: SingleUseRegistry,
) -> VerificationResult:
    """Verify a signed approval token, fail-closed, in a pinned step order.

    The steps run in a fixed order so a failing token never has the side effect
    a later step would: (1) recompute the HMAC-SHA256 tag over the canonical
    claims bytes under ``key`` and compare it, in constant time, against the
    token's decoded signature; (2) reject an expired token (valid iff
    ``now_epoch_s < expires_at``); (3) consume the single-use registry slot
    *last*, keyed on the canonicalized decoded signature (``provided.hex()``)
    so hex re-spellings of one authentic signature collapse to a single slot,
    ensuring only a token that is both authentic and unexpired can burn its
    one-time use. The whole body is wrapped fail-closed: a malformed hex
    signature (or any other raised exception) is caught and reported invalid
    before the expiry or registry steps are ever reached.

    Args:
        token: The signed token to verify.
        key: The shared HMAC key the token was signed under.
        now_epoch_s: The current time, in epoch seconds.
        registry: The single-use registry gating replay.

    Returns:
        A :class:`VerificationResult`; ``valid`` is ``True`` only if every step
        passed.
    """
    try:
        expected = hmac.new(
            key, canonical_claims_bytes(token.claims), hashlib.sha256
        ).digest()
        provided = bytes.fromhex(token.signature_hex)
        if not hmac.compare_digest(expected, provided):
            return VerificationResult(valid=False, reason="signature mismatch")
        if now_epoch_s >= token.claims.expires_at:
            return VerificationResult(valid=False, reason="token expired")
        if not registry.consume(provided.hex()):
            return VerificationResult(valid=False, reason="token already consumed")
        return VerificationResult(valid=True, reason="ok")
    except Exception as exc:  # Fail-closed: malformed signature or any error.
        return VerificationResult(valid=False, reason=f"verification error: {exc}")
