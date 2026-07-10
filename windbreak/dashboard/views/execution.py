"""Renderer for the live execution-quality view (issue #58).

Renders every ``ExecutionQualityRecorded`` read-model row into an HTML table of a
fill's identity and its live-vs-paper cost slippage. Every ledger-derived value
is HTML-escaped (:func:`windbreak.dashboard.views._html.escape`) before output --
a fill id is forecast/venue-adjacent and therefore an XSS surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from windbreak.dashboard.views._html import escape, section

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The section heading the execution-quality view renders under.
_TITLE = "Execution quality (live vs paper)"


def _execution_row(row: dict[str, object]) -> str:
    """Render one execution-quality record into an escaped table row.

    Args:
        row: One ``execution_quality`` read-model row
            (``{seq, created_at, event_type, data}``).

    Returns:
        An HTML ``<tr>`` with the fill id, market ticker, and slippage escaped.
    """
    data = cast("Mapping[str, object]", row["data"])
    return (
        "<tr>"
        f"<td>{escape(data.get('fill_id'))}</td>"
        f"<td>{escape(data.get('market_ticker'))}</td>"
        f"<td>{escape(data.get('slippage_micros'))}</td>"
        "</tr>"
    )


def render_execution_quality(rows: list[dict[str, object]]) -> str:
    """Render the execution-quality read model into an HTML section.

    Args:
        rows: The ``execution_quality`` read-model rows, in ledger order; an
            empty list renders the shared "no data yet" placeholder.

    Returns:
        An HTML section fragment listing each fill's slippage, all values
        HTML-escaped.
    """
    body = [_execution_row(row) for row in rows]
    return section(_TITLE, body)
