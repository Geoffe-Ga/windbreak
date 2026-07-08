"""Shared HTML-rendering helpers for the PAPER-loop dashboard views (issue #48).

Every ledger-derived string flows through :func:`escape` before it reaches
output -- selector/veto reasons and market tickers are forecast/LLM-adjacent and
therefore an XSS surface -- so the view renderers never interpolate a raw ledger
value into HTML. :func:`section` wraps a title and body rows into a labelled
section, rendering the shared :data:`NO_DATA_PLACEHOLDER` when there is nothing
to show (mirroring :mod:`windbreak.dashboard.app`'s ``never``-placeholder
precedent for a missing heartbeat).
"""

from __future__ import annotations

import html

#: The readable placeholder every view renders when its read model is empty.
NO_DATA_PLACEHOLDER = "No data yet."


def escape(value: object) -> str:
    """Return ``value`` stringified and HTML-escaped for safe interpolation.

    Args:
        value: The (possibly ledger-derived, possibly non-string) value to
            render. Coerced to ``str`` first so an integer count or a hostile
            string are both escaped uniformly.

    Returns:
        The HTML-escaped string form of ``value``.
    """
    return html.escape(str(value))


def section(title: str, body_rows: list[str]) -> str:
    """Wrap a section title and its already-escaped body rows into HTML.

    Args:
        title: The section heading (a fixed, trusted literal from the caller).
        body_rows: The rendered, already-escaped body lines; an empty list
            renders the shared "no data yet" placeholder instead.

    Returns:
        A ``<section>`` HTML fragment.
    """
    inner = f"<p>{NO_DATA_PLACEHOLDER}</p>" if not body_rows else "\n".join(body_rows)
    return f"<section>\n<h2>{escape(title)}</h2>\n{inner}\n</section>\n"
