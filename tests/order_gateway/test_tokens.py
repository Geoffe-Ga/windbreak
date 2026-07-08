"""Failing-first tests for `windbreak.order_gateway.tokens` (issue #37, RED).

`windbreak/order_gateway/tokens.py` does not exist yet (only the empty package
marker `windbreak/order_gateway/__init__.py` does), so importing it fails
collection with `ModuleNotFoundError: No module named
'windbreak.order_gateway.tokens'` -- the expected Gate 1 RED state for issue
#37.

This module pins `verify_and_consume`'s pinned step order and full branch
coverage: `intent_matches_claims` runs *first* (comparing exactly the 7 SPEC
S10.6 fields `intent_id`/`market_ticker`/`outcome`/`action`/`price.value`/
`size.value`/`idempotency_key` -- never the 2 intent-only or 4 claims-only
fields), so a mismatch returns `INTENT_MISMATCH` *without* touching the
single-use registry -- proven by re-verifying the identical token against the
true, matching intent afterward. Only once the intent matches does
`verify_and_consume` delegate to the shared `windbreak.tokens.verify.verify_token`
and map its `reason` string onto a `VerifyResult`: `valid=True` -> `OK`;
"signature mismatch" -> `BAD_SIGNATURE`; "token expired" -> `EXPIRED`; "token
already consumed" -> `REPLAYED`; anything else (a malformed-hex "verification
error: ...", or any other raised exception `verify_token` itself catches
fail-closed) -> `REJECTED`. A dedicated 256-way bit-flip sweep (32 signature
bytes * 8 bits) proves no single-bit corruption of a valid signature is ever
misclassified as `OK`.
"""

from __future__ import annotations

import dataclasses

import pytest

from tests.order_gateway.conftest import (
    DEFAULT_NOW_EPOCH_S,
    KEY_MATERIAL,
    issue_matching_token,
    make_claims_for_intent,
    make_intent,
)
from windbreak.numeric.types import (
    ContractCentis,
    MoneyMicros,
    PricePips,
    ProbabilityPpm,
)
from windbreak.order_gateway.tokens import (
    VerifyResult,
    intent_matches_claims,
    verify_and_consume,
)
from windbreak.riskkernel.signing import SigningKeyHandle
from windbreak.riskkernel.tokens import TokenIssuer
from windbreak.tokens.verify import InMemorySingleUseRegistry

#: The 7 SPEC S10.6 fields `intent_matches_claims` compares, each paired with
#: a value that differs from `make_intent()`'s own default for that field --
#: used to prove a single-field mismatch (and nothing else) flips the verdict.
_COMPARED_FIELD_MISMATCHES = (
    ("intent_id", "intent-DIFFERENT"),
    ("market_ticker", "OTHER-TICKER"),
    ("outcome", "no"),
    ("action", "sell_to_close"),
    ("price", PricePips(9999)),
    ("size", ContractCentis(1)),
    ("idempotency_key", "idem-DIFFERENT"),
)


def test_verify_result_has_exactly_the_six_pinned_members() -> None:
    """`VerifyResult` names exactly the 6 pinned outcomes -- no more, no
    fewer -- so an added/renamed/removed member is caught here directly.
    """
    assert {member.name for member in VerifyResult} == {
        "OK",
        "INTENT_MISMATCH",
        "BAD_SIGNATURE",
        "EXPIRED",
        "REPLAYED",
        "REJECTED",
    }


def test_intent_matches_claims_true_for_a_matching_pair() -> None:
    """Claims built to mirror an intent's 7 compared fields match it."""
    intent = make_intent()
    claims = make_claims_for_intent(intent)

    assert intent_matches_claims(intent, claims) is True


@pytest.mark.parametrize("field,mismatched_value", _COMPARED_FIELD_MISMATCHES)
def test_intent_matches_claims_false_when_one_compared_field_differs(
    field: str, mismatched_value: object
) -> None:
    """Mismatching exactly one of the 7 compared fields flips the verdict to
    `False`, proving that field (and not some other coincidence) drove it.
    """
    intent = make_intent()
    claims = make_claims_for_intent(intent)
    mismatched_intent = dataclasses.replace(intent, **{field: mismatched_value})

    assert intent_matches_claims(mismatched_intent, claims) is False


def test_intent_matches_claims_ignores_every_uncompared_field() -> None:
    """`intent.max_notional`/`.implied_probability` and
    `claims.max_fee_micros`/`.config_hash`/`.kernel_sequence_number`/
    `.expires_at` never affect the match verdict -- only the 7 compared
    fields do.
    """
    intent = make_intent(
        max_notional=MoneyMicros(999_999_999),
        implied_probability=ProbabilityPpm(1),
    )
    claims = make_claims_for_intent(
        intent,
        max_fee_micros=MoneyMicros(1),
        config_hash="totally-different-hash",
        kernel_sequence_number=999,
        expires_at=DEFAULT_NOW_EPOCH_S + 3600,
    )

    assert intent_matches_claims(intent, claims) is True


@pytest.mark.parametrize("field,mismatched_value", _COMPARED_FIELD_MISMATCHES)
def test_verify_and_consume_intent_mismatch_does_not_burn_the_registry(
    field: str, mismatched_value: object
) -> None:
    """A field-mismatched call returns `INTENT_MISMATCH` *without* consuming
    the single-use registry slot: the same token then verifies `OK` against
    the true, matching intent -- proof that the match check runs, and fails,
    strictly before `verify_token`'s registry-consuming step.
    """
    true_intent = make_intent()
    token = issue_matching_token(true_intent)
    mismatched_intent = dataclasses.replace(true_intent, **{field: mismatched_value})
    registry = InMemorySingleUseRegistry()

    mismatched_result = verify_and_consume(
        token,
        mismatched_intent,
        key=KEY_MATERIAL,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        registry=registry,
    )
    true_result = verify_and_consume(
        token,
        true_intent,
        key=KEY_MATERIAL,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        registry=registry,
    )

    assert mismatched_result is VerifyResult.INTENT_MISMATCH
    assert true_result is VerifyResult.OK


def test_verify_and_consume_matching_intent_and_claims_is_ok() -> None:
    """A freshly minted, matching, unexpired, unconsumed token verifies `OK`."""
    intent = make_intent()
    token = issue_matching_token(intent)
    registry = InMemorySingleUseRegistry()

    result = verify_and_consume(
        token,
        intent,
        key=KEY_MATERIAL,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        registry=registry,
    )

    assert result is VerifyResult.OK


def test_verify_and_consume_wrong_key_returns_bad_signature() -> None:
    """Verifying under a key different from the signing key is `BAD_SIGNATURE`."""
    intent = make_intent()
    token = issue_matching_token(intent, key_material=KEY_MATERIAL)
    wrong_key = b"z" * 32
    registry = InMemorySingleUseRegistry()

    result = verify_and_consume(
        token, intent, key=wrong_key, now_epoch_s=DEFAULT_NOW_EPOCH_S, registry=registry
    )

    assert result is VerifyResult.BAD_SIGNATURE


def test_verify_and_consume_now_equal_to_expires_at_is_expired() -> None:
    """`now_epoch_s == claims.expires_at` is the expiry boundary: expired."""
    intent = make_intent()
    claims = make_claims_for_intent(intent, expires_at=DEFAULT_NOW_EPOCH_S)
    token = TokenIssuer(SigningKeyHandle(KEY_MATERIAL)).issue(claims)
    registry = InMemorySingleUseRegistry()

    result = verify_and_consume(
        token,
        intent,
        key=KEY_MATERIAL,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        registry=registry,
    )

    assert result is VerifyResult.EXPIRED


def test_verify_and_consume_now_one_second_before_expiry_is_not_expired() -> None:
    """One second before the boundary, the identical token is still `OK`."""
    intent = make_intent()
    claims = make_claims_for_intent(intent, expires_at=DEFAULT_NOW_EPOCH_S)
    token = TokenIssuer(SigningKeyHandle(KEY_MATERIAL)).issue(claims)
    registry = InMemorySingleUseRegistry()

    result = verify_and_consume(
        token,
        intent,
        key=KEY_MATERIAL,
        now_epoch_s=DEFAULT_NOW_EPOCH_S - 1,
        registry=registry,
    )

    assert result is VerifyResult.OK


def test_verify_and_consume_second_verification_of_same_token_is_replayed() -> None:
    """A second verification of the identical token against the same
    registry is `REPLAYED`, never a second `OK`.
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    registry = InMemorySingleUseRegistry()

    first = verify_and_consume(
        token,
        intent,
        key=KEY_MATERIAL,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        registry=registry,
    )
    second = verify_and_consume(
        token,
        intent,
        key=KEY_MATERIAL,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        registry=registry,
    )

    assert first is VerifyResult.OK
    assert second is VerifyResult.REPLAYED


@pytest.mark.parametrize("bad_signature_hex", ["not-hex-zz", "abc"])
def test_verify_and_consume_malformed_signature_hex_is_rejected(
    bad_signature_hex: str,
) -> None:
    """A signature that is not valid hex (non-hex characters, or an odd
    length that `bytes.fromhex` cannot pair up) is fail-closed `REJECTED`,
    not any other verdict. (An *empty* string is deliberately excluded: it is
    valid hex -- `bytes.fromhex("") == b""` -- so it exercises `BAD_SIGNATURE`
    via a length-mismatched `hmac.compare_digest`, not this `REJECTED` path.)
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    bad_token = dataclasses.replace(token, signature_hex=bad_signature_hex)
    registry = InMemorySingleUseRegistry()

    result = verify_and_consume(
        bad_token,
        intent,
        key=KEY_MATERIAL,
        now_epoch_s=DEFAULT_NOW_EPOCH_S,
        registry=registry,
    )

    assert result is VerifyResult.REJECTED


def test_verify_and_consume_every_single_bit_flip_in_the_signature_never_verifies() -> (
    None
):
    """Flipping any one of the 256 bits (32 signature bytes * 8) of a valid
    token's signature never yields `OK`. Each flipped variant gets its own
    fresh registry, so a coincidental single-use collision can never mask a
    real verification bypass.
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    original_bytes = bytes.fromhex(token.signature_hex)

    for byte_index in range(len(original_bytes)):
        for bit_index in range(8):
            flipped = bytearray(original_bytes)
            flipped[byte_index] ^= 1 << bit_index
            flipped_token = dataclasses.replace(
                token, signature_hex=bytes(flipped).hex()
            )
            registry = InMemorySingleUseRegistry()

            result = verify_and_consume(
                flipped_token,
                intent,
                key=KEY_MATERIAL,
                now_epoch_s=DEFAULT_NOW_EPOCH_S,
                registry=registry,
            )

            assert result is not VerifyResult.OK, (
                f"byte {byte_index} bit {bit_index} verified as OK"
            )
