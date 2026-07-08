"""Shared fixtures/builders for `tests/order_gateway/*` (issue #37, RED).

None of `windbreak/order_gateway/{tokens,state_machine,gateway}.py` exist yet
-- only the empty package marker `windbreak/order_gateway/__init__.py` does --
so importing any of them fails collection with `ModuleNotFoundError: No
module named 'windbreak.order_gateway.tokens'` (or `.state_machine` /
`.gateway`), the expected Gate 1 RED state for issue #37. This module itself
imports only already-shipped machinery (`windbreak.riskkernel.{signing,tokens}`,
`windbreak.tokens.verify`, `windbreak.connector.paper`), so it collects cleanly
on its own; the `ModuleNotFoundError` surfaces from the three `test_*.py`
files that import the not-yet-existing Order Gateway modules directly.

Builder-placement choice mirrors `tests/riskkernel/conftest.py`: plain,
explicitly-imported functions (`make_intent`, `make_claims_for_intent`,
`issue_matching_token`) rather than pytest fixtures, so they compose cleanly
inside `@pytest.mark.parametrize`-driven scenario tables. `KEY_MATERIAL` is
the single 32-byte HMAC key every test in this package signs *and* verifies
under: SPEC S10.6 approval tokens are symmetric, so the identical bytes mint
via `TokenIssuer`/`SigningKeyHandle` (the Risk Kernel side, reused unmodified
here per issue #37's "Reuse" list) and will verify via
`windbreak.order_gateway.tokens.verify_and_consume` (the Gateway side).

Implementation note (mirrors `tests/riskkernel/conftest.py`'s own docstring):
`make_claims_for_intent` builds one fully-populated `ApprovalTokenClaims`
first, then applies any overrides via `dataclasses.replace` -- never by
splatting a loosely-typed `dict[str, object]` into the constructor, which
`mypy --strict` would reject against the dataclass's concretely-typed fields.
`dataclasses.replace`'s stub accepts `**changes: Any`, so it is the one
sanctioned spot for that looseness.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from windbreak.numeric.types import (
    ContractCentis,
    MoneyMicros,
    PricePips,
    ProbabilityPpm,
)
from windbreak.riskkernel.checks import OrderIntent
from windbreak.riskkernel.signing import SigningKeyHandle
from windbreak.riskkernel.tokens import TokenIssuer
from windbreak.tokens.verify import ApprovalTokenClaims, SignedApprovalToken

if TYPE_CHECKING:
    from windbreak.connector.paper import PaperExchange

#: The single 32-byte HMAC key shared by every mint/verify pair in this
#: package's tests. SPEC S10.6 approval tokens are symmetric: the exact same
#: bytes both sign (Risk Kernel side, via `TokenIssuer`) and verify (Gateway
#: side, via `verify_and_consume`).
KEY_MATERIAL = b"k" * 32

#: The market ticker every default `OrderIntent` targets: the sole ticker in
#: the shared `tests/fixtures/books/deep_walk` `PaperExchange` fixture (issue
#: #19), so the Gateway happy-path tests get a real, tradeable market for
#: free, with no fixture duplication.
DEFAULT_MARKET_TICKER = "MKT-DEEP"

#: A fixed "current instant" (epoch seconds) every default claims' `expires_at`
#: and every gateway-under-test injected clock agree on, so a freshly minted
#: token is unexpired by default and the expiry boundary can be tested by
#: moving just one side of the comparison.
DEFAULT_NOW_EPOCH_S = 1_700_000_000

#: One `windbreak.riskkernel.tokens.DEFAULT_TOKEN_TTL_SECONDS` (60s) past
#: `DEFAULT_NOW_EPOCH_S`, so a default token is unexpired unless a test
#: deliberately overrides `expires_at`.
DEFAULT_EXPIRES_AT = DEFAULT_NOW_EPOCH_S + 60

#: Immutable scaled-int defaults for `make_intent`, held as module-level
#: singletons (ruff B008) -- the wrapper types are frozen, so sharing one
#: instance is safe. `price`/`size` are chosen to cross exactly the sole
#: 4600-pip/200-centis ask level in the `deep_walk` fixture's order book, so
#: the Gateway happy-path tests get a deterministic, hand-derivable fill.
_DEFAULT_PRICE = PricePips(4600)
_DEFAULT_SIZE = ContractCentis(200)
_DEFAULT_MAX_NOTIONAL = MoneyMicros(50_000_000)
_DEFAULT_IMPLIED_PROBABILITY = ProbabilityPpm(520_000)
_DEFAULT_IDEMPOTENCY_KEY = "idem-0001"

#: Claims-only field defaults (fields `OrderIntent` has no counterpart for),
#: for `make_claims_for_intent`.
_DEFAULT_MAX_FEE_MICROS = MoneyMicros(450_000)
_DEFAULT_CONFIG_HASH = "cfg-hash-abc123"
_DEFAULT_KERNEL_SEQUENCE_NUMBER = 1


def make_intent(
    *,
    intent_id: str = "intent-0001",
    market_ticker: str = DEFAULT_MARKET_TICKER,
    outcome: str = "yes",
    action: str = "buy",
    price: PricePips = _DEFAULT_PRICE,
    size: ContractCentis = _DEFAULT_SIZE,
    max_notional: MoneyMicros = _DEFAULT_MAX_NOTIONAL,
    implied_probability: ProbabilityPpm = _DEFAULT_IMPLIED_PROBABILITY,
    idempotency_key: str = _DEFAULT_IDEMPOTENCY_KEY,
) -> OrderIntent:
    """Build a valid `OrderIntent`, with any field overridable by keyword.

    Args:
        intent_id: The intent's unique identifier.
        market_ticker: The exchange ticker the intent targets.
        outcome: The market outcome the intent trades (e.g. "yes"/"no").
        action: The trade action (e.g. "buy"/"sell_to_close").
        price: The limit price, in pips.
        size: The contract count, in centis.
        max_notional: The notional cap, in money-micros.
        implied_probability: The forecast-implied probability, in ppm.
        idempotency_key: The caller-supplied idempotency key.

    Returns:
        A fully populated, valid `OrderIntent`.
    """
    return OrderIntent(
        intent_id=intent_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        price=price,
        size=size,
        max_notional=max_notional,
        implied_probability=implied_probability,
        idempotency_key=idempotency_key,
    )


def make_claims_for_intent(
    intent: OrderIntent, **overrides: object
) -> ApprovalTokenClaims:
    """Build `ApprovalTokenClaims` whose 7 compared fields mirror `intent`.

    The 7 fields `intent_matches_claims` compares (`intent_id`,
    `market_ticker`, `outcome`, `action`, `price.value`/`limit_price_pips`,
    `size.value`/`count_centis`, `idempotency_key`) are copied straight from
    `intent`; the 4 claim-only fields with no `OrderIntent` counterpart
    (`max_fee_micros`, `expires_at`, `config_hash`, `kernel_sequence_number`)
    get independent defaults. Any field -- compared or not -- can be
    overridden by keyword, so a test can deliberately mismatch exactly one
    compared field, or vary an uncompared one to prove it's ignored.

    Args:
        intent: The intent whose compared fields the claims should mirror.
        **overrides: Field name to value, for any `ApprovalTokenClaims` field.

    Returns:
        A fully populated `ApprovalTokenClaims`.
    """
    base = ApprovalTokenClaims(
        intent_id=intent.intent_id,
        market_ticker=intent.market_ticker,
        outcome=intent.outcome,
        action=intent.action,
        limit_price_pips=PricePips(intent.price.value),
        count_centis=ContractCentis(intent.size.value),
        max_fee_micros=_DEFAULT_MAX_FEE_MICROS,
        expires_at=DEFAULT_EXPIRES_AT,
        idempotency_key=intent.idempotency_key,
        config_hash=_DEFAULT_CONFIG_HASH,
        kernel_sequence_number=_DEFAULT_KERNEL_SEQUENCE_NUMBER,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def issue_matching_token(
    intent: OrderIntent,
    *,
    key_material: bytes = KEY_MATERIAL,
    **claim_overrides: object,
) -> SignedApprovalToken:
    """Mint a `SignedApprovalToken` whose claims match `intent` exactly.

    Mirrors `tests/riskkernel/test_process_isolation.py` /
    `tests/riskkernel/test_tokens.py`'s minting idiom: a fresh
    `TokenIssuer(SigningKeyHandle(key_material))` signs claims built by
    `make_claims_for_intent`.

    Args:
        intent: The intent the minted token should approve.
        key_material: The signing key material (>=32 bytes). Defaults to
            `KEY_MATERIAL`, the shared key every Gateway-side test verifies
            under.
        **claim_overrides: Forwarded to `make_claims_for_intent`, so a caller
            can mint a token whose claims deliberately mismatch `intent` (to
            prove `intent_matches_claims` catches it) or carry a specific
            `expires_at` (to test the expiry boundary).

    Returns:
        The signed token.
    """
    claims = make_claims_for_intent(intent, **claim_overrides)
    issuer = TokenIssuer(SigningKeyHandle(key_material))
    return issuer.issue(claims)


@pytest.fixture
def paper_exchange() -> PaperExchange:
    """Provide a fresh `PaperExchange` loaded from the shared `deep_walk`
    books fixture (`tests/fixtures/books/deep_walk`, the same fixture
    `tests/connector/conftest.py` uses for issue #19's own tests). Its sole
    ticker is `MKT-DEEP`, matching `DEFAULT_MARKET_TICKER` above, so
    `make_intent()`'s defaults are directly tradeable against it.
    """
    from windbreak.connector.paper import PaperExchange

    books_dir = Path(__file__).resolve().parents[1] / "fixtures" / "books" / "deep_walk"
    return PaperExchange.from_fixture_dir(books_dir)
