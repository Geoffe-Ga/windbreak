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


def write_weekly_stub(output_dir: Path, *, today: date) -> Path:
    """Write ``weekly-YYYY-MM-DD.md`` for ``today``, creating ``output_dir``.

    Unconditionally overwrites any existing file for the identical ``today`` (a
    plain write, never an error). The written body carries markdown section
    headers, each with a ``No data yet.`` placeholder.

    Args:
        output_dir: The directory the report is written into; created (with
            parents) when absent.
        today: The report date, stamped into both the filename and the body.

    Returns:
        The path of the written report file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = today.strftime(_DATE_FORMAT)
    path = output_dir.joinpath(f"weekly-{stamp}.md")
    path.write_text(_REPORT_BODY.format(date=stamp), encoding="utf-8")
    return path


def maybe_write_weekly(output_dir: Path, *, today: date) -> Path:
    """Write this ISO week's report at most once, returning the report path.

    Idempotent per ISO calendar week: if any ``weekly-*.md`` file already exists
    for a date in ``today``'s ISO week, that file is returned untouched;
    otherwise a fresh stub is written. The always-on loop can therefore call
    this every beat yet produce exactly one report per week.

    Args:
        output_dir: The directory the report is written into (created when
            absent).
        today: The report date, whose ISO week gates whether a new file is
            written.

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
    return write_weekly_stub(output_dir, today=today)
