# ADR-0001: Approval tokens use symmetric HMAC-SHA256, not an asymmetric signature

- **Status:** Accepted
- **Date:** 2026-07-05
- **Issue:** #31 (feat: serialized reservations and signed single-use approval tokens)
- **SPEC:** §5.2 (Kernel holds signing key; Gateway holds verification key), §10.6
  (approval tokens), §3.4 (boring technology / standard crypto primitives), threats T3/T4

## Context

The Risk Kernel issues single-use approval tokens that the Order Gateway later
verifies before submitting an order (§10.6). A token is a tag over the canonical
serialization of the intent's economic fields. We must choose the primitive that
produces and checks that tag.

Two families were considered:

1. **Symmetric MAC (HMAC-SHA256).** One shared secret both signs (Kernel) and
   verifies (Gateway).
2. **Asymmetric signature (e.g. Ed25519).** The Kernel holds a private signing
   key; the Gateway holds only the public verification key and cannot forge.

The #29 skeleton's `signing.py` docstring speculatively named "Ed25519", so this
decision also records why that guess was superseded.

## Decision

Use **HMAC-SHA256 via the Python standard library** (`hmac` + `hashlib`,
compared with `hmac.compare_digest`). The signing key and the verification key
are the same 256-bit shared secret.

Rationale:

- SPEC §10.6 specifies the tag as an "HMAC/signature" and §3.4 mandates "boring
  technology … standard crypto primitives". stdlib `hmac` is a standard, audited
  primitive — not a hand-rolled MAC — and adds **zero new dependencies**. An
  asymmetric scheme would require pulling in `cryptography`/`PyNaCl`, expanding
  the dependency and supply-chain surface for no requirement the SPEC states.
- The Kernel and Gateway are two processes of one operator-controlled system
  (§5.1–§5.2), not mutually distrusting parties. §5.2's "signing key" / "verification
  key" language describes the two *roles*; under a symmetric MAC both roles are
  served by the same secret, held independently by each process.
- The security property the tokens exist to provide — making floor-breach-via-race
  (T4) and replay/forgery (T3) fail closed — is fully delivered by an HMAC whose
  canonical serialization is byte-stable and whose verification is single-use and
  TTL-bound.

## Consequences

- **Positive:** No new dependency; a small, auditable surface; constant-time
  verification; the token's forgery matrix (bit-flip, mutation, expiry,
  partial-field forgery, replay) fails completely, pinned bit-by-bit in tests.
- **Trade-off (accepted):** The verification key *is* the forgery key. Anyone who
  can verify tokens can also mint them, so the shared secret must be protected on
  **both** the Kernel and Gateway sides with equal care. It is loaded only through
  an injected secrets seam (`SigningKeyHandle.from_env`, a hex-encoded environment
  variable), never from a config file, and the handle never exposes the key via
  `repr`, `vars()`, pickle, logs, exception messages, or ledger payloads. A future
  EPIC_01 keyring replaces the env-var loader without changing this decision.
- **Boundary:** Only `windbreak.riskkernel` may import the signing-key handle
  (`windbreak.riskkernel.signing`); `tokens.py` is its sole importer. The shared,
  Gateway-consumable `windbreak.tokens` verification module recomputes the HMAC
  from an injected key and never imports the handle, preserving the §5.3 import
  boundary enforced by the AST scanner in `tests/riskkernel/test_process_isolation.py`
  and the `.importlinter` contract.

## Revisiting

Move to an asymmetric signature (Ed25519 via `cryptography`) if a future
deployment places the Gateway in a lower-trust boundary than the Kernel — e.g. a
third-party or network-exposed submission service that must verify but must never
be able to mint tokens. That change would supersede this ADR.
