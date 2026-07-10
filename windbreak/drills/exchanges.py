"""The held-positions drill exchange (issue #59).

:class:`HeldPositionsExchange` is the exchange the kill/ratchet drills audit: it
holds both resting orders and open positions, and its :meth:`cancel_order`
removes an order from the open-orders book *only* -- it never touches positions.
"Cannot move funds" is made **structural**: the class exposes no
``withdraw``/``transfer``/``move_funds`` surface at all, so a drill's
position-hold invariant is an absence a reviewer can see, not merely an
untested capability.

It reuses :class:`~windbreak.connector.models.OpenOrder` /
:class:`~windbreak.connector.models.Position` verbatim rather than minting
parallel drill-only types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from windbreak.connector.models import OpenOrder, Position


class HeldPositionsExchange:
    """An exchange holding resting orders and positions, with orders cancellable.

    Cancellation is order-book-only: :meth:`cancel_order` removes at most the
    matching resting order and never mutates the held positions, even when an
    order and a position share a ticker. The class deliberately exposes no
    fund-movement method, so a drill "cannot move funds" structurally.
    """

    def __init__(
        self,
        *,
        open_orders: tuple[OpenOrder, ...],
        positions: tuple[Position, ...],
    ) -> None:
        """Seed the exchange with resting orders and held positions.

        Args:
            open_orders: The resting orders, whose insertion order is preserved
                across reads.
            positions: The held positions, never mutated by cancellation.
        """
        self._open_orders: list[OpenOrder] = list(open_orders)
        self._positions: tuple[Position, ...] = positions

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Return the resting orders in their seeded order.

        Returns:
            The currently resting orders.
        """
        return tuple(self._open_orders)

    def get_positions(self) -> tuple[Position, ...]:
        """Return the held positions.

        Returns:
            The currently held positions.
        """
        return self._positions

    def cancel_order(self, order_id: str) -> None:
        """Remove the matching resting order, if any; never touch positions.

        Canceling an unknown id is a no-op. Positions are never mutated here,
        so a kill/ratchet drill's position-hold invariant is preserved by
        construction.

        Args:
            order_id: The venue id of the resting order to cancel.
        """
        self._open_orders = [
            order for order in self._open_orders if order.id != order_id
        ]
