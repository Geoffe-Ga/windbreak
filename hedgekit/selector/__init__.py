"""The selector: pure forecast-to-intent decision stage (SPEC S9.1-S9.3).

The selector turns a forecast plus market and account context into a
ledgerable :class:`SelectorDecision`. Per SPEC S9.1 it is *pure,
credentialless, no-I/O, and no-clock*: it never opens a socket, reads a
secret, or calls the wall clock -- freshness is judged by comparing timestamps
carried *inside* :class:`SelectorInputs`, never against ``datetime.now`` -- so
the same inputs always yield the same, byte-identically serializable decision.

Issue #44 lands the real fee-aware edge and entry logic (SPEC S9.2-S9.3):
:func:`select` prices a fixed-size probe fill via
:func:`~hedgekit.selector.edge.compute_executable_edge`, renders every SPEC
S9.3 entry condition into ``reasons`` (never silently empty), and emits exactly
one normalized ``yes``/``buy`` intent when -- and only when -- every condition
passes. Sizing (SPEC S9.5, issue #45) is not built yet, so the probe uses a
fixed :data:`_PROBE_SIZE_CENTIS` placeholder.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from hedgekit.ledger.events import canonical_json
from hedgekit.numeric import ContractCentis, MoneyMicros, ProbabilityPpm
from hedgekit.riskkernel.checks import OrderIntent
from hedgekit.selector.edge import (
    InsufficientDepth,
    NonAnnualizable,
    _fee_micros,
    compute_executable_edge,
)
from hedgekit.selector.entry import evaluate_entry_conditions
from hedgekit.selector.serialization import serialize_decision
from hedgekit.selector.types import (
    FeeModelInput,
    NormalizedOrderIntent,
    PositionReadModelRef,
    RiskConfigInput,
    SelectorDecision,
    SelectorInputs,
    SlippageModelInput,
)

if TYPE_CHECKING:
    from hedgekit.selector.edge import EdgeFigures
    from hedgekit.selector.entry import EntryCheck

#: The probe size every evaluation prices at until real sizing lands (SPEC
#: S9.5, issue #45): a small fixed 1.00-contract order, enough to exercise the
#: fee-aware edge and entry logic without committing to a sizing model that
#: does not exist yet.
_PROBE_SIZE_CENTIS = ContractCentis(100)

#: The single outcome/action the selector emits today: a YES-side opening buy.
#: Sells and NO-side intents belong to later execution/sizing work.
_OUTCOME_YES = "yes"
_ACTION_BUY = "buy"

__all__ = [
    "FeeModelInput",
    "NormalizedOrderIntent",
    "PositionReadModelRef",
    "RiskConfigInput",
    "SelectorDecision",
    "SelectorInputs",
    "SlippageModelInput",
    "select",
    "serialize_decision",
]


def _render(check: EntryCheck) -> str:
    """Render one entry check into a ledger reason string.

    Args:
        check: The evaluated entry condition.

    Returns:
        ``"pass:<name>"`` when the condition passed, else
        ``"fail:<name>: <detail>"``.
    """
    if check.passed:
        return f"pass:{check.name}"
    return f"fail:{check.name}: {check.detail}"


def _idempotency_key(
    forecast_id: str, market_ticker: str, price_pips: int, size_centis: int
) -> str:
    """Derive the intent's deterministic idempotency key (SPEC S9.1).

    Hashes exactly the six identifying fields through the same
    ``sha256(canonical_json(...))`` primitive
    :func:`hedgekit.order_gateway.client_order_id.client_order_id` uses, so the
    key is a byte-stable function of the intent's economic identity alone.

    Args:
        forecast_id: The originating forecast's id.
        market_ticker: The market the intent targets.
        price_pips: The intent's price, in pips.
        size_centis: The intent's size, in contract-centis.

    Returns:
        The 64-character, lowercase-hex SHA-256 idempotency key.
    """
    fields: dict[str, object] = {
        "forecast_id": forecast_id,
        "market_ticker": market_ticker,
        "outcome": _OUTCOME_YES,
        "action": _ACTION_BUY,
        "price": price_pips,
        "size": size_centis,
    }
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()


def _build_intent(inputs: SelectorInputs, figures: EdgeFigures) -> OrderIntent:
    """Build the single normalized intent for an all-pass evaluation (S9.1).

    Prices at the marginal (deepest-walked) level, sizes at the fixed probe, and
    caps the notional at the fill's executable cost plus its worst-case fee.

    Args:
        inputs: The selector inputs the intent is derived from.
        figures: The executable-edge figures the fill was priced at.

    Returns:
        The normalized :class:`~hedgekit.riskkernel.checks.OrderIntent` to emit.
    """
    forecast = inputs.forecast
    price = figures.marginal_price_pips
    fee_micros = _fee_micros(
        inputs.fee_model, figures.executable_price_pips.value, _PROBE_SIZE_CENTIS.value
    )
    return OrderIntent(
        intent_id=f"{forecast.forecast_id}:{_OUTCOME_YES}:{_ACTION_BUY}:probe",
        market_ticker=forecast.market_ticker,
        outcome=_OUTCOME_YES,
        action=_ACTION_BUY,
        price=price,
        size=_PROBE_SIZE_CENTIS,
        max_notional=MoneyMicros(figures.executable_cost_micros.value + fee_micros),
        implied_probability=ProbabilityPpm(forecast.probability_ppm),
        idempotency_key=_idempotency_key(
            forecast.forecast_id,
            forecast.market_ticker,
            price.value,
            _PROBE_SIZE_CENTIS.value,
        ),
    )


def _decision(
    inputs: SelectorInputs,
    intents: tuple[NormalizedOrderIntent, ...],
    reasons: tuple[str, ...],
) -> SelectorDecision:
    """Assemble a :class:`SelectorDecision`, echoing the inputs' identity.

    Args:
        inputs: The evaluated inputs, supplying the forecast id/ticker and the
            calibration-map version echoed for ledger traceability.
        intents: The emitted normalized intents (possibly empty).
        reasons: The non-empty reasons explaining the verdict.

    Returns:
        The assembled decision.
    """
    return SelectorDecision(
        intents=intents,
        reasons=reasons,
        forecast_id=inputs.forecast.forecast_id,
        market_ticker=inputs.forecast.market_ticker,
        calibration_map_version=inputs.calibration_map_version,
    )


def select(inputs: SelectorInputs) -> SelectorDecision:
    """Evaluate the selector inputs into a decision (SPEC S9.1-S9.3).

    Prices a fixed-size probe fill (SPEC S9.2). If the book is too shallow to
    fill it, declines with the depth-shortfall reason and no intents; if the
    fill priced but its return cannot be annualized (a 0-pip price or a
    zero-hour forecast horizon), declines with the non-annualizable reason and
    no intents. Otherwise renders every SPEC S9.3 entry condition into
    ``reasons`` and, only when all twelve pass, emits exactly one normalized
    ``yes``/``buy`` intent. Reads no clock and does no I/O, so the decision is a
    pure function of ``inputs``.

    Args:
        inputs: The complete, immutable input bundle to evaluate.

    Returns:
        A :class:`SelectorDecision` carrying the emitted intents (one when every
        entry condition passes, none otherwise) and the non-empty reasons for
        the verdict, alongside the forecast id, market ticker, and
        calibration-map version carried over from ``inputs``.
    """
    figures = compute_executable_edge(
        order_book=inputs.order_book,
        size=_PROBE_SIZE_CENTIS,
        forecast=inputs.forecast,
        fee_model=inputs.fee_model,
        slippage_model=inputs.slippage_model,
    )
    if isinstance(figures, (InsufficientDepth, NonAnnualizable)):
        return _decision(inputs, (), (figures.reason,))

    checks = evaluate_entry_conditions(inputs, figures)
    reasons = tuple(_render(check) for check in checks)
    if all(check.passed for check in checks):
        return _decision(inputs, (_build_intent(inputs, figures),), reasons)
    return _decision(inputs, (), reasons)
