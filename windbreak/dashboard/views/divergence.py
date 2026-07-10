"""Renderer for the live-vs-paper divergence view (issue #58).

Renders every ``LiveDivergenceSampled`` and ``LiveDivergenceBreached`` read-model
row into an HTML table of the two divergence series against their thresholds,
plus the firing trigger. Each series value, each threshold, and the trigger name
is drawn from the row payload and HTML-escaped
(:func:`windbreak.dashboard.views._html.escape`) before output; a sentinel value
(e.g. ``"UNDEFINED"``) renders verbatim. Sampled rows carry no ``trigger`` and
render the :data:`_MISSING` placeholder for that cell; breach rows render the
escaped firing trigger name so an operator can see which threshold fired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from windbreak.dashboard.views._html import escape, section

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The section heading the divergence view renders under.
_TITLE = "Live vs paper divergence"

#: Rendered for a payload field a (minimal) sampled row does not carry, so a
#: missing threshold never renders a bare ``None``.
_MISSING = "n/a"


def _cell(data: Mapping[str, object], key: str) -> str:
    """Render one escaped table cell from a sampled-payload field.

    Args:
        data: The sampled event's ``data`` payload.
        key: The payload key to render.

    Returns:
        The HTML ``<td>`` with the value escaped, or a placeholder when absent.
    """
    return f"<td>{escape(data.get(key, _MISSING))}</td>"


def _divergence_row(row: dict[str, object]) -> str:
    """Render one divergence sample into an escaped table row.

    Args:
        row: One ``live_divergence`` read-model row
            (``{seq, created_at, event_type, data}``).

    Returns:
        An HTML ``<tr>`` pairing each series value with its threshold and the
        firing trigger, escaped. Sampled rows carry no ``trigger`` and render the
        :data:`_MISSING` placeholder there; breach rows render the trigger name.
    """
    data = cast("Mapping[str, object]", row["data"])
    return (
        "<tr>"
        + _cell(data, "live_slippage_ratio_ppm")
        + _cell(data, "live_slippage_ratio_limit_ppm")
        + _cell(data, "live_brier_degradation_ppm")
        + _cell(data, "live_brier_degradation_band_ppm")
        + _cell(data, "trigger")
        + "</tr>"
    )


def render_live_divergence(rows: list[dict[str, object]]) -> str:
    """Render the live-divergence read model into an HTML section.

    Args:
        rows: The ``live_divergence`` read-model rows, in ledger order; an empty
            list renders the shared "no data yet" placeholder.

    Returns:
        An HTML section fragment listing each sampled or breached row's two
        series against their thresholds plus the firing trigger (the
        :data:`_MISSING` placeholder for sampled rows), all values HTML-escaped.
    """
    body = [_divergence_row(row) for row in rows]
    return section(_TITLE, body)
