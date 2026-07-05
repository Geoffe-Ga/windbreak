"""The Order Gateway (Process C) submission surface and CLI (SPEC S5.1-S5.3).

This module wires the Gateway's runtime surface:

    * :class:`SubmissionAck` / :class:`GatewayResult`: frozen, slotted value
      types carrying a submission receipt and the Gateway's per-intent verdict.
    * :class:`OrderSubmitter`: the structural protocol the Gateway submits
      through, with a real :class:`PaperSubmitter` adapting an
      :class:`~hedgekit.riskkernel.checks.OrderIntent` to the paper exchange's
      :class:`~hedgekit.connector.paper.PaperOrderIntent`.
    * :class:`OrderGateway`: verifies each single-use approval token *before*
      submitting, walking the real :mod:`~hedgekit.order_gateway.state_machine`
      lifecycle only on a verified token, and holding the verification key in a
      private, never-exposed attribute (mirroring the signing side's no-leak
      guarantee, issue #31).
    * :func:`build_parser` / :func:`main`: a bounded ``--max-beats`` /
      ``--heartbeat-interval`` heartbeat CLI mirroring
      :mod:`hedgekit.riskkernel.process`'s conventions.

Per the SPEC S5.3 import boundary, this package is the *sole* legitimate
importer of the exchange order-submission client
(:mod:`hedgekit.connector.paper`); the boundary is enforced by the AST scanner
in ``tests/architecture/test_import_boundaries.py`` and the matching
``plans/architecture/.importlinter`` contract.

Every quantity on the money path is a :mod:`hedgekit.numeric` scaled integer,
never a float (SPEC S6.1); the heartbeat interval is whole seconds.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from hedgekit.connector.paper import PaperOrderIntent
from hedgekit.logging_setup import configure_logging
from hedgekit.numeric import ContractCentis
from hedgekit.order_gateway.state_machine import OrderEvent, OrderState, transition
from hedgekit.order_gateway.tokens import VerifyResult, verify_and_consume
from hedgekit.tokens.verify import InMemorySingleUseRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType
    from typing import Literal

    from hedgekit.connector.paper import PaperExchange
    from hedgekit.riskkernel.checks import OrderIntent
    from hedgekit.tokens.verify import SignedApprovalToken, SingleUseRegistry

#: Component label stamped on every log record this process emits.
_COMPONENT = "order_gateway"

#: The environment variable the CLI reads its hex-encoded verification key from.
#: SPEC S10.6 approval tokens are symmetric, so this is the same variable
#: ``hedgekit.riskkernel.signing.SigningKeyHandle.from_env`` signs under -- the
#: same 32 bytes both sign (Risk Kernel) and verify (here).
_KEY_ENV_VAR = "HEDGEKIT_APPROVAL_TOKEN_KEY"

#: Whole seconds between heartbeats when ``--heartbeat-interval`` is omitted. An
#: integer (not a float): the Gateway stays on the no-floats path (SPEC S6.1).
_DEFAULT_HEARTBEAT_INTERVAL = 5

#: The two admissible market outcomes, each mapping to the paper exchange's
#: ``Literal["yes", "no"]`` side. Any other outcome is unroutable.
_ADMISSIBLE_OUTCOMES: frozenset[str] = frozenset({"yes", "no"})

_LOGGER = logging.getLogger("hedgekit.order_gateway")


def _default_clock() -> int:
    """Return the current wall clock as whole epoch seconds.

    Casts :func:`time.time` to an ``int`` so the Gateway's clock stays off the
    banned float path (SPEC S6.1); token ``expires_at`` values are integral.

    Returns:
        The current time, in whole epoch seconds.
    """
    return int(time.time())


@dataclass(frozen=True, slots=True)
class SubmissionAck:
    """The receipt returned by an :class:`OrderSubmitter` after submitting.

    Attributes:
        order_id: The venue's identifier for the resulting resting order, or
            ``None`` when the submission left no resting order behind.
        filled: The quantity filled immediately on submission, in contract-centis.
    """

    order_id: str | None
    filled: ContractCentis


class OrderSubmitter(Protocol):
    """The structural seam through which the Gateway submits a verified order."""

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Submit ``intent`` (authorized by ``token``) and return its receipt.

        Args:
            intent: The verified order intent to submit.
            token: The approval token that authorized it.

        Returns:
            The :class:`SubmissionAck` receipt of the submission.
        """
        ...


def _outcome_to_side(outcome: str) -> Literal["yes", "no"]:
    """Map an intent outcome onto the paper exchange's order side.

    Args:
        outcome: The intent's market outcome.

    Returns:
        ``"yes"`` or ``"no"``, the :class:`~hedgekit.connector.paper.PaperOrderIntent`
        side.

    Raises:
        ValueError: If ``outcome`` is neither ``"yes"`` nor ``"no"``.
    """
    if outcome == "yes":
        return "yes"
    if outcome == "no":
        return "no"
    raise ValueError(
        f"unroutable outcome {outcome!r}; expected one of "
        f"{sorted(_ADMISSIBLE_OUTCOMES)}"
    )


class PaperSubmitter:
    """An :class:`OrderSubmitter` backed by a :class:`PaperExchange`.

    Adapts an :class:`~hedgekit.riskkernel.checks.OrderIntent` into a
    :class:`~hedgekit.connector.paper.PaperOrderIntent`, places it, and projects
    the resulting :class:`~hedgekit.connector.paper.PaperPlacement` into a
    :class:`SubmissionAck`.
    """

    def __init__(self, exchange: PaperExchange) -> None:
        """Bind the submitter to a paper exchange.

        Args:
            exchange: The paper exchange orders are placed against.
        """
        self._exchange = exchange

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Place ``intent`` on the paper exchange and return its receipt.

        The immediate taker-walk fill total (summed across the emitted per-level
        fills) becomes :attr:`SubmissionAck.filled`; the resting remainder's
        order id (or ``None`` when nothing rested) becomes
        :attr:`SubmissionAck.order_id`.

        Args:
            intent: The verified order intent to submit.
            token: The approval token that authorized it (passed through to the
                exchange, which accepts and ignores it in paper mode).

        Returns:
            The :class:`SubmissionAck` receipt.

        Raises:
            ValueError: If ``intent.outcome`` is neither ``"yes"`` nor ``"no"``.
        """
        paper_intent = PaperOrderIntent(
            ticker=intent.market_ticker,
            side=_outcome_to_side(intent.outcome),
            price=intent.price,
            quantity=intent.size,
        )
        placement = self._exchange.place_order(paper_intent, token)
        filled = ContractCentis(sum(fill.quantity.value for fill in placement.fills))
        order_id = (
            placement.resting_order.id if placement.resting_order is not None else None
        )
        return SubmissionAck(order_id=order_id, filled=filled)


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """The Gateway's verdict for one processed intent.

    Attributes:
        verify_result: The token-verification verdict.
        state: The lifecycle state the order reached (``INTENT_CREATED`` when
            verification failed and no submission occurred; ``ACKED`` on the
            happy path).
        ack: The submission receipt on the happy path, else ``None``.
    """

    verify_result: VerifyResult
    state: OrderState
    ack: SubmissionAck | None


class _UnwiredSubmitter:
    """A placeholder :class:`OrderSubmitter` for the credential-free heartbeat CLI.

    The bounded heartbeat loop (:meth:`OrderGateway.run`) never submits, so the
    CLI wires the Gateway to this stand-in rather than a real trading client --
    mirroring how :mod:`hedgekit.riskkernel.process`'s ``main`` wires a
    ``LoggingKernelLedgerWriter`` stand-in. A future submission client carrying
    trade credentials replaces it; until then any actual call fails closed.
    """

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Fail closed: this stand-in must never actually submit an order.

        Args:
            intent: The intent that would be submitted.
            token: The approval token that would authorize it.

        Returns:
            Never returns normally.

        Raises:
            NotImplementedError: Always -- no trade-credential client is wired.
        """
        del intent, token
        raise NotImplementedError("no order-submission client is wired for the CLI")


class OrderGateway:
    """Verifies approval tokens and submits the orders they authorize.

    The verification key is held only in a private slot: no public, non-callable
    attribute ever exposes the raw bytes, mirroring
    :class:`~hedgekit.riskkernel.signing.SigningKeyHandle`'s no-leak guarantee
    (issue #31). ``__slots__`` keeps the instance ``__dict__``-free as defense in
    depth.
    """

    __slots__ = ("_clock", "_registry", "_submitter", "_verification_key")

    def __init__(
        self,
        submitter: OrderSubmitter,
        *,
        verification_key: bytes,
        registry: SingleUseRegistry | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """Initialize the Gateway.

        Args:
            submitter: The seam verified orders are submitted through.
            verification_key: The shared HMAC key approval tokens verify under.
                Stored privately and never exposed.
            registry: The single-use registry gating replay. Defaults to a fresh
                :class:`~hedgekit.tokens.verify.InMemorySingleUseRegistry`.
            clock: A zero-argument callable returning the current epoch second,
                injected so verification is deterministic under test. Defaults to
                :func:`_default_clock` (real wall clock).
        """
        self._submitter = submitter
        self._verification_key = verification_key
        self._registry: SingleUseRegistry = (
            registry if registry is not None else InMemorySingleUseRegistry()
        )
        self._clock = clock if clock is not None else _default_clock

    def process_intent(
        self, intent: OrderIntent, token: SignedApprovalToken
    ) -> GatewayResult:
        """Verify ``token`` authorizes ``intent``, submitting only if it does.

        Verifies (and consumes the single use of) the token first. A non-``OK``
        verdict short-circuits to a :class:`GatewayResult` still in
        ``INTENT_CREATED`` with no ack -- the submitter is never called
        (check-then-act, never act-then-check). An ``OK`` verdict walks the real
        ``APPROVE -> REQUEST_SUBMISSION -> (submit) -> SUBMIT -> ACK`` chain
        through :func:`~hedgekit.order_gateway.state_machine.transition` and
        returns the ``ACKED`` result carrying the submission receipt.

        Args:
            intent: The order intent to process.
            token: The accompanying single-use approval token.

        Returns:
            The :class:`GatewayResult` verdict.
        """
        verify_result = verify_and_consume(
            token,
            intent,
            key=self._verification_key,
            now_epoch_s=self._clock(),
            registry=self._registry,
        )
        if verify_result is not VerifyResult.OK:
            return GatewayResult(
                verify_result=verify_result,
                state=OrderState.INTENT_CREATED,
                ack=None,
            )
        state = transition(OrderState.INTENT_CREATED, OrderEvent.APPROVE)
        state = transition(state, OrderEvent.REQUEST_SUBMISSION)
        ack = self._submitter.submit(intent, token)
        state = transition(state, OrderEvent.SUBMIT)
        state = transition(state, OrderEvent.ACK)
        return GatewayResult(verify_result=verify_result, state=state, ack=ack)

    def _emit_heartbeat(self, beat: int) -> None:
        """Log one Gateway heartbeat.

        Args:
            beat: The 1-based heartbeat sequence number.
        """
        _LOGGER.info(
            "order gateway heartbeat beat=%d",
            beat,
            extra={"component": _COMPONENT},
        )

    def run(
        self,
        *,
        max_beats: int | None = None,
        heartbeat_interval: int = _DEFAULT_HEARTBEAT_INTERVAL,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Emit heartbeats until the beat budget or stop event ends the loop.

        The loop is always bounded by ``max_beats`` and/or ``stop_event`` -- there
        is never an unbounded sleep -- so it terminates deterministically under
        test and shuts down cleanly on a signal in production.

        Args:
            max_beats: Maximum number of heartbeats to emit before returning.
                ``None`` runs until ``stop_event`` is set.
            heartbeat_interval: Whole seconds to wait between beats. ``0`` waits
                not at all.
            stop_event: Optional event that, once set, ends the loop after the
                current beat. Defaults to a fresh, never-set event.
        """
        if stop_event is None:
            stop_event = threading.Event()
        beat = 0
        while (max_beats is None or beat < max_beats) and not stop_event.is_set():
            beat += 1
            self._emit_heartbeat(beat)
            stop_event.wait(heartbeat_interval)


def _non_negative_int(raw: str) -> int:
    """Parse a non-negative int for use as an argparse ``type``.

    Args:
        raw: The raw command-line token.

    Returns:
        The parsed integer value.

    Raises:
        argparse.ArgumentTypeError: If ``raw`` is negative.
    """
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the ``hedgekit.order_gateway`` bounded-heartbeat CLI parser.

    Both options parse as non-negative integers (a negative value is an argparse
    usage error, exit code 2), matching :mod:`hedgekit.riskkernel.process`'s
    convention. The interval is whole seconds (SPEC S6.1, float-free).

    Returns:
        A parser exposing ``--heartbeat-interval`` and ``--max-beats``.
    """
    parser = argparse.ArgumentParser(
        prog="hedgekit-order-gateway",
        description="Order Gateway (Process C) bounded heartbeat loop.",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=_non_negative_int,
        default=_DEFAULT_HEARTBEAT_INTERVAL,
        help="Whole seconds between heartbeats (default: %(default)s).",
    )
    parser.add_argument(
        "--max-beats",
        type=_non_negative_int,
        default=None,
        help="Stop after this many heartbeats (default: run until signalled).",
    )
    return parser


def _install_signal_handlers(stop_event: threading.Event) -> None:
    """Install SIGINT/SIGTERM handlers that request a graceful shutdown.

    Args:
        stop_event: The event set when a shutdown signal is delivered, so an
            in-flight :meth:`OrderGateway.run` unwinds after its current beat.
    """

    def _handle(_signum: int, _frame: FrameType | None) -> None:
        """Request shutdown by setting the stop event."""
        stop_event.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the bounded Order Gateway heartbeat loop.

    Sources the hex-encoded verification key from the :data:`_KEY_ENV_VAR`
    environment variable (the same symmetric key the Risk Kernel signs under),
    then runs the credential-free bounded heartbeat loop. Negative CLI arguments
    are rejected by argparse (exit code 2) before any key loading occurs.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code (always 0 on a clean run).
    """
    args = build_parser().parse_args(argv)
    configure_logging(level=logging.INFO)
    verification_key = bytes.fromhex(os.environ[_KEY_ENV_VAR])
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    gateway = OrderGateway(_UnwiredSubmitter(), verification_key=verification_key)
    gateway.run(
        max_beats=args.max_beats,
        heartbeat_interval=args.heartbeat_interval,
        stop_event=stop_event,
    )
    return 0
