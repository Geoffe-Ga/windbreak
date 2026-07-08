"""The selector: pure forecast-to-intent decision stage (SPEC S9.1, issue #43).

The selector turns a forecast plus market and account context into a
ledgerable :class:`SelectorDecision`. Per SPEC S9.1 it is *pure,
credentialless, no-I/O, and no-clock*: it never opens a socket, reads a
secret, or calls the wall clock -- freshness is judged by comparing timestamps
carried *inside* :class:`SelectorInputs`, never against ``datetime.now`` -- so
the same inputs always yield the same, byte-identically serializable decision.

This issue (#43) lands only the skeleton: the type surface, the canonical
serializer, and a :func:`select` *stub* that emits zero intents with a
``"stub: ..."`` reason. The real edge/sizing/band/selection logic (SPEC
S9.2-S9.5) belongs to issues #44-#47 and is deliberately absent here.
"""

from __future__ import annotations

from hedgekit.selector.serialization import serialize_decision
from hedgekit.selector.types import (
    FeeModelRef,
    NormalizedOrderIntent,
    PositionReadModelRef,
    RiskConfigRef,
    SelectorDecision,
    SelectorInputs,
    SlippageModelRef,
)

#: The reason the not-yet-implemented :func:`select` stub returns, pinning that
#: it declines rather than fabricates an intent; the ``"stub:"`` prefix is
#: asserted by the determinism harness.
_STUB_REASON = "stub: selection logic not yet implemented"

__all__ = [
    "FeeModelRef",
    "NormalizedOrderIntent",
    "PositionReadModelRef",
    "RiskConfigRef",
    "SelectorDecision",
    "SelectorInputs",
    "SlippageModelRef",
    "select",
    "serialize_decision",
]


def select(inputs: SelectorInputs) -> SelectorDecision:
    """Evaluate the selector inputs into a decision (SPEC S9.1 stub).

    This is the issue-#43 stub: it performs no edge, sizing, or band logic
    (that is SPEC S9.2-S9.5, deferred to issues #44-#47) and reads no clock and
    no I/O. It emits zero intents and a single ``"stub: ..."`` reason, echoing
    the forecast's identity and the calibration-map version straight from the
    inputs so the decision stays a pure function of them.

    Args:
        inputs: The complete, immutable input bundle to evaluate.

    Returns:
        A :class:`SelectorDecision` with no intents, a single ``"stub: ..."``
        reason, and the forecast id, market ticker, and calibration-map version
        carried over from ``inputs``.
    """
    return SelectorDecision(
        intents=(),
        reasons=(_STUB_REASON,),
        forecast_id=inputs.forecast.forecast_id,
        market_ticker=inputs.forecast.market_ticker,
        calibration_map_version=inputs.calibration_map_version,
    )
