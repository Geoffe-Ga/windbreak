"""Core types for the selector (SPEC S9.1-S9.3, issues #43/#44).

The selector is the pure decision stage that turns a forecast plus market and
account context into a ledgerable :class:`SelectorDecision`. SPEC S9.1 fixes
its shape as *pure, credentialless, no-I/O, no-clock*: it never opens a socket,
reads a secret, or calls the wall clock -- freshness is judged by comparing
timestamps carried *inside* the inputs, never against ``datetime.now``. This
module holds the input/output value types, the three concrete seam carriers the
fee-aware edge work (issue #44) needs, and the still-opaque position handle.

Issue #44 enriches three of the four issue-#43 placeholder ``*Ref`` seams into
concrete *input* carriers -- :class:`FeeModelInput` (a real
:class:`~hedgekit.connector.fees.FeeModel` plus its ``as_of`` freshness stamp),
:class:`SlippageModelInput` (a per-contract ppm buffer), and
:class:`RiskConfigInput` (a real :class:`~hedgekit.config.schema.RiskConfig`
plus its content hash) -- because SPEC S9.2's executable-edge and S9.3's
entry-condition arithmetic must read those values, not merely name them.
:class:`PositionReadModelRef` stays an opaque handle: concentration/sizing
(issues #45-#47) does not run yet.

Every type here is a frozen, slotted dataclass so a decision's inputs and
outputs are immutable by construction and cheap to hold, and no numeric field
is ever a float (SPEC S6.1) -- the arithmetic-bearing values live inside the
already unit-typed :class:`~hedgekit.forecast.records.ForecastRecord`,
:class:`~hedgekit.connector.models.OrderBookSnapshot`, and
:class:`~hedgekit.connector.fees.FeeModel`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from hedgekit.riskkernel.checks import OrderIntent

if TYPE_CHECKING:
    from datetime import datetime

    from hedgekit.config.schema import RiskConfig
    from hedgekit.connector.fees import FeeModel
    from hedgekit.connector.models import OrderBookSnapshot
    from hedgekit.forecast.records import ForecastRecord


@dataclass(frozen=True, slots=True)
class FeeModelInput:
    """The fee schedule and freshness stamp an evaluation prices with (S9.2).

    Carries the real, post-init-validated
    :class:`~hedgekit.connector.fees.FeeModel` the executable-edge arithmetic
    charges its worst-case trading and settlement fee bounds against, together
    with the ``as_of`` instant the ``fee_model_current`` entry condition (SPEC
    S9.3) measures staleness against -- read from the input, not from a wall
    clock the pure selector may not touch.

    Attributes:
        model: The fee schedule to price the evaluation with.
        as_of: When the fee schedule was captured, for the freshness check.
    """

    model: FeeModel
    as_of: datetime


@dataclass(frozen=True, slots=True)
class SlippageModelInput:
    """The slippage buffer an evaluation subtracts per contract (SPEC S9.2).

    Attributes:
        model_id: Identifier of the slippage model the buffer came from, for
            ledger traceability.
        per_contract_buffer_ppm: The conservative per-contract slippage haircut,
            in ppm, subtracted from the fee-adjusted edge (SPEC S9.2).
    """

    model_id: str
    per_contract_buffer_ppm: int


@dataclass(frozen=True, slots=True)
class RiskConfigInput:
    """The risk configuration an evaluation honors, plus its content hash.

    Carries the real :class:`~hedgekit.config.schema.RiskConfig` whose
    thresholds the SPEC S9.3 entry conditions read (net-edge floor, annualized
    hurdle, idle-cash APR, quote ttl, open-price band) together with the hash
    pinning exactly which configuration was applied, for ledger traceability.

    Attributes:
        config: The risk configuration whose thresholds gate entry.
        config_hash: Content hash pinning the exact configuration used.
    """

    config: RiskConfig
    config_hash: str


@dataclass(frozen=True, slots=True)
class PositionReadModelRef:
    """Placeholder reference to the position read model an evaluation reads.

    SPEC S9.1 lists the current positions among the selector's inputs; the
    concrete position snapshot and its concentration arithmetic are realized by
    the later sizing/concentration work (issues #45-#47). Until then this stays
    an opaque, immutable handle -- unlike the fee/slippage/risk seams, which
    issue #44's edge and entry logic already reads concretely.

    Attributes:
        snapshot_id: Opaque identifier of the position read-model snapshot.
    """

    snapshot_id: str


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
        fee_model: The fee schedule (and its ``as_of`` stamp) to price with.
        slippage_model: The per-contract slippage buffer to apply.
        positions: Reference to the current-positions read-model snapshot.
        risk_config: The risk configuration (and its hash) to honor.
        correlation_tags: Correlation/event tags grouping related markets, as
            an immutable tuple.
    """

    forecast: ForecastRecord
    calibration_map_version: str
    order_book: OrderBookSnapshot
    fee_model: FeeModelInput
    slippage_model: SlippageModelInput
    positions: PositionReadModelRef
    risk_config: RiskConfigInput
    correlation_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectorDecision:
    """A ledgerable decision record produced by a single selection (SPEC S9.1).

    This is the selector's whole observable output: the normalized intents it
    emits (possibly none) and the reasons explaining the verdict. ``reasons``
    is *never silently empty* -- an evaluation that emits no intents must still
    say why (a per-condition ``"pass:<name>"`` / ``"fail:<name>: <detail>"``
    reason, or a pre-entry decline reason such as ``"insufficient_book_depth:
    ..."`` or ``"non_annualizable: ..."``), so a downstream reader can always
    distinguish "declined, here is why" from "nothing ran". The record
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
