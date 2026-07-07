"""Pure reduce-only admission math for closing orders (issue #39, SPEC S6.4/S11.2).

The Order Gateway must never let a ``SELL_TO_CLOSE`` grow a position past flat:
a close is admitted only up to what is actually held, net of whatever closes are
already in flight for the same ticker. This module is the *pure* core of that
rule -- no I/O, no mutation, no floats. Every quantity is an ``int`` count of
contract-centis (SPEC S6.1, guarded by ``scripts/lint_no_floats.py``); the
Gateway (:mod:`hedgekit.order_gateway.gateway`) supplies the live positions and
in-flight tallies and acts on these verdicts.

    * :func:`held_for_ticker` collapses a (possibly duplicate-rowed) position
      list into a single held count for one ticker.
    * :func:`closeable_centis` is the admissible headroom: held minus in-flight,
      floored at zero.
    * :func:`is_close_admissible` decides a brand-new close against that
      headroom.
    * :func:`is_net_short_after_fill` is the *post*-fill invariant: it flags a
      venue fill that overshot the held position into a net-short (the
      fail-closed halt trigger, SPEC S11.5).
    * :class:`PositionSnapshot` is the immutable justification ledgered on a
      refusal, pinning the exact numbers the verdict was computed from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hedgekit.connector.models import Position


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """The immutable position picture a reduce-only verdict was computed from.

    Ledgered verbatim on a refusal so an auditor can re-derive the decision:
    the close was admissible iff ``requested_close_centis`` did not exceed
    ``held_centis - inflight_closing_centis`` (floored at zero).

    Attributes:
        ticker: The market ticker the close targets.
        held_centis: The net held position for ``ticker``, in contract-centis.
        inflight_closing_centis: The sum of closes already in flight for
            ``ticker`` (not yet reflected in ``held_centis``), in
            contract-centis.
        requested_close_centis: The size of the close being decided, in
            contract-centis.
    """

    ticker: str
    held_centis: int
    inflight_closing_centis: int
    requested_close_centis: int


def held_for_ticker(positions: Sequence[Position], ticker: str) -> int:
    """Sum the net held quantity for ``ticker`` across ``positions``.

    Sums every matching row rather than assuming a single position per ticker,
    so a venue that reports a ticker across duplicate rows is still totalled
    correctly.

    Args:
        positions: The positions to search, from the Gateway's position source.
        ticker: The market ticker to total the held quantity for.

    Returns:
        The net held quantity for ``ticker``, in contract-centis; ``0`` when no
        row matches.
    """
    return sum(p.quantity.value for p in positions if p.ticker == ticker)


def closeable_centis(held: int, inflight: int) -> int:
    """Return the admissible closeable headroom: held minus in-flight, floored.

    Args:
        held: The net held position for the ticker, in contract-centis.
        inflight: The sum of closes already in flight for the ticker, in
            contract-centis.

    Returns:
        ``max(held - inflight, 0)``: the additional quantity that may still be
        closed without overshooting flat.
    """
    return max(held - inflight, 0)


def is_close_admissible(requested: int, held: int, inflight: int) -> bool:
    """Decide whether a brand-new close of ``requested`` may be admitted.

    Args:
        requested: The size of the close being decided, in contract-centis.
        held: The net held position for the ticker, in contract-centis.
        inflight: The sum of closes already in flight for the ticker, in
            contract-centis.

    Returns:
        ``True`` iff ``requested`` fits within
        :func:`closeable_centis` ``(held, inflight)``.
    """
    return requested <= closeable_centis(held, inflight)


def is_net_short_after_fill(held: int, filled: int) -> bool:
    """Flag a fill that overshot the held position into a net-short.

    The post-fill invariant (SPEC S11.5): a close may never leave the position
    net-short. If the venue filled more than was held, ``held - filled`` is
    negative and the Gateway must halt, fail-closed.

    Timing contract (load-bearing): ``held`` must be the *pre-fill* position --
    the quantity held before the fill this call is checking landed -- so that a
    genuine overshoot surfaces as ``held - filled < 0``. A position source that
    already reflects the just-placed fill in ``held`` would false-positive on
    every normal full close; the Gateway's default source is ``None`` (enforcement
    off) and any real source wired later (issue #40) must lag the just-placed
    fill for this check to be correct.

    Args:
        held: The net *pre-fill* held position for the ticker, in
            contract-centis (see the timing contract above).
        filled: The quantity the venue reported filled, in contract-centis.

    Returns:
        ``True`` iff ``held - filled`` is negative (a net-short breach).
    """
    return held - filled < 0
