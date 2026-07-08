"""Weekly PAPER-loop report stubs, idempotent per ISO calendar week (issue #48).

:func:`write_weekly_stub` writes a dated ``weekly-YYYY-MM-DD.md`` file with
markdown section headers and ``No data yet.`` bodies, creating the output
directory when absent. :func:`maybe_write_weekly` layers ISO-week idempotence on
top: at most one file is written per ISO calendar week, so the always-on loop
can call it every beat yet only produce one report per week. The real report
bodies are a later documentation pass; the stub exists so an operator always has
a current file on disk.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

#: The date format stamped into the ``weekly-YYYY-MM-DD.md`` filename.
_DATE_FORMAT = "%Y-%m-%d"

#: The glob matching every weekly report file, for the ISO-week idempotence scan.
_WEEKLY_GLOB = "weekly-*.md"

#: The report body: section headers each carrying a ``No data yet.`` placeholder
#: (the real bodies are a later documentation pass, issue #48).
_REPORT_BODY = (
    "# Weekly report {date}\n\n"
    "## Equity vs floor\n\n"
    "No data yet.\n\n"
    "## Positions\n\n"
    "No data yet.\n\n"
    "## Decisions\n\n"
    "No data yet.\n"
)


def _iso_week_key(today: date) -> tuple[int, int]:
    """Return the ``(iso_year, iso_week)`` key identifying ``today``'s ISO week.

    Args:
        today: The date whose ISO calendar week is keyed.

    Returns:
        The ISO year and ISO week number, so two dates in the same Mon-Sun ISO
        week compare equal while a date in a later week does not.
    """
    iso = today.isocalendar()
    return iso.year, iso.week


def write_weekly_stub(
    output_dir: Path, *, today: date, body: str | None = None
) -> Path:
    """Write ``weekly-YYYY-MM-DD.md`` for ``today``, creating ``output_dir``.

    Unconditionally overwrites any existing file for the identical ``today`` (a
    plain write, never an error). When ``body`` is ``None`` the written body is
    the default stub -- markdown section headers each with a ``No data yet.``
    placeholder; otherwise ``body`` is written verbatim, letting a caller supply
    a fully-rendered report (issue #55).

    Args:
        output_dir: The directory the report is written into; created (with
            parents) when absent.
        today: The report date, stamped into both the filename and the default
            body.
        body: The exact report body to write, or ``None`` to write the stub.

    Returns:
        The path of the written report file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = today.strftime(_DATE_FORMAT)
    path = output_dir.joinpath(f"weekly-{stamp}.md")
    content = _REPORT_BODY.format(date=stamp) if body is None else body
    path.write_text(content, encoding="utf-8")
    return path


def maybe_write_weekly(
    output_dir: Path, *, today: date, body: str | None = None
) -> Path:
    """Write this ISO week's report at most once, returning the report path.

    Idempotent per ISO calendar week: if any ``weekly-*.md`` file already exists
    for a date in ``today``'s ISO week, that file is returned untouched;
    otherwise a fresh report is written. The always-on loop can therefore call
    this every beat yet produce exactly one report per week.

    Args:
        output_dir: The directory the report is written into (created when
            absent).
        today: The report date, whose ISO week gates whether a new file is
            written.
        body: The exact report body to write when a new file is created, or
            ``None`` to write the default stub (issue #55).

    Returns:
        The path of the freshly written, or already-existing, report file.
    """
    target_key = _iso_week_key(today)
    if output_dir.is_dir():
        for existing in output_dir.glob(_WEEKLY_GLOB):
            existing_date = datetime.strptime(
                existing.stem.removeprefix("weekly-"), _DATE_FORMAT
            ).date()
            if _iso_week_key(existing_date) == target_key:
                return existing
    return write_weekly_stub(output_dir, today=today, body=body)
