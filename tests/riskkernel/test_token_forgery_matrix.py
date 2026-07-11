"""Forgery/replay matrix for approval-token verification (issue #31, RED).

`verify_token` is the single gate between a signed approval token and the
Gateway acting on it, so this file exhaustively tries to break it: every
single-bit flip of the signature, every claims field perturbed individually
(both re-checked against an unrelated, still-valid signature), a field
spliced in from a second, differently signed token, replay of a legitimate
token, non-consumption of the single-use slot on a *failed* verification,
the exact expiry boundary, the wrong key, and malformed signature encodings.
A closing Hypothesis property test generalizes (a)/(b) over random claims.

None of `windbreak/tokens/verify.py`, `windbreak/riskkernel/tokens.py`, or
`windbreak/riskkernel/signing.py`'s real `SigningKeyHandle` exist yet, so
every import below fails collection -- the expected Gate 1 RED state for
issue #31.

Every case here is fully deterministic: injectable `now_epoch_s` integers,
no `time.time()`, no `sleep`, no threads.
"""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.riskkernel.test_tokens import make_claims
from windbreak.numeric.types import ContractCentis, MoneyMicros, PricePips
from windbreak.riskkernel.signing import SigningKeyHandle
from windbreak.riskkernel.tokens import TokenIssuer
from windbreak.tokens.verify import (
    ApprovalTokenClaims,
    InMemorySingleUseRegistry,
    SignedApprovalToken,
    verify_token,
)

#: The signing key every forgery in this file is issued (or wrongly
#: verified) under.
_KEY_MATERIAL = b"k" * 32
#: A different, equally valid-shaped key, for the wrong-key case.
_WRONG_KEY_MATERIAL = b"w" * 32

#: A fixed base claims set, expiring well after every `now_epoch_s` used
#: below except the dedicated expiry-boundary tests (which override it).
_BASE_CLAIMS = make_claims(expires_at=2_000_000_000)
_VALID_NOW = 1_000_000_000

#: Every `ApprovalTokenClaims` field name, in declaration order.
_ALL_CLAIMS_FIELDS: tuple[str, ...] = tuple(
    field.name for field in dataclasses.fields(ApprovalTokenClaims)
)

#: A second, fully distinct claims set (every field different from
#: `_BASE_CLAIMS`), used by the partial-field-splice tests.
_OTHER_CLAIMS = ApprovalTokenClaims(
    intent_id="intent-9999",
    market_ticker="OTHER-TICKER",
    outcome="no",
    action="sell_to_close",
    limit_price_pips=PricePips(1234),
    count_centis=ContractCentis(4321),
    max_fee_micros=MoneyMicros(999_999),
    expires_at=_BASE_CLAIMS.expires_at + 100,
    idempotency_key="idem-9999",
    config_hash="cfg-hash-other",
    kernel_sequence_number=_BASE_CLAIMS.kernel_sequence_number + 1,
)


def _issue(
    claims: ApprovalTokenClaims, key: bytes = _KEY_MATERIAL
) -> SignedApprovalToken:
    """Sign `claims` under `key` via a fresh `TokenIssuer`.

    Args:
        claims: The claims to sign.
        key: The (>=32-byte) key material to sign under.

    Returns:
        The resulting `SignedApprovalToken`.
    """
    return TokenIssuer(SigningKeyHandle(key)).issue(claims)


def _fresh_registry() -> InMemorySingleUseRegistry:
    """Return a never-consumed `InMemorySingleUseRegistry`."""
    return InMemorySingleUseRegistry()


def _perturb(claims: ApprovalTokenClaims, field: str) -> ApprovalTokenClaims:
    """Return `claims` with exactly one field changed by the smallest
    meaningful delta for its type: `+"!"` for a string, `+1` for a plain
    `int`, or `+1` on the scaled `.value` for a scaled-unit type.

    Args:
        claims: The claims to perturb.
        field: The single field name to change.

    Returns:
        A new `ApprovalTokenClaims` differing from `claims` in exactly one
        field.

    Raises:
        TypeError: If `field` names a value of an unhandled type -- guards
            against silently skipping a field SPEC S10.6 adds later.
    """
    value = getattr(claims, field)
    new_value: object
    if isinstance(value, str):
        new_value = value + "!"
    elif isinstance(value, (PricePips, ContractCentis, MoneyMicros)):
        new_value = type(value)(value.value + 1)
    elif isinstance(value, int):
        new_value = value + 1
    else:
        raise TypeError(f"unhandled claims field type for {field!r}: {type(value)!r}")
    return dataclasses.replace(claims, **{field: new_value})


# --- (a) every single-bit flip of the 32-byte signature -------------------------


def test_every_single_bit_flip_of_the_signature_fails_verification() -> None:
    """Flipping any one of the 32 signature bytes' 8 bits (256 total
    mutants) always fails verification -- the comparison is sensitive to
    every bit, not just gross corruption.
    """
    token = _issue(_BASE_CLAIMS)
    original_bytes = bytes.fromhex(token.signature_hex)
    assert len(original_bytes) == 32

    for byte_index in range(32):
        for bit_index in range(8):
            mutated = bytearray(original_bytes)
            mutated[byte_index] ^= 1 << bit_index
            forged = dataclasses.replace(token, signature_hex=bytes(mutated).hex())

            result = verify_token(
                forged,
                key=_KEY_MATERIAL,
                now_epoch_s=_VALID_NOW,
                registry=_fresh_registry(),
            )

            detail = f"byte {byte_index} bit {bit_index} unexpectedly verified"
            assert result.valid is False, detail


# --- (b) each claims field perturbed, re-checked against the original signature -


@pytest.mark.parametrize("field", _ALL_CLAIMS_FIELDS)
def test_perturbing_any_single_claims_field_fails_the_original_signature(
    field: str,
) -> None:
    """Each of the 11 claims fields, perturbed by one character or one unit,
    fails verification when re-checked against the original, now-stale
    signature -- the signature covers every field, not a subset.
    """
    token = _issue(_BASE_CLAIMS)
    forged_claims = _perturb(_BASE_CLAIMS, field)
    forged = SignedApprovalToken(
        claims=forged_claims, signature_hex=token.signature_hex
    )

    result = verify_token(
        forged, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=_fresh_registry()
    )

    assert result.valid is False


# --- (c) partial-field forgery: splice one field from a second, valid token -----


@pytest.mark.parametrize("field", _ALL_CLAIMS_FIELDS)
def test_splicing_any_single_field_from_another_token_fails_verification(
    field: str,
) -> None:
    """Token A's signature, replayed against claims spliced from a second,
    validly signed token B in exactly one field, fails verification for
    every one of the 11 fields -- no single field can be silently swapped in
    from a different token.
    """
    token_a = _issue(_BASE_CLAIMS)
    spliced_claims = dataclasses.replace(
        _BASE_CLAIMS, **{field: getattr(_OTHER_CLAIMS, field)}
    )
    forged = SignedApprovalToken(
        claims=spliced_claims, signature_hex=token_a.signature_hex
    )

    result = verify_token(
        forged, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=_fresh_registry()
    )

    assert result.valid is False


# --- (d) replay and non-consumption-on-failure ----------------------------------


def test_replaying_a_valid_token_fails_on_the_second_verification() -> None:
    """A legitimate token verifies once; a second `verify_token` call against
    the same registry fails via the single-use registry.
    """
    token = _issue(_BASE_CLAIMS)
    registry = _fresh_registry()

    first = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )
    second = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )

    assert first.valid is True
    assert second.valid is False


def test_replaying_a_valid_token_with_an_uppercase_signature_fails() -> None:
    """An uppercase re-spelling of an already-consumed signature still
    authenticates (hex decoding is case-insensitive) but must be rejected by
    the single-use registry as the same, already-consumed token, not
    accepted as a distinct one.
    """
    token = _issue(_BASE_CLAIMS)
    registry = _fresh_registry()
    uppercased = dataclasses.replace(token, signature_hex=token.signature_hex.upper())

    first = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )
    second = verify_token(
        uppercased, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )

    assert first.valid is True
    assert second.valid is False
    assert second.reason == "token already consumed"


def test_replaying_a_valid_token_with_whitespace_in_the_signature_fails() -> None:
    """A whitespace-interleaved re-spelling of an already-consumed signature
    still authenticates (hex decoding ignores ASCII whitespace) but must be
    rejected by the single-use registry as the same, already-consumed token.
    """
    token = _issue(_BASE_CLAIMS)
    registry = _fresh_registry()
    spaced_hex = " ".join(
        token.signature_hex[i : i + 2] for i in range(0, len(token.signature_hex), 2)
    )
    spaced = dataclasses.replace(token, signature_hex=spaced_hex)

    first = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )
    second = verify_token(
        spaced, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )

    assert first.valid is True
    assert second.valid is False
    assert second.reason == "token already consumed"


def test_replaying_uppercase_then_original_signature_fails_the_second_call() -> None:
    """The single-use key must be spelling-independent, not merely
    lowercase-favoring: consuming the uppercase spelling first still leaves
    the original lowercase spelling of the same signature rejected as
    already consumed.
    """
    token = _issue(_BASE_CLAIMS)
    registry = _fresh_registry()
    uppercased = dataclasses.replace(token, signature_hex=token.signature_hex.upper())

    first = verify_token(
        uppercased, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )
    second = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )

    assert first.valid is True
    assert second.valid is False
    assert second.reason == "token already consumed"


def test_a_forged_signature_does_not_consume_the_single_use() -> None:
    """A verification that fails on signature never touches the registry, so
    the legitimate token can still be verified once, afterward.
    """
    token = _issue(_BASE_CLAIMS)
    forged = dataclasses.replace(token, signature_hex="00" * 32)
    registry = _fresh_registry()

    forged_result = verify_token(
        forged, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )
    legit_result = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=registry
    )

    assert forged_result.valid is False
    assert legit_result.valid is True


def test_an_expired_verification_does_not_consume_the_single_use() -> None:
    """An expired verification never reaches the registry either: verifying
    at `now == expires_at` fails (expired) but does not burn the single use,
    so verifying the same signature again one second earlier still succeeds.
    """
    claims = dataclasses.replace(_BASE_CLAIMS, expires_at=1_000)
    token = _issue(claims)
    registry = _fresh_registry()

    expired_result = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=1_000, registry=registry
    )
    still_unused_result = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=999, registry=registry
    )

    assert expired_result.valid is False
    assert still_unused_result.valid is True


# --- (e) expiry boundary --------------------------------------------------------


def test_expiry_boundary_now_equal_to_expires_at_fails() -> None:
    """`now_epoch_s == expires_at` fails -- expiry is exclusive."""
    claims = dataclasses.replace(_BASE_CLAIMS, expires_at=5_000)
    token = _issue(claims)

    result = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=5_000, registry=_fresh_registry()
    )

    assert result.valid is False


def test_expiry_boundary_now_after_expires_at_fails() -> None:
    """`now_epoch_s > expires_at` fails."""
    claims = dataclasses.replace(_BASE_CLAIMS, expires_at=5_000)
    token = _issue(claims)

    result = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=5_001, registry=_fresh_registry()
    )

    assert result.valid is False


def test_expiry_boundary_one_second_before_expires_at_passes() -> None:
    """`now_epoch_s == expires_at - 1` passes -- the last valid instant."""
    claims = dataclasses.replace(_BASE_CLAIMS, expires_at=5_000)
    token = _issue(claims)

    result = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=4_999, registry=_fresh_registry()
    )

    assert result.valid is True


# --- (f) wrong key, malformed/truncated/empty signature -------------------------


def test_verification_fails_under_the_wrong_key() -> None:
    """A token verified under a different (equally well-formed) key fails."""
    token = _issue(_BASE_CLAIMS)

    result = verify_token(
        token,
        key=_WRONG_KEY_MATERIAL,
        now_epoch_s=_VALID_NOW,
        registry=_fresh_registry(),
    )

    assert result.valid is False


@pytest.mark.parametrize(
    "signature_hex",
    ["", "not-hex-at-all", "ab" * 16, "ab" * 33, "a"],
    ids=["empty", "non_hex", "truncated_16_bytes", "oversized_33_bytes", "odd_length"],
)
def test_verification_fails_for_malformed_or_wrong_length_signatures(
    signature_hex: str,
) -> None:
    """An empty, non-hex, truncated, oversized, or odd-length signature all
    fail verification -- fail-closed on any hex-decode error.
    """
    token = _issue(_BASE_CLAIMS)
    forged = dataclasses.replace(token, signature_hex=signature_hex)

    result = verify_token(
        forged, key=_KEY_MATERIAL, now_epoch_s=_VALID_NOW, registry=_fresh_registry()
    )

    assert result.valid is False


# --- Hypothesis property: issue-then-verify always passes; any single-field ----
# --- mutation after issuing always fails ----------------------------------------

_claims_text_strategy = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=20
)
_price_strategy = st.integers(min_value=0, max_value=10_000)
_size_strategy = st.integers(min_value=1, max_value=100_000)
_fee_strategy = st.integers(min_value=0, max_value=1_000_000)
_expires_strategy = st.integers(min_value=2_000_000_000, max_value=3_000_000_000)
_sequence_strategy = st.integers(min_value=1, max_value=1_000_000)


def _claims_from_raw(
    *,
    intent_id: str,
    market_ticker: str,
    outcome: str,
    action: str,
    price: int,
    size: int,
    fee: int,
    expires_at: int,
    idempotency_key: str,
    config_hash: str,
    sequence_number: int,
) -> ApprovalTokenClaims:
    """Assemble an `ApprovalTokenClaims` from Hypothesis-generated raw
    primitives, wrapping the scaled fields in their unit types.
    """
    return ApprovalTokenClaims(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        limit_price_pips=PricePips(price),
        count_centis=ContractCentis(size),
        max_fee_micros=MoneyMicros(fee),
        expires_at=expires_at,
        idempotency_key=idempotency_key,
        config_hash=config_hash,
        kernel_sequence_number=sequence_number,
    )


@given(
    intent_id=_claims_text_strategy,
    market_ticker=_claims_text_strategy,
    outcome=st.sampled_from(["yes", "no"]),
    action=st.sampled_from(["buy", "sell_to_close"]),
    price=_price_strategy,
    size=_size_strategy,
    fee=_fee_strategy,
    expires_at=_expires_strategy,
    idempotency_key=_claims_text_strategy,
    config_hash=_claims_text_strategy,
    sequence_number=_sequence_strategy,
)
def test_property_issue_then_verify_always_passes_for_random_claims(
    intent_id: str,
    market_ticker: str,
    outcome: str,
    action: str,
    price: int,
    size: int,
    fee: int,
    expires_at: int,
    idempotency_key: str,
    config_hash: str,
    sequence_number: int,
) -> None:
    """For any well-typed claims, issuing then immediately verifying (well
    before expiry, against a fresh registry) always succeeds.
    """
    claims = _claims_from_raw(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        price=price,
        size=size,
        fee=fee,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
        config_hash=config_hash,
        sequence_number=sequence_number,
    )
    token = _issue(claims)

    result = verify_token(
        token, key=_KEY_MATERIAL, now_epoch_s=1_000_000_000, registry=_fresh_registry()
    )

    assert result.valid is True


@given(
    intent_id=_claims_text_strategy,
    market_ticker=_claims_text_strategy,
    outcome=st.sampled_from(["yes", "no"]),
    action=st.sampled_from(["buy", "sell_to_close"]),
    price=_price_strategy,
    size=_size_strategy,
    fee=_fee_strategy,
    expires_at=_expires_strategy,
    idempotency_key=_claims_text_strategy,
    config_hash=_claims_text_strategy,
    sequence_number=_sequence_strategy,
    field=st.sampled_from(_ALL_CLAIMS_FIELDS),
)
def test_property_any_single_field_mutation_after_issuing_always_fails(
    intent_id: str,
    market_ticker: str,
    outcome: str,
    action: str,
    price: int,
    size: int,
    fee: int,
    expires_at: int,
    idempotency_key: str,
    config_hash: str,
    sequence_number: int,
    field: str,
) -> None:
    """For any well-typed claims, mutating exactly one field after issuing
    (keeping the original signature) always fails verification.
    """
    claims = _claims_from_raw(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        price=price,
        size=size,
        fee=fee,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
        config_hash=config_hash,
        sequence_number=sequence_number,
    )
    token = _issue(claims)
    mutated_claims = _perturb(claims, field)
    forged = SignedApprovalToken(
        claims=mutated_claims, signature_hex=token.signature_hex
    )

    result = verify_token(
        forged, key=_KEY_MATERIAL, now_epoch_s=1_000_000_000, registry=_fresh_registry()
    )

    assert result.valid is False
