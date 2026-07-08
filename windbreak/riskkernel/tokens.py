"""The Risk Kernel's approval-token issuer (SPEC S10.6).

This is the *sole* repo importer of :mod:`windbreak.riskkernel.signing` outside
the signing module itself: it pairs the isolated :class:`SigningKeyHandle` with
the shared :func:`~windbreak.tokens.verify.canonical_claims_bytes` encoding to
mint a :class:`~windbreak.tokens.verify.SignedApprovalToken`. Verification lives
in the shared :mod:`windbreak.tokens` package, which never sees the key handle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from windbreak.riskkernel.signing import SigningKeyHandle
from windbreak.tokens.verify import SignedApprovalToken, canonical_claims_bytes

if TYPE_CHECKING:
    from windbreak.tokens.verify import ApprovalTokenClaims

#: Default lifetime of a freshly issued approval token, in seconds (SPEC S10.6).
DEFAULT_TOKEN_TTL_SECONDS = 60


class TokenIssuer:
    """Signs approval-token claims into :class:`SignedApprovalToken` instances."""

    def __init__(self, handle: SigningKeyHandle) -> None:
        """Bind the issuer to a signing key handle.

        Args:
            handle: The Risk Kernel's isolated signing key handle.
        """
        self._handle = handle

    @classmethod
    def from_key_material(cls, key_material: bytes) -> TokenIssuer:
        """Build an issuer directly from raw signing-key material.

        Constructs the isolated :class:`SigningKeyHandle` internally so a
        composition root outside the ``riskkernel`` package (e.g. the PAPER
        scheduler wiring the kernel and gateway to one ephemeral key) can mint
        tokens from shared key bytes *without* importing
        :mod:`windbreak.riskkernel.signing` itself -- preserving the signing-key
        module boundary enforced by
        ``tests/riskkernel/test_process_isolation.py`` (this module remains the
        sole importer of the signing module).

        Args:
            key_material: The shared HMAC signing key material (>=32 bytes),
                the same secret the verifier checks tokens under.

        Returns:
            A :class:`TokenIssuer` bound to a fresh
            :class:`SigningKeyHandle` over ``key_material``.
        """
        return cls(SigningKeyHandle(key_material))

    def issue(self, claims: ApprovalTokenClaims) -> SignedApprovalToken:
        """Sign ``claims`` and return the resulting token.

        Args:
            claims: The approval-token claims to sign.

        Returns:
            A :class:`SignedApprovalToken` whose ``signature_hex`` is the
            HMAC-SHA256 tag over :func:`canonical_claims_bytes` of ``claims``.
        """
        signature = self._handle.sign(canonical_claims_bytes(claims))
        return SignedApprovalToken(claims=claims, signature_hex=signature.hex())
