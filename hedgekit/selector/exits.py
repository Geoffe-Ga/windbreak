"""Reduce-only close-intent construction for the selector (SPEC S9.8/S6.4).

SPEC S9.8 fixes the v1 exit policy at *hold-to-resolution*: an opening position
is never closed by strategy logic. A ``sell_to_close`` may be constructed **only**
from one of three non-strategy triggers -- the kill path, a Kernel de-risk
directive, or an explicit operator command (:class:`CloseTrigger`) -- never from
:func:`hedgekit.selector.select`, the open-side entry point. SPEC S6.4 requires
every close to be *reduce-only*: it can shrink, never flip or grow, the held
position.

:func:`build_close_intent` is the sole selector construction site for a close.
It emits a reduce-only :class:`~hedgekit.selector.types.SelectorOrderIntent`
sized at ``min(requested, held)`` -- never more than is held -- always
``execution_style="cross"`` (a de-risk close never rests: resting a close would
re-expose exactly the position we are trying to shed). The gateway sweeper
(issue #41) enforces the post-placement cancels; this module only constructs the
intent. This module is on ``scripts/lint_no_floats.py``'s denylist: no float, no
bare ``/`` -- the notional/probability products below are exact integer scale
bridges (1 pip == 100 ppm), with no division on the path.
"""

import enum
import hashlib

from hedgekit.connector.models import Position
from hedgekit.ledger.events import canonical_json
from hedgekit.numeric import ContractCentis, MoneyMicros, PricePips, ProbabilityPpm
from hedgekit.selector.types import SelectorOrderIntent

# NOTE: this module deliberately does *not* use ``from __future__ import
# annotations``. ``tests/selector/test_exits.py`` introspects
# ``build_close_intent`` via ``typing.get_type_hints`` (asserting ``trigger`` is
# annotated ``CloseTrigger``), which evaluates *every* parameter annotation at
# runtime -- so ``Position`` and ``PricePips`` must be real runtime imports, not
# ``TYPE_CHECKING``-only names. Runtime-evaluated annotations also keep these two
# imports out of ruff's ``TCH`` (flake8-type-checking) reach without a noqa.

#: The single outcome and action every close carries (SPEC S9.8): a YES-side
#: reduce-only sell. The literal ``"sell_to_close"`` lives only in this module --
#: no other selector module on ``select``'s call graph may construct a close.
_OUTCOME_YES = "yes"
_ACTION_SELL_TO_CLOSE = "sell_to_close"

#: Ppm-of-$1 per pip: a pip is 1e-4 $ and a ppm is 1e-6 $, so one pip is 100 ppm.
#: Converts the close price in pips into the implied probability in ppm-of-$1.
_PPM_PER_PIP = 100


class CloseTrigger(enum.Enum):
    """The three non-strategy triggers permitted to construct a close (SPEC S9.8).

    Closed set: a close may originate only from the kill path, a Kernel de-risk
    directive, or an explicit operator command -- never from strategy logic. Each
    member's ``value`` is the machine-readable token stamped into the emitted
    intent's ``intent_id`` suffix and hashed into its idempotency key.

    Attributes:
        KILL_PATH: The emergency kill path is flattening exposure.
        KERNEL_DERISK: The Risk Kernel issued a de-risk directive.
        OPERATOR_COMMAND: A human operator explicitly commanded the close.
    """

    KILL_PATH = "kill_path"
    KERNEL_DERISK = "kernel_derisk"
    OPERATOR_COMMAND = "operator_command"


def _close_idempotency_key(
    trigger: CloseTrigger,
    market_ticker: str,
    price_pips: int,
    size_centis: int,
) -> str:
    """Derive a close intent's deterministic, trigger-scoped idempotency key.

    Hashes exactly the six identifying fields through the same
    ``sha256(canonical_json(...))`` primitive
    :func:`hedgekit.order_gateway.client_order_id.client_order_id` and
    :func:`hedgekit.selector.__init__._idempotency_key` use, so the key is a
    byte-stable function of the close's economic identity. ``trigger`` is hashed
    as its string ``.value`` -- a bare :class:`CloseTrigger` member is not
    JSON-serializable -- so two closes differing only in trigger get distinct
    keys.

    Args:
        trigger: The close trigger, hashed as its ``.value`` string.
        market_ticker: The market the close targets.
        price_pips: The close price, in pips.
        size_centis: The emitted (reduce-only) size, in contract-centis.

    Returns:
        The 64-character, lowercase-hex SHA-256 idempotency key.
    """
    fields: dict[str, object] = {
        "trigger": trigger.value,
        "market_ticker": market_ticker,
        "outcome": _OUTCOME_YES,
        "action": _ACTION_SELL_TO_CLOSE,
        "price": price_pips,
        "size": size_centis,
    }
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()


def build_close_intent(
    trigger: CloseTrigger,
    position: Position,
    close_price: PricePips,
    size: ContractCentis | None = None,
) -> SelectorOrderIntent:
    """Build a reduce-only close intent for a held position (SPEC S9.8/S6.4).

    Reduce-only: the emitted size is ``min(requested, held)`` (``requested``
    defaults to the full held quantity when ``size`` is ``None``), so a close can
    only ever shrink the position, never flip or grow it. Every close crosses
    (``execution_style="cross"``, both resting fields ``None``) -- a de-risk close
    never rests. ``trigger`` is required and first: every close must name why it
    fired.

    Args:
        trigger: Which non-strategy path is closing the position.
        position: The held position to reduce.
        close_price: The limit price to close at, in pips.
        size: The requested close size, in contract-centis; ``None`` closes the
            full held position.

    Returns:
        The reduce-only :class:`~hedgekit.selector.types.SelectorOrderIntent`.

    Raises:
        ValueError: If the held position quantity is non-positive (nothing to
            close), or a requested ``size`` is non-positive.
    """
    if position.quantity.value <= 0:
        raise ValueError(
            f"cannot close a non-positive position: quantity={position.quantity.value}"
        )
    if size is not None and size.value <= 0:
        raise ValueError(f"requested close size must be positive: size={size.value}")
    requested = size if size is not None else position.quantity
    emitted_size = ContractCentis(min(requested.value, position.quantity.value))
    intent_id = (
        f"{position.ticker}:{_OUTCOME_YES}:{_ACTION_SELL_TO_CLOSE}:{trigger.value}"
    )
    return SelectorOrderIntent(
        intent_id=intent_id,
        market_ticker=position.ticker,
        outcome=_OUTCOME_YES,
        action=_ACTION_SELL_TO_CLOSE,
        price=close_price,
        size=emitted_size,
        max_notional=MoneyMicros(close_price.value * emitted_size.value),
        implied_probability=ProbabilityPpm(close_price.value * _PPM_PER_PIP),
        idempotency_key=_close_idempotency_key(
            trigger, position.ticker, close_price.value, emitted_size.value
        ),
        execution_style="cross",
    )
