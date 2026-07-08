"""Renderer for the PAPER-loop equity-vs-floor view (issue #48).

Renders every ``EquitySampled`` read-model row into an HTML table of the sampled
equity against the configured floor. Every ledger-derived value is HTML-escaped
(:func:`windbreak.dashboard.views._html.escape`) before output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from windbreak.dashboard.views._html import escape, section

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The section heading the equity view renders under.
_TITLE = "Equity vs floor"


def _equity_row(row: dict[str, object]) -> str:
    """Render one equity sample into an escaped table row.

    Args:
        row: One ``equity_curve.json`` read-model row.

    Returns:
        An HTML ``<tr>`` with the sample's equity, floor, and epoch escaped.
    """
    data = cast("Mapping[str, object]", row["data"])
    return (
        "<tr>"
        f"<td>{escape(data.get('epoch_s'))}</td>"
        f"<td>{escape(data.get('equity_micros'))}</td>"
        f"<td>{escape(data.get('floor_micros'))}</td>"
        "</tr>"
    )


def render_equity_vs_floor(rows: list[dict[str, object]]) -> str:
    """Render the equity curve into an HTML section.

    Args:
        rows: The ``equity_curve.json`` read-model rows, in ledger order; an
            empty list renders the "no data yet" placeholder.

    Returns:
        An HTML section fragment listing each sample's equity and floor, all
        values HTML-escaped.
    """
    body = [_equity_row(row) for row in rows]
    return section(_TITLE, body)
