"""Failing-first tests for approval-token signing and serialization (issue #31, RED).

Issue #31 gives the Risk Kernel real approval-token machinery:

    * :mod:`hedgekit.riskkernel.signing` -- `SigningKeyHandle` grows real
      HMAC-SHA256 signing over injectable key material, plus
      `SigningKeyHandle.from_env` to load that key material from an
      environment mapping.
    * :mod:`hedgekit.tokens.verify` -- a *shared*, Gateway-consumable module
      (never importing `hedgekit.riskkernel.signing`) defining
      `ApprovalTokenClaims` (SPEC S10.6), the exact canonical byte encoding
      those claims are signed over (`canonical_claims_bytes`), and the
      `SignedApprovalToken` / `VerificationResult` / `SingleUseRegistry`
      shapes `verify_token` operates on.
    * :mod:`hedgekit.riskkernel.tokens` -- `TokenIssuer`, pairing a
      `SigningKeyHandle` with `canonical_claims_bytes` to produce a
      `SignedApprovalToken`.

None of `hedgekit/riskkernel/tokens.py`, `hedgekit/tokens/__init__.py`, or
`hedgekit/tokens/verify.py` exist yet, and `SigningKeyHandle` does not yet
accept key material, so every import and construction below fails at
collection or at call time -- the expected Gate 1 RED state for issue #31.

This file pins: a known-answer HMAC-SHA256 test (so a serialization-drift
mutant changes the signature, not just "some bytes come back");
`SigningKeyHandle.from_env`'s happy path and its three `ValueError` failure
modes (missing var, bad hex, short key); that neither `repr()` nor pickling
ever leaks key bytes; a byte-exact "golden" canonical encoding; a
field-boundary-shift ambiguity check; that every scaled type serializes as
its bare `.value` integer; that the domain prefix is present;
`TokenIssuer.issue` round-tripping through `verify_token`; and
`DEFAULT_TOKEN_TTL_SECONDS == 60`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import pickle
from dataclasses import FrozenInstanceError

import pytest

from hedgekit.ledger.events import canonical_json
from hedgekit.numeric.types import ContractCentis, MoneyMicros, PricePips
from hedgekit.riskkernel.signing import SigningKeyHandle
from hedgekit.riskkernel.tokens import DEFAULT_TOKEN_TTL_SECONDS, TokenIssuer
from hedgekit.tokens.verify import (
    ApprovalTokenClaims,
    InMemorySingleUseRegistry,
    SignedApprovalToken,
    VerificationResult,
    canonical_claims_bytes,
    verify_token,
)

#: The domain-separation prefix SPEC S10.6 prepends to every canonical
#: encoding, so an approval-token signature can never be replayed as a
#: signature over some other hedgekit-signed artifact.
_DOMAIN_PREFIX = b"hedgekit.approval-token.v1\x00"

#: A fixed, valid (>=32-byte) key used throughout this file's known-answer
#: and round-trip tests.
_KEY_MATERIAL = b"k" * 32

#: Immutable scaled-int / plain defaults for :func:`make_claims`, held as
#: module-level singletons so they are not reconstructed in the function's
#: argument defaults (ruff B008); the wrapper types are frozen, so sharing
#: one instance is safe.
_DEFAULT_INTENT_ID = "intent-0001"
_DEFAULT_MARKET_TICKER = "PRES-2028-DEM"
_DEFAULT_OUTCOME = "yes"
_DEFAULT_ACTION = "buy"
_DEFAULT_LIMIT_PRICE_PIPS = PricePips(5000)
_DEFAULT_COUNT_CENTIS = ContractCentis(1000)
_DEFAULT_MAX_FEE_MICROS = MoneyMicros(450_000)
_DEFAULT_EXPIRES_AT = 1_700_000_060
_DEFAULT_IDEMPOTENCY_KEY = "idem-0001"
_DEFAULT_CONFIG_HASH = "cfg-hash-abc123"
_DEFAULT_KERNEL_SEQUENCE_NUMBER = 1


def make_claims(
    *,
    intent_id: str = _DEFAULT_INTENT_ID,
    market_ticker: str = _DEFAULT_MARKET_TICKER,
    outcome: str = _DEFAULT_OUTCOME,
    action: str = _DEFAULT_ACTION,
    limit_price_pips: PricePips = _DEFAULT_LIMIT_PRICE_PIPS,
    count_centis: ContractCentis = _DEFAULT_COUNT_CENTIS,
    max_fee_micros: MoneyMicros = _DEFAULT_MAX_FEE_MICROS,
    expires_at: int = _DEFAULT_EXPIRES_AT,
    idempotency_key: str = _DEFAULT_IDEMPOTENCY_KEY,
    config_hash: str = _DEFAULT_CONFIG_HASH,
    kernel_sequence_number: int = _DEFAULT_KERNEL_SEQUENCE_NUMBER,
) -> ApprovalTokenClaims:
    """Build a valid `ApprovalTokenClaims`, with any field overridable by
    keyword. Reused by `tests/riskkernel/test_token_forgery_matrix.py`.

    Args:
        intent_id: The approved intent's unique identifier.
        market_ticker: The exchange ticker the intent targets.
        outcome: The market outcome the intent trades (e.g. "yes"/"no").
        action: The trade action (e.g. "buy"/"sell_to_close").
        limit_price_pips: The limit price, in pips.
        count_centis: The contract count, in centis.
        max_fee_micros: The combined worst-case trading + settlement fee cap.
        expires_at: The token's expiry, in epoch seconds.
        idempotency_key: The caller-supplied idempotency key.
        config_hash: The configuration revision hash active at approval time.
        kernel_sequence_number: The reservation-ledger sequence number.

    Returns:
        A fully populated, valid `ApprovalTokenClaims`.
    """
    return ApprovalTokenClaims(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        limit_price_pips=limit_price_pips,
        count_centis=count_centis,
        max_fee_micros=max_fee_micros,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
        config_hash=config_hash,
        kernel_sequence_number=kernel_sequence_number,
    )


# --- SigningKeyHandle: known-answer HMAC-SHA256, construction, from_env ---------


def test_signing_key_handle_sign_is_hmac_sha256_known_answer() -> None:
    """`SigningKeyHandle(key).sign(payload)` equals an independently computed
    `hmac.new(key, payload, sha256).digest()` -- the known-answer test any
    serialization- or algorithm-drift mutant must fail.
    """
    handle = SigningKeyHandle(_KEY_MATERIAL)
    payload = b"some canonical payload bytes"

    signature = handle.sign(payload)

    assert signature == hmac.new(_KEY_MATERIAL, payload, hashlib.sha256).digest()


def test_signing_key_handle_rejects_key_material_shorter_than_32_bytes() -> None:
    """Key material under 32 bytes is rejected at construction."""
    with pytest.raises(ValueError, match="32"):
        SigningKeyHandle(b"x" * 31)


def test_signing_key_handle_accepts_exactly_32_bytes() -> None:
    """32 bytes is the minimum accepted length (the boundary passes)."""
    handle = SigningKeyHandle(b"x" * 32)

    assert handle.sign(b"p") == hmac.new(b"x" * 32, b"p", hashlib.sha256).digest()


def test_signing_key_handle_repr_does_not_leak_key_bytes() -> None:
    """`repr()` never contains the raw key material, hex or otherwise."""
    handle = SigningKeyHandle(_KEY_MATERIAL)

    rendered = repr(handle)

    assert _KEY_MATERIAL.hex() not in rendered
    assert _KEY_MATERIAL.decode("latin-1") not in rendered


def test_signing_key_handle_pickling_is_blocked() -> None:
    """Pickling a `SigningKeyHandle` raises `TypeError` -- a serialized
    handle would put the key material at rest outside its access boundary.
    """
    handle = SigningKeyHandle(_KEY_MATERIAL)

    with pytest.raises(TypeError):
        pickle.dumps(handle)


def test_signing_key_handle_from_env_happy_path_decodes_hex_key() -> None:
    """`from_env` hex-decodes the configured variable into working key
    material."""
    environ = {"HEDGEKIT_APPROVAL_TOKEN_KEY": _KEY_MATERIAL.hex()}

    handle = SigningKeyHandle.from_env(environ)

    assert handle.sign(b"x") == hmac.new(_KEY_MATERIAL, b"x", hashlib.sha256).digest()


def test_signing_key_handle_from_env_missing_var_raises_value_error() -> None:
    """A missing environment variable raises `ValueError` naming it."""
    with pytest.raises(ValueError, match="HEDGEKIT_APPROVAL_TOKEN_KEY"):
        SigningKeyHandle.from_env({})


def test_signing_key_handle_from_env_undecodable_hex_raises_value_error() -> None:
    """A value that is not valid hex raises `ValueError`."""
    with pytest.raises(ValueError):
        SigningKeyHandle.from_env({"HEDGEKIT_APPROVAL_TOKEN_KEY": "not-hex-at-all!!"})


def test_signing_key_handle_from_env_short_key_raises_value_error() -> None:
    """A hex-decodable but too-short (<32 byte) key raises `ValueError`."""
    short_key_hex = (b"k" * 16).hex()

    with pytest.raises(ValueError, match="32"):
        SigningKeyHandle.from_env({"HEDGEKIT_APPROVAL_TOKEN_KEY": short_key_hex})


def test_signing_key_handle_from_env_uses_a_custom_var_name() -> None:
    """`var=` overrides the default environment variable name."""
    environ = {"CUSTOM_KEY_VAR": _KEY_MATERIAL.hex()}

    handle = SigningKeyHandle.from_env(environ, var="CUSTOM_KEY_VAR")

    assert handle.sign(b"x") == hmac.new(_KEY_MATERIAL, b"x", hashlib.sha256).digest()


# --- ApprovalTokenClaims: field order, frozen, slotted --------------------------


def test_approval_token_claims_field_order_matches_spec_10_6_exactly() -> None:
    """The 11 fields appear in exactly the SPEC S10.6 order."""
    field_names = tuple(f.name for f in dataclasses.fields(ApprovalTokenClaims))

    assert field_names == (
        "intent_id",
        "market_ticker",
        "outcome",
        "action",
        "limit_price_pips",
        "count_centis",
        "max_fee_micros",
        "expires_at",
        "idempotency_key",
        "config_hash",
        "kernel_sequence_number",
    )


def test_approval_token_claims_is_frozen() -> None:
    """Mutating any field of a constructed `ApprovalTokenClaims` raises."""
    claims = make_claims()
    frozen_field = "intent_id"

    with pytest.raises(FrozenInstanceError):
        setattr(claims, frozen_field, "changed")


def test_approval_token_claims_is_slotted_with_no_instance_dict() -> None:
    """`slots=True` means no per-instance `__dict__` -- a stray attribute
    can never be smuggled onto a claims instance.
    """
    claims = make_claims()

    assert not hasattr(claims, "__dict__")


# --- canonical_claims_bytes: domain prefix, golden, boundaries, .value ----------


def test_canonical_claims_bytes_starts_with_the_domain_prefix() -> None:
    """Every encoding begins with the fixed domain-separation prefix."""
    encoded = canonical_claims_bytes(make_claims())

    assert encoded.startswith(_DOMAIN_PREFIX)


def test_canonical_claims_bytes_is_an_exact_golden_value() -> None:
    """A byte-exact golden encoding for one fixed set of claims -- pins the
    serialization format itself (field set, key names, `.value` extraction,
    JSON canonicalization), not merely "it round-trips".

    The expected dict is authored independently of `canonical_claims_bytes`
    (not reflected off the claims instance), then passed through the same
    `canonical_json` the production code is required to use -- so a typo in
    a hand-written byte literal can't silently make this test vacuous.
    """
    claims = make_claims(
        intent_id="AB",
        market_ticker="C",
        outcome="yes",
        action="buy",
        limit_price_pips=PricePips(5000),
        count_centis=ContractCentis(1000),
        max_fee_micros=MoneyMicros(450_000),
        expires_at=1_700_000_060,
        idempotency_key="idem-0001",
        config_hash="cfg-hash-abc123",
        kernel_sequence_number=1,
    )

    encoded = canonical_claims_bytes(claims)

    expected_dict: dict[str, object] = {
        "token_schema_version": 1,
        "intent_id": "AB",
        "market_ticker": "C",
        "outcome": "yes",
        "action": "buy",
        "limit_price_pips": 5000,
        "count_centis": 1000,
        "max_fee_micros": 450_000,
        "expires_at": 1_700_000_060,
        "idempotency_key": "idem-0001",
        "config_hash": "cfg-hash-abc123",
        "kernel_sequence_number": 1,
    }
    expected = _DOMAIN_PREFIX + canonical_json(expected_dict).encode("utf-8")

    assert encoded == expected


def test_canonical_claims_bytes_distinguishes_a_field_boundary_shift() -> None:
    """Shifting a character across the `intent_id`/`market_ticker` boundary
    (`"AB"` + `"C"` vs. `"A"` + `"BC"`) produces different canonical bytes --
    pins that fields are JSON-string-delimited, never naively concatenated.
    """
    claims_ab_c = make_claims(intent_id="AB", market_ticker="C")
    claims_a_bc = make_claims(intent_id="A", market_ticker="BC")

    assert canonical_claims_bytes(claims_ab_c) != canonical_claims_bytes(claims_a_bc)


def test_canonical_claims_bytes_serializes_scaled_types_as_bare_integers() -> None:
    """Every scaled-unit field serializes as its bare `.value` integer, never
    as a nested object or a string.
    """
    claims = make_claims(
        limit_price_pips=PricePips(4242),
        count_centis=ContractCentis(777),
        max_fee_micros=MoneyMicros(999),
    )

    encoded = canonical_claims_bytes(claims).decode("utf-8")

    assert '"limit_price_pips":4242' in encoded
    assert '"count_centis":777' in encoded
    assert '"max_fee_micros":999' in encoded


# --- SignedApprovalToken / VerificationResult: frozen ---------------------------


def test_signed_approval_token_is_frozen() -> None:
    """Mutating any field of a `SignedApprovalToken` raises."""
    token = SignedApprovalToken(claims=make_claims(), signature_hex="ab" * 32)
    frozen_field = "signature_hex"

    with pytest.raises(FrozenInstanceError):
        setattr(token, frozen_field, "00" * 32)


def test_verification_result_is_frozen() -> None:
    """Mutating any field of a `VerificationResult` raises."""
    result = VerificationResult(valid=True, reason="ok")
    frozen_field = "valid"

    with pytest.raises(FrozenInstanceError):
        setattr(result, frozen_field, False)


# --- InMemorySingleUseRegistry: consume exactly once per signature -------------


def test_in_memory_single_use_registry_consume_true_exactly_once() -> None:
    """The first `consume()` of a signature returns `True`; every subsequent
    `consume()` of that same signature returns `False`.
    """
    registry = InMemorySingleUseRegistry()
    signature = "ab" * 32

    first = registry.consume(signature)
    second = registry.consume(signature)

    assert first is True
    assert second is False


def test_in_memory_single_use_registry_tracks_signatures_independently() -> None:
    """Consuming one signature never marks a different signature as used."""
    registry = InMemorySingleUseRegistry()

    assert registry.consume("sig-a") is True
    assert registry.consume("sig-b") is True
    assert registry.consume("sig-a") is False
    assert registry.consume("sig-b") is False


# --- TokenIssuer: issues via canonical bytes + HMAC, round-trips ----------------


def test_token_issuer_issue_signature_is_the_exact_known_hmac_sha256_digest() -> None:
    """`TokenIssuer.issue` signs `canonical_claims_bytes(claims)` with
    HMAC-SHA256 under the handle's key -- computed independently here, so a
    serialization-drift mutant (a reordered or renamed field) changes the
    signature and fails this test.
    """
    handle = SigningKeyHandle(_KEY_MATERIAL)
    issuer = TokenIssuer(handle)
    claims = make_claims()

    token = issuer.issue(claims)

    expected_bytes = canonical_claims_bytes(claims)
    expected_signature = hmac.new(
        _KEY_MATERIAL, expected_bytes, hashlib.sha256
    ).digest()
    assert token.signature_hex == expected_signature.hex()
    assert token.claims == claims


def test_token_issuer_issue_round_trips_through_verify_token_with_the_same_key() -> (
    None
):
    """A freshly issued token verifies successfully under the same key,
    before its expiry, against a fresh registry.
    """
    handle = SigningKeyHandle(_KEY_MATERIAL)
    issuer = TokenIssuer(handle)
    claims = make_claims(expires_at=2_000_000_000)
    registry = InMemorySingleUseRegistry()

    token = issuer.issue(claims)
    result = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=1_999_999_999, registry=registry
    )

    assert result.valid is True


def test_default_token_ttl_seconds_is_60() -> None:
    """`DEFAULT_TOKEN_TTL_SECONDS` is exactly 60 (SPEC-pinned constant)."""
    assert DEFAULT_TOKEN_TTL_SECONDS == 60
