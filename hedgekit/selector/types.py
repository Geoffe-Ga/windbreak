"""Core types for the selector (SPEC S9.1-S9.3, issues #43/#44/#45).

The selector is the pure decision stage that turns a forecast plus market and
account context into a ledgerable :class:`SelectorDecision`. SPEC S9.1 fixes
its shape as *pure, credentialless, no-I/O, no-clock*: it never opens a socket,
reads a secret, or calls the wall clock -- freshness is judged by comparing
timestamps carried *inside* the inputs, never against ``datetime.now``. This
module holds the input/output value types and the four concrete seam carriers
the fee-aware edge work (issue #44) and the dispersion-scaled Kelly sizing
(issue #45) read.

Issue #44 enriches three of the four issue-#43 placeholder ``*Ref`` seams into
concrete *input* carriers -- :class:`FeeModelInput` (a real
:class:`~hedgekit.connector.fees.FeeModel` plus its ``as_of`` freshness stamp),
:class:`SlippageModelInput` (a per-contract ppm buffer), and
:class:`RiskConfigInput` (a real :class:`~hedgekit.config.schema.RiskConfig`
plus its content hash) -- because SPEC S9.2's executable-edge and S9.3's
entry-condition arithmetic must read those values, not merely name them.
Issue #45 realizes the fourth and last seam: :class:`PositionReadModelInput`,
a concrete carrier of the capital and exposure figures the sizing stage
(SPEC S9.5/S9.6) reads -- the fractional-Kelly stake sizes against
``above_floor_capital_micros`` and its five notional caps clip against the
equity, per-dimension exposures, deploy cap, and daily notional. The carrier
mirrors the *shape* of :class:`~hedgekit.riskkernel.context.AccountState` but
is defined here, importing nothing from the kernel, so the selector stays
kernel-independent (SPEC S9.9 defense-in-depth). Bucket *tagging* remains
issue #47's, and the mode-gated caps stay fenced (see :mod:`hedgekit.selector.
sizing`).

Every type here is a frozen, slotted dataclass so a decision's inputs and
outputs are immutable by construction and cheap to hold, and no numeric field
is ever a float (SPEC S6.1) -- the money-valued position figures are carried in
:class:`~hedgekit.numeric.MoneyMicros`, and the remaining arithmetic-bearing
values live inside the already unit-typed
:class:`~hedgekit.forecast.records.ForecastRecord`,
:class:`~hedgekit.connector.models.OrderBookSnapshot`, and
:class:`~hedgekit.connector.fees.FeeModel`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from hedgekit.riskkernel.checks import OrderIntent

#: The two execution styles the selector may stamp on an opening intent (SPEC
#: S9.7). ``"cross"`` takes liquidity now (the default); ``"rest_inside_spread"``
#: posts a passive resting order priced inside a wide spread, permitted only when
#: :func:`hedgekit.selector.execution_style.decide_execution_style` proves the
#: improved price still clears the edge floor.
ExecutionStyle: TypeAlias = Literal["cross", "rest_inside_spread"]

if TYPE_CHECKING:
    from datetime import datetime

    from hedgekit.config.schema import RiskConfig
    from hedgekit.connector.fees import FeeModel
    from hedgekit.connector.models import OrderBookSnapshot
    from hedgekit.forecast.records import ForecastRecord
    from hedgekit.numeric import MoneyMicros


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
class PositionReadModelInput:
    """The account capital and exposure figures the sizing stage reads (S9.5/S9.6).

    Realizes the issue-#43 opaque ``PositionReadModelRef`` placeholder into the
    concrete carrier the dispersion-scaled fractional-Kelly sizing consumes: the
    stake sizes against ``above_floor_capital_micros`` (SPEC S9.5), and its five
    notional caps clip against the equity, the three per-dimension exposures, the
    deploy cap, and the day's traded notional (SPEC S9.6). Every money field is a
    :class:`~hedgekit.numeric.MoneyMicros` (SPEC S6.1, no floats on the money
    path). The field naming mirrors
    :class:`~hedgekit.riskkernel.context.AccountState`, but this type imports
    nothing from the kernel so the selector stays kernel-independent (SPEC S9.9
    defense-in-depth: the selector sizes conservatively, the kernel re-checks).

    Attributes:
        snapshot_id: Identifier of the position read-model snapshot, for ledger
            traceability.
        equity_micros: Total account equity, in micros; the base the three
            percentage-of-equity concentration ceilings are taken from.
        above_floor_capital_micros: Capital above the equity floor the Kelly
            stake sizes against, in micros (SPEC S9.5).
        total_deploy_cap_micros: The absolute ceiling on total deployed capital,
            in micros; the total-deployed cap's headroom is measured against it.
        market_exposure: Current exposure to the single market, in micros.
        event_exposure: Current exposure to the parent event, in micros.
        bucket_exposure: Current exposure to the correlation bucket, in micros.
        total_exposure: Current total portfolio exposure, in micros; the
            total-deployed cap's used capital.
        notional_today: Notional traded so far today, in micros; the
            daily-notional cap's used amount.
    """

    snapshot_id: str
    equity_micros: MoneyMicros
    above_floor_capital_micros: MoneyMicros
    total_deploy_cap_micros: MoneyMicros
    market_exposure: MoneyMicros
    event_exposure: MoneyMicros
    bucket_exposure: MoneyMicros
    total_exposure: MoneyMicros
    notional_today: MoneyMicros


@dataclass(frozen=True)
class SelectorOrderIntent(OrderIntent):
    """A normalized order intent enriched with SPEC S6.4 execution-style fields.

    A genuine subclass of the Risk Kernel's
    :class:`~hedgekit.riskkernel.checks.OrderIntent` (IS-A, never a parallel
    redefinition), so a selector-built intent is still directly accepted by the
    kernel -- whose 24 pre-trade checks read only the base ``OrderIntent`` fields
    every ``SelectorOrderIntent`` still carries. It *extends by inheritance* with
    the three SPEC S6.4/S9.7 execution-style / adverse-selection fields the kernel
    veto shape omits, so there is zero field drift between the two.

    ``slots`` is deliberately not enabled (matching the base ``OrderIntent``,
    whose own docstring documents why: on CPython, ``frozen`` + ``slots`` routes
    an undeclared-attribute assignment through a stale ``super()`` cell that
    raises the wrong error). The three added fields are trailing and defaulted so
    every base positional/keyword construction still type-checks unchanged.

    Attributes:
        execution_style: How the intent takes liquidity -- ``"cross"`` (the
            default: cross the spread now) or ``"rest_inside_spread"`` (post a
            passive resting order inside a wide spread, SPEC S9.7).
        resting_ttl_seconds: For a resting intent, how long the passive order may
            rest before cancellation, in seconds; ``None`` for a ``cross`` intent.
        cancel_on_move_ticks: For a resting intent, the adverse price move (in
            ticks) that cancels the resting order; ``None`` for a ``cross`` intent.
    """

    execution_style: ExecutionStyle = "cross"
    resting_ttl_seconds: int | None = None
    cancel_on_move_ticks: int | None = None

    def __post_init__(self) -> None:
        """Enforce the execution-style / resting-fields invariant (SPEC S9.7).

        A ``rest_inside_spread`` intent must carry *both* resting fields (a
        passive order needs a ttl and a cancel-on-move guard), and a ``cross``
        intent must carry *neither* (crossing rests nothing). The base
        ``OrderIntent`` defines no ``__post_init__``, so no ``super()`` call is
        needed.

        Raises:
            ValueError: If a ``rest_inside_spread`` intent is missing either
                resting field, or a ``cross`` intent supplies either one.
        """
        both_present = (
            self.resting_ttl_seconds is not None
            and self.cancel_on_move_ticks is not None
        )
        both_absent = (
            self.resting_ttl_seconds is None and self.cancel_on_move_ticks is None
        )
        if self.execution_style == "rest_inside_spread" and not both_present:
            raise ValueError(
                "rest_inside_spread requires both resting_ttl_seconds and "
                "cancel_on_move_ticks to be set"
            )
        if self.execution_style == "cross" and not both_absent:
            raise ValueError(
                "cross requires both resting_ttl_seconds and cancel_on_move_ticks "
                "to be None"
            )


#: The selector's order-intent type: since issue #46 it is
#: :class:`SelectorOrderIntent`, a genuine subclass of the Risk Kernel's
#: :class:`~hedgekit.riskkernel.checks.OrderIntent` that *extends it by
#: inheritance* (never a parallel redefinition, so zero field drift) with the
#: SPEC S6.4/S9.7 execution-style and adverse-selection fields the kernel veto
#: shape omits. A selector-built intent still IS-A kernel ``OrderIntent`` and is
#: accepted unchanged by the kernel's checks, which read only the base fields.
#: This supersedes the issue-#43/#44 ``NormalizedOrderIntent is OrderIntent``
#: identity -- the same documented-supersession precedent this package already
#: applies elsewhere (e.g. the sizing-shaped golden superseding the stub one).
NormalizedOrderIntent: TypeAlias = SelectorOrderIntent


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
        positions: The current-positions capital/exposure figures the sizing
            stage reads (SPEC S9.5/S9.6).
        risk_config: The risk configuration (and its hash) to honor.
        correlation_tags: Correlation/event tags grouping related markets, as
            an immutable tuple.
    """

    forecast: ForecastRecord
    calibration_map_version: str
    order_book: OrderBookSnapshot
    fee_model: FeeModelInput
    slippage_model: SlippageModelInput
    positions: PositionReadModelInput
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
