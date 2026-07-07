"""Process C: the Order Gateway (SPEC S5.1-S5.3).

The Order Gateway holds trade-only exchange credentials and the sole
approval-token **verification** key (SPEC S5.2). It verifies each single-use
token before submitting the corresponding order and hosts the Reconciler. Per
the SPEC S5.3 import boundary, only this package may import the exchange
order-submission client (:mod:`hedgekit.connector.paper`); that rule is enforced
by the pure-``ast`` scanner in ``tests/architecture/test_import_boundaries.py``
and the matching ``order-submission-client-isolation`` contract in
``plans/architecture/.importlinter``.

This package ships three surfaces:

    * :mod:`hedgekit.order_gateway.tokens` -- Gateway-side approval-token
      verification wrapping the shared, key-isolated ``verify_token``.
    * :mod:`hedgekit.order_gateway.state_machine` -- the order-lifecycle state
      machine and its legal transition table.
    * :mod:`hedgekit.order_gateway.gateway` -- the :class:`OrderGateway` itself,
      its submission adapters, and the bounded-heartbeat CLI.
"""

from hedgekit.order_gateway.client_order_id import client_order_id
from hedgekit.order_gateway.gateway import (
    GatewayHaltedError,
    GatewayPositionSource,
    GatewayResult,
    GatewayStatusSource,
    OrderGateway,
    OrderSubmitter,
    PaperSubmitter,
    ReduceOnlyCapableSubmitter,
    SubmissionAck,
    SubmitOutcome,
    build_parser,
    main,
)
from hedgekit.order_gateway.ledger_writer import (
    GatewayLedgerWriter,
    InMemoryGatewayLedgerWriter,
    LoggingGatewayLedgerWriter,
    OrderTransitionLedgered,
    ReduceOnlyRefused,
    ReduceOnlyViolation,
    SubmissionRefused,
    apply_and_ledger,
)
from hedgekit.order_gateway.reduce_only import (
    PositionSnapshot,
    closeable_centis,
    held_for_ticker,
    is_close_admissible,
    is_net_short_after_fill,
)
from hedgekit.order_gateway.state_machine import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    OrderEvent,
    OrderState,
    transition,
)
from hedgekit.order_gateway.tokens import (
    VerifyResult,
    intent_matches_claims,
    verify_and_consume,
)

__all__ = [
    "LEGAL_TRANSITIONS",
    "GatewayHaltedError",
    "GatewayLedgerWriter",
    "GatewayPositionSource",
    "GatewayResult",
    "GatewayStatusSource",
    "IllegalTransitionError",
    "InMemoryGatewayLedgerWriter",
    "LoggingGatewayLedgerWriter",
    "OrderEvent",
    "OrderGateway",
    "OrderState",
    "OrderSubmitter",
    "OrderTransitionLedgered",
    "PaperSubmitter",
    "PositionSnapshot",
    "ReduceOnlyCapableSubmitter",
    "ReduceOnlyRefused",
    "ReduceOnlyViolation",
    "SubmissionAck",
    "SubmissionRefused",
    "SubmitOutcome",
    "VerifyResult",
    "apply_and_ledger",
    "build_parser",
    "client_order_id",
    "closeable_centis",
    "held_for_ticker",
    "intent_matches_claims",
    "is_close_admissible",
    "is_net_short_after_fill",
    "main",
    "transition",
    "verify_and_consume",
]
