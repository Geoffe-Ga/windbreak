"""Renderer for the PAPER-loop decisions view (issue #48).

Renders the interleaved selector/intent read-model rows
(``SelectorDecisionRecorded`` plus the bare ``IntentApproved``/``IntentVetoed``
verdicts) into an HTML list of each decision's subject and reasons. Selector and
veto reasons flow from forecast/LLM-adjacent input, so every ledger-derived
string is HTML-escaped (:func:`windbreak.dashboard.views._html.escape`) before
output -- rendering one unescaped would be a stored-XSS vector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from windbreak.dashboard.views._html import escape, section

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The section heading the decisions view renders under.
_TITLE = "Decisions"


def _subject(event_type: str, data: Mapping[str, object]) -> object:
    """Return the decision's subject: its market ticker, else its intent id.

    Args:
        event_type: The row's event type (unused today, kept for readability of
            the branch and future per-type formatting).
        data: The row's ``data`` payload.

    Returns:
        The ``market_ticker`` for a selector decision, or the ``intent_id`` for
        a bare intent verdict.
    """
    del event_type
    return data.get("market_ticker") or data.get("intent_id")


def _reasons(data: Mapping[str, object]) -> list[str]:
    """Render each of a row's reasons into an escaped list item.

    Args:
        data: The row's ``data`` payload carrying a ``reasons`` list.

    Returns:
        One escaped ``<li>`` per reason (an empty list when there are none).
    """
    reasons = cast("list[object]", data.get("reasons", []))
    return [f"<li>{escape(reason)}</li>" for reason in reasons]


def _decision_row(row: dict[str, object]) -> str:
    """Render one decision row into an escaped HTML fragment.

    Args:
        row: One ``selector_decisions.json`` read-model row.

    Returns:
        An HTML fragment naming the row's event type, subject, and reasons, all
        ledger-derived values HTML-escaped.
    """
    event_type = cast("str", row["event_type"])
    data = cast("Mapping[str, object]", row["data"])
    reason_items = _reasons(data)
    reasons_html = f"<ul>{''.join(reason_items)}</ul>" if reason_items else ""
    return (
        "<article>"
        f"<h3>{escape(event_type)}: {escape(_subject(event_type, data))}</h3>"
        f"{reasons_html}"
        "</article>"
    )


def render_decisions(rows: list[dict[str, object]]) -> str:
    """Render the selector/intent decision trail into an HTML section.

    Args:
        rows: The ``selector_decisions.json`` read-model rows, in ledger order;
            an empty list renders the "no data yet" placeholder.

    Returns:
        An HTML section fragment listing each decision's subject and reasons,
        all ledger-derived values HTML-escaped.
    """
    body = [_decision_row(row) for row in rows]
    return section(_TITLE, body)
