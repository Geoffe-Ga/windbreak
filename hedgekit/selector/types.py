"""Core types for the selector skeleton (SPEC S9.1, issue #43).

The selector is the pure decision stage that turns a forecast plus market and
account context into a ledgerable :class:`SelectorDecision`. SPEC S9.1 fixes
its shape as *pure, credentialless, no-I/O, no-clock*: it never opens a socket,
reads a secret, or calls the wall clock -- freshness is judged by comparing
timestamps carried *inside* the inputs, never against ``datetime.now``. This
module holds the input/output value types and the four placeholder reference
types that later issues (#44-#47) will realize into concrete models.

Every type here is a frozen, slotted dataclass so a decision's inputs and
outputs are immutable by construction and cheap to hold, and no numeric field
is ever a float (SPEC S6.1) -- the arithmetic-bearing values live inside the
already unit-typed :class:`~hedgekit.forecast.records.ForecastRecord` and
:class:`~hedgekit.connector.models.OrderBookSnapshot`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from hedgekit.riskkernel.checks import OrderIntent

if TYPE_CHECKING:
    from hedgekit.connector.models import OrderBookSnapshot
    from hedgekit.forecast.records import ForecastRecord


@dataclass(frozen=True, slots=True)
class FeeModelRef:
    """Placeholder reference to the fee model an evaluation should price with.

    SPEC S9.1 lists a fee model among the selector's inputs; the concrete fee
    schedule and its edge/sizing arithmetic are realized by the sizing/edge
    work in issues #44-#47. Until then this is an opaque, immutable handle so
    the input contract and its determinism harness can be pinned now without
    coupling to a model shape that does not exist yet.

    Attributes:
        model_id: Opaque identifier of the fee model to apply.
    """

    model_id: str


@dataclass(frozen=True, slots=True)
class SlippageModelRef:
    """Placeholder reference to the slippage model an evaluation should use.

    SPEC S9.1 lists a slippage model among the selector's inputs; the concrete
    slippage curve and its edge/sizing arithmetic are realized by issues
    #44-#47. Until then this is an opaque, immutable handle (see
    :class:`FeeModelRef`).

    Attributes:
        model_id: Opaque identifier of the slippage model to apply.
    """

    model_id: str


@dataclass(frozen=True, slots=True)
class PositionReadModelRef:
    """Placeholder reference to the position read model an evaluation reads.

    SPEC S9.1 lists the current positions among the selector's inputs; the
    concrete position snapshot and its concentration arithmetic are realized by
    issues #44-#47. Until then this is an opaque, immutable handle (see
    :class:`FeeModelRef`).

    Attributes:
        snapshot_id: Opaque identifier of the position read-model snapshot.
    """

    snapshot_id: str


@dataclass(frozen=True, slots=True)
class RiskConfigRef:
    """Placeholder reference to the risk configuration an evaluation honors.

    SPEC S9.1 lists the risk configuration among the selector's inputs; the
    concrete limits and their band/sizing arithmetic are realized by issues
    #44-#47. Until then this is an opaque, immutable handle (see
    :class:`FeeModelRef`).

    Attributes:
        config_hash: Content hash pinning the exact risk configuration used.
    """

    config_hash: str


#: The selector's order-intent type is, by construction, the very
#: :class:`~hedgekit.riskkernel.checks.OrderIntent` the Risk Kernel consumes
#: (SPEC S6.4 + S9.1): the selector emits *already-normalized* intents, so its
#: output surface aligns with what the order-gateway forwards to the kernel
#: with no translation step. It is a genuine ``TypeAlias`` -- the identity of
#: ``OrderIntent`` -- never a parallel redefinition that could drift.
NormalizedOrderIntent: TypeAlias = OrderIntent


@dataclass(frozen=True, slots=True)
class SelectorInputs:
    """The complete, immutable input bundle a single selection evaluates over.

    SPEC S9.1 fixes this input list; holding it as one frozen, slotted value
    keeps every evaluation reproducible and lets the golden-determinism harness
    record a bundle and replay it byte-for-byte. Freshness is judged from the
    timestamps carried inside ``forecast`` and ``order_book`` -- the selector
    never reads a clock of its own.

    Attributes:
        forecast: The forecast record under evaluation, carrying the
            probability estimate, its timestamp, and the market it targets.
        calibration_map_version: Version tag of the calibration map applied to
            the forecast; echoed into the decision for ledger traceability.
        order_book: The market's order-book snapshot, carrying its own fetch
            timestamp used for freshness comparison.
        fee_model: Reference to the fee model to price the evaluation with.
        slippage_model: Reference to the slippage model to apply.
        positions: Reference to the current-positions read-model snapshot.
        risk_config: Reference to the risk configuration to honor.
        correlation_tags: Correlation/event tags grouping related markets, as
            an immutable tuple.
    """

    forecast: ForecastRecord
    calibration_map_version: str
    order_book: OrderBookSnapshot
    fee_model: FeeModelRef
    slippage_model: SlippageModelRef
    positions: PositionReadModelRef
    risk_config: RiskConfigRef
    correlation_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectorDecision:
    """A ledgerable decision record produced by a single selection (SPEC S9.1).

    This is the selector's whole observable output: the normalized intents it
    emits (possibly none) and the reasons explaining the verdict. ``reasons``
    is *never silently empty* -- an evaluation that emits no intents must still
    say why (e.g. the stub's ``"stub: ..."`` note), so a downstream reader can
    always distinguish "declined, here is why" from "nothing ran". The record
    carries no datetime field: SPEC S9.1 forbids the selector from reading a
    clock, so any temporal fact must come from the inputs, keeping the
    serialized decision byte-identical across runs.

    Attributes:
        intents: The normalized order intents emitted, as an immutable tuple;
            empty when the selection declines to trade.
        reasons: Human-readable reasons for the verdict, as an immutable tuple;
            always non-empty.
        forecast_id: Identifier of the forecast this decision was made from.
        market_ticker: Exchange ticker the decision concerns.
        calibration_map_version: Version tag of the calibration map applied,
            echoed from the inputs for ledger traceability.
    """

    intents: tuple[NormalizedOrderIntent, ...]
    reasons: tuple[str, ...]
    forecast_id: str
    market_ticker: str
    calibration_map_version: str
