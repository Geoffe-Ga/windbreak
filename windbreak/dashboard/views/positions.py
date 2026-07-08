"""Renderer for the PAPER-loop positions view (issue #48).

Renders the latest ``PositionsSnapshotRecorded`` read-model row into an HTML
table of each held position's ticker and quantity. Every ledger-derived value is
HTML-escaped (:func:`windbreak.dashboard.views._html.escape`) before output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from windbreak.dashboard.views._html import escape, section

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The section heading the positions view renders under.
_TITLE = "Positions"


def _position_row(position: Mapping[str, object]) -> str:
    """Render one position into an escaped table row.

    Args:
        position: One position mapping (``ticker``/``quantity_centis``/
            ``average_price_pips``) from a snapshot's ``data.positions``.

    Returns:
        An HTML ``<tr>`` with the ticker, quantity, and average price escaped.
    """
    return (
        "<tr>"
        f"<td>{escape(position.get('ticker'))}</td>"
        f"<td>{escape(position.get('quantity_centis'))}</td>"
        f"<td>{escape(position.get('average_price_pips'))}</td>"
        "</tr>"
    )


def render_positions(rows: list[dict[str, object]]) -> str:
    """Render the latest positions snapshot into an HTML section.

    Args:
        rows: The ``positions.json`` read-model rows (at most one, the latest
            snapshot); an empty list renders the "no data yet" placeholder.

    Returns:
        An HTML section fragment listing each held position's ticker and
        quantity, all values HTML-escaped.
    """
    if not rows:
        return section(_TITLE, [])
    data = cast("Mapping[str, object]", rows[-1]["data"])
    positions = cast("list[Mapping[str, object]]", data.get("positions", []))
    body = [_position_row(position) for position in positions]
    return section(_TITLE, body)
