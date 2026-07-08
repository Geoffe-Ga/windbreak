"""Shared, Gateway-consumable approval-token verification (SPEC S10.6).

This package holds the approval-token claims, their exact canonical signing
encoding, and :func:`verify_token`, so a consumer (e.g. the Order Gateway) can
verify a token without importing the Risk Kernel's private signing key handle
-- preserving the SPEC S5.3 key-isolation boundary. It carries money-bearing
claims and is therefore on the no-floats money path (SPEC S6.1).
"""

from windbreak.tokens.verify import (
    ApprovalTokenClaims,
    InMemorySingleUseRegistry,
    SignedApprovalToken,
    SingleUseRegistry,
    VerificationResult,
    canonical_claims_bytes,
    verify_token,
)

__all__ = [
    "ApprovalTokenClaims",
    "InMemorySingleUseRegistry",
    "SignedApprovalToken",
    "SingleUseRegistry",
    "VerificationResult",
    "canonical_claims_bytes",
    "verify_token",
]
