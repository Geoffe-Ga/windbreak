"""Failing-first tests for `hedgekit.order_gateway.gateway` (issue #37, RED).

`hedgekit/order_gateway/gateway.py` does not exist yet, so importing it fails
collection with `ModuleNotFoundError: No module named
'hedgekit.order_gateway.gateway'` -- the expected Gate 1 RED state for issue
#37.

This module pins:

    * `SubmissionAck` / `GatewayResult`: frozen, slotted value types.
    * `PaperSubmitter`: adapts `OrderIntent` -> `PaperOrderIntent` ->
      (`PaperExchange.place_order`) -> `PaperPlacement` -> `SubmissionAck`,
      mapping `intent.outcome` to the `Literal["yes", "no"]`
      `PaperOrderIntent.side` (an unrecognized outcome raises `ValueError`).
    * `OrderGateway.process_intent`: a non-`OK` `verify_and_consume` result
      short-circuits to `GatewayResult(result, OrderState.INTENT_CREATED,
      None)` *without ever calling the submitter*; an `OK` result walks the
      full `APPROVE -> REQUEST_SUBMISSION -> (submit) -> SUBMIT -> ACK` state
      chain and returns `GatewayResult(OK, ACKED, ack)`.
    * The verification key is never exposed via any public attribute (mirrors
      `SigningKeyHandle`'s no-leak guarantee, issue #31).
    * `build_parser`/`main`: a bounded `--max-beats`/`--heartbeat-interval`
      CLI mirroring `hedgekit.riskkernel.process`'s conventions, plus
      `hedgekit.order_gateway.__main__` delegating to it.

The Gateway happy-path test drives a *real* `PaperExchange` (issue #19's
`tests/fixtures/books/deep_walk` fixture) rather than a stub, so its
`SubmissionAck` numbers are hand-derived, not "no exception raised": a
200-centis buy at 4600 pips crosses the sole 4600-pip/200-centis ask level,
capped at `PaperExchange`'s default 25% participation rate --
`floor(200 * 250_000 / 1_000_000) == 50` centis fill, the remaining 150
resting as the exchange's first-ever order (`paper-order-1`).
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from hedgekit.numeric.types import ContractCentis, PricePips
from hedgekit.order_gateway.gateway import (
    GatewayResult,
    OrderGateway,
    OrderSubmitter,
    PaperSubmitter,
    SubmissionAck,
    build_parser,
    main,
)
from hedgekit.order_gateway.state_machine import OrderState
from hedgekit.order_gateway.tokens import VerifyResult
from hedgekit.riskkernel.signing import SigningKeyHandle
from hedgekit.riskkernel.tokens import TokenIssuer
from hedgekit.tokens.verify import InMemorySingleUseRegistry
from tests.order_gateway.conftest import (
    DEFAULT_NOW_EPOCH_S,
    KEY_MATERIAL,
    issue_matching_token,
    make_claims_for_intent,
    make_intent,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from hedgekit.connector.paper import PaperExchange
    from hedgekit.riskkernel.checks import OrderIntent
    from hedgekit.tokens.verify import SignedApprovalToken

#: The environment variable the Order Gateway CLI is expected to read its
#: verification key from -- the *same* variable name
#: `hedgekit.riskkernel.signing.SigningKeyHandle.from_env` already uses on
#: the signing side, since SPEC S10.6 approval tokens are symmetric (the same
#: 32 bytes sign and verify). Judgment call (issue #37's plan pins `main`'s
#: signature but not its internal key-loading source): flagged in the
#: handoff.
_KEY_ENV_VAR = "HEDGEKIT_APPROVAL_TOKEN_KEY"


class _SpySubmitter:
    """A test-double `OrderSubmitter` recording every `submit()` call."""

    def __init__(self) -> None:
        """Initialize with an empty call log."""
        self.calls: list[tuple[OrderIntent, SignedApprovalToken]] = []

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Record the call and return a fixed, deterministic `SubmissionAck`.

        Args:
            intent: The intent being submitted.
            token: The approval token accompanying it.

        Returns:
            A `SubmissionAck` whose `filled` mirrors `intent.size`.
        """
        self.calls.append((intent, token))
        return SubmissionAck(order_id="spy-order-1", filled=intent.size)


def _accepts_order_submitter(submitter: OrderSubmitter) -> OrderSubmitter:
    """Identity helper pinning `OrderSubmitter` as the type both
    `PaperSubmitter` and `_SpySubmitter` must satisfy.

    Args:
        submitter: Any object structurally satisfying `OrderSubmitter`.

    Returns:
        `submitter`, unchanged.
    """
    return submitter


# --- SubmissionAck / GatewayResult: frozen, slotted value types -----------------


def test_submission_ack_is_frozen() -> None:
    """Mutating either field of a constructed `SubmissionAck` raises."""
    ack = SubmissionAck(order_id="o1", filled=ContractCentis(10))

    with pytest.raises(dataclasses.FrozenInstanceError):
        ack.order_id = "o2"


def test_submission_ack_is_slotted_with_no_instance_dict() -> None:
    """`slots=True` means no per-instance `__dict__`."""
    ack = SubmissionAck(order_id="o1", filled=ContractCentis(10))

    assert not hasattr(ack, "__dict__")


def test_gateway_result_is_frozen() -> None:
    """Mutating any field of a constructed `GatewayResult` raises."""
    result = GatewayResult(
        verify_result=VerifyResult.OK, state=OrderState.ACKED, ack=None
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = OrderState.FILLED


def test_gateway_result_is_slotted_with_no_instance_dict() -> None:
    """`slots=True` means no per-instance `__dict__`."""
    result = GatewayResult(
        verify_result=VerifyResult.OK, state=OrderState.ACKED, ack=None
    )

    assert not hasattr(result, "__dict__")


# --- OrderSubmitter Protocol conformance -----------------------------------------


def test_paper_submitter_and_spy_submitter_satisfy_order_submitter_protocol(
    paper_exchange: PaperExchange,
) -> None:
    """Both a real `PaperSubmitter` and a test-double satisfy `OrderSubmitter`
    structurally, wherever the protocol is expected.
    """
    paper_submitter = _accepts_order_submitter(PaperSubmitter(paper_exchange))
    spy_submitter = _accepts_order_submitter(_SpySubmitter())

    assert callable(paper_submitter.submit)
    assert callable(spy_submitter.submit)


# --- PaperSubmitter: adapts OrderIntent -> PaperOrderIntent -> SubmissionAck ----


def test_paper_submitter_submit_adapts_and_returns_typed_submission_ack(
    paper_exchange: PaperExchange,
) -> None:
    """A crossing intent's `SubmissionAck` carries the hand-derived,
    participation-capped fill and the resulting resting order's id.
    """
    intent = make_intent()  # market_ticker="MKT-DEEP", price=4600, size=200
    token = issue_matching_token(intent)
    submitter = PaperSubmitter(paper_exchange)

    ack = submitter.submit(intent, token)

    assert isinstance(ack, SubmissionAck)
    assert ack.order_id == "paper-order-1"
    assert ack.filled == ContractCentis(50)


@pytest.mark.parametrize("outcome,expected_side", [("yes", "yes"), ("no", "no")])
def test_paper_submitter_maps_outcome_to_paper_order_intent_side(
    paper_exchange: PaperExchange, outcome: str, expected_side: str
) -> None:
    """`intent.outcome` maps to `PaperOrderIntent.side`
    (`Literal["yes", "no"]`) -- proven via a non-crossing limit order (well
    below every recorded ask on both sides), so the resting order left behind
    carries the mapped side untouched by any fill.
    """
    intent = make_intent(
        outcome=outcome, price=PricePips(1000), size=ContractCentis(100)
    )
    token = issue_matching_token(intent)
    submitter = PaperSubmitter(paper_exchange)

    ack = submitter.submit(intent, token)

    assert ack.filled == ContractCentis(0)
    assert ack.order_id is not None
    resting_orders = paper_exchange.get_open_orders()
    assert len(resting_orders) == 1
    assert resting_orders[0].side == expected_side


def test_paper_submitter_unknown_outcome_raises_value_error(
    paper_exchange: PaperExchange,
) -> None:
    """An outcome that is neither `"yes"` nor `"no"` raises `ValueError`."""
    intent = make_intent(outcome="maybe")
    token = issue_matching_token(intent)
    submitter = PaperSubmitter(paper_exchange)

    with pytest.raises(ValueError):
        submitter.submit(intent, token)


# --- OrderGateway.process_intent: non-OK short-circuits, OK walks the chain ----


def _bad_signature_scenario() -> tuple[
    SignedApprovalToken, OrderIntent, InMemorySingleUseRegistry
]:
    """Build a token signed under the wrong key: `BAD_SIGNATURE`."""
    intent = make_intent()
    claims = make_claims_for_intent(intent)
    wrong_issuer = TokenIssuer(SigningKeyHandle(b"z" * 32))
    token = wrong_issuer.issue(claims)
    return token, intent, InMemorySingleUseRegistry()


def _expired_scenario() -> tuple[
    SignedApprovalToken, OrderIntent, InMemorySingleUseRegistry
]:
    """Build a token whose `expires_at` is already at the clock: `EXPIRED`."""
    intent = make_intent()
    claims = make_claims_for_intent(intent, expires_at=DEFAULT_NOW_EPOCH_S)
    token = TokenIssuer(SigningKeyHandle(KEY_MATERIAL)).issue(claims)
    return token, intent, InMemorySingleUseRegistry()


def _intent_mismatch_scenario() -> tuple[
    SignedApprovalToken, OrderIntent, InMemorySingleUseRegistry
]:
    """Build a token for one intent, then verify it against another (mismatch)."""
    intent = make_intent()
    token = issue_matching_token(intent)
    mismatched_intent = dataclasses.replace(intent, market_ticker="OTHER-TICKER")
    return token, mismatched_intent, InMemorySingleUseRegistry()


def _rejected_scenario() -> tuple[
    SignedApprovalToken, OrderIntent, InMemorySingleUseRegistry
]:
    """Build a token with malformed signature hex: `REJECTED`."""
    intent = make_intent()
    token = issue_matching_token(intent)
    bad_token = dataclasses.replace(token, signature_hex="not-hex-zz")
    return bad_token, intent, InMemorySingleUseRegistry()


def _replayed_scenario() -> tuple[
    SignedApprovalToken, OrderIntent, InMemorySingleUseRegistry
]:
    """Pre-consume a valid token's registry slot: `REPLAYED`."""
    intent = make_intent()
    token = issue_matching_token(intent)
    registry = InMemorySingleUseRegistry()
    registry.consume(token.signature_hex)
    return token, intent, registry


@pytest.mark.parametrize(
    "build_scenario,expected_result",
    [
        (_bad_signature_scenario, VerifyResult.BAD_SIGNATURE),
        (_expired_scenario, VerifyResult.EXPIRED),
        (_intent_mismatch_scenario, VerifyResult.INTENT_MISMATCH),
        (_rejected_scenario, VerifyResult.REJECTED),
        (_replayed_scenario, VerifyResult.REPLAYED),
    ],
    ids=["bad_signature", "expired", "intent_mismatch", "rejected", "replayed"],
)
def test_process_intent_non_ok_verify_leaves_intent_created_ack_none_uncalled(
    build_scenario: Callable[
        [], tuple[SignedApprovalToken, OrderIntent, InMemorySingleUseRegistry]
    ],
    expected_result: VerifyResult,
) -> None:
    """Every non-`OK` verification outcome leaves `GatewayResult.state ==
    INTENT_CREATED`, `.ack is None`, and never calls the submitter: the
    Gateway must check-then-act, never act-then-check.
    """
    token, intent, registry = build_scenario()
    spy = _SpySubmitter()
    gateway = OrderGateway(
        spy,
        verification_key=KEY_MATERIAL,
        registry=registry,
        clock=lambda: DEFAULT_NOW_EPOCH_S,
    )

    result = gateway.process_intent(intent, token)

    assert result.verify_result is expected_result
    assert result.state is OrderState.INTENT_CREATED
    assert result.ack is None
    assert spy.calls == []


def test_process_intent_called_twice_with_same_token_replays_on_second_call() -> None:
    """The first call to `process_intent` with a fresh token succeeds and
    submits once; an identical second call is `REPLAYED` and never submits
    again.
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    spy = _SpySubmitter()
    gateway = OrderGateway(
        spy,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
    )

    first = gateway.process_intent(intent, token)
    second = gateway.process_intent(intent, token)

    assert first.verify_result is VerifyResult.OK
    assert first.state is OrderState.ACKED
    assert second.verify_result is VerifyResult.REPLAYED
    assert second.state is OrderState.INTENT_CREATED
    assert second.ack is None
    assert len(spy.calls) == 1


def test_process_intent_happy_path_with_real_paper_exchange_returns_ok_acked(
    paper_exchange: PaperExchange,
) -> None:
    """A verified intent walks the full state chain against a *real*
    `PaperExchange`, landing on `ACKED` with a typed, golden-valued
    `SubmissionAck`.
    """
    intent = make_intent()
    token = issue_matching_token(intent)
    submitter = PaperSubmitter(paper_exchange)
    gateway = OrderGateway(
        submitter,
        verification_key=KEY_MATERIAL,
        registry=InMemorySingleUseRegistry(),
        clock=lambda: DEFAULT_NOW_EPOCH_S,
    )

    result = gateway.process_intent(intent, token)

    assert result.verify_result is VerifyResult.OK
    assert result.state is OrderState.ACKED
    assert isinstance(result.ack, SubmissionAck)
    assert result.ack.order_id == "paper-order-1"
    assert result.ack.filled == ContractCentis(50)


def test_order_gateway_default_registry_and_clock_still_verify_ok(
    paper_exchange: PaperExchange,
) -> None:
    """Omitting `registry=`/`clock=` still yields a working gateway: the
    defaults are a fresh single-use registry and a real wall clock, so a
    token whose expiry is far in the future verifies `OK`.
    """
    intent = make_intent()
    far_future_expiry = DEFAULT_NOW_EPOCH_S + 10_000_000_000
    token = issue_matching_token(intent, expires_at=far_future_expiry)
    submitter = PaperSubmitter(paper_exchange)
    gateway = OrderGateway(submitter, verification_key=KEY_MATERIAL)

    result = gateway.process_intent(intent, token)

    assert result.verify_result is VerifyResult.OK
    assert result.state is OrderState.ACKED


# --- The verification key must never leak via a public attribute --------------


def test_order_gateway_exposes_no_key_byte_attribute(
    paper_exchange: PaperExchange,
) -> None:
    """No public, non-callable attribute of an `OrderGateway` instance ever
    holds the raw verification key bytes -- mirrors `SigningKeyHandle`'s
    no-leak guarantee (issue #31) on the verifier side.
    """
    submitter = PaperSubmitter(paper_exchange)
    gateway = OrderGateway(submitter, verification_key=KEY_MATERIAL)

    public_attribute_names = [name for name in dir(gateway) if not name.startswith("_")]

    for name in public_attribute_names:
        value = getattr(gateway, name)
        if callable(value):
            continue
        if isinstance(value, (bytes, bytearray)):
            assert bytes(value) != KEY_MATERIAL, f"{name!r} exposes the raw key bytes"


# --- CLI: build_parser / main, bounded, mirroring riskkernel.process ------------


def test_build_parser_parses_heartbeat_interval_and_max_beats() -> None:
    """`build_parser` exposes exactly the two bounded-loop options."""
    parser = build_parser()

    args = parser.parse_args(["--heartbeat-interval", "3", "--max-beats", "7"])

    assert args.heartbeat_interval == 3
    assert args.max_beats == 7


def test_build_parser_rejects_negative_max_beats() -> None:
    """A negative `--max-beats` is an argparse usage error (exit code 2)."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--max-beats", "-1"])

    assert exc_info.value.code == 2


def test_build_parser_rejects_negative_heartbeat_interval() -> None:
    """A negative `--heartbeat-interval` is likewise an argparse usage error."""
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--heartbeat-interval", "-1"])

    assert exc_info.value.code == 2


def test_main_rejects_negative_max_beats() -> None:
    """`main` itself rejects a negative `--max-beats` before any gateway
    construction or key loading occurs (pure argparse validation).
    """
    with pytest.raises(SystemExit) as exc_info:
        main(["--max-beats", "-1"])

    assert exc_info.value.code == 2


def test_main_rejects_negative_heartbeat_interval() -> None:
    """`main` itself rejects a negative `--heartbeat-interval`."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--heartbeat-interval", "-1"])

    assert exc_info.value.code == 2


def test_main_bounded_run_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded `--max-beats`/`--heartbeat-interval` run exits 0.

    Judgment call: `main`'s internal key-loading source is not pinned by
    issue #37's plan (only `build_parser`'s two options are), so this test
    sets the same environment variable `SigningKeyHandle.from_env` already
    reads on the signing side (`HEDGEKIT_APPROVAL_TOKEN_KEY`) in case `main`
    mirrors it -- harmless if the implementation sources the key another way.
    """
    monkeypatch.setenv(_KEY_ENV_VAR, KEY_MATERIAL.hex())

    exit_code = main(["--max-beats", "2", "--heartbeat-interval", "0"])

    assert exit_code == 0


def test_order_gateway_dunder_main_module_imports_cleanly() -> None:
    """`python -m hedgekit.order_gateway`'s entry module imports without
    error, for in-process coverage of the delegation to `gateway.main`.
    """
    module = importlib.import_module("hedgekit.order_gateway.__main__")

    assert module is not None


@pytest.mark.timeout(30)
def test_order_gateway_module_invocation_smoke_via_subprocess() -> None:
    """`python -m hedgekit.order_gateway --max-beats 2 --heartbeat-interval 0`
    exits 0. Bounded via both `--max-beats` and a hard subprocess `timeout=`
    -- never an unbounded wait.
    """
    env = dict(os.environ)
    env[_KEY_ENV_VAR] = KEY_MATERIAL.hex()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hedgekit.order_gateway",
            "--max-beats",
            "2",
            "--heartbeat-interval",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )

    assert result.returncode == 0
