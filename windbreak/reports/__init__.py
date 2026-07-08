"""Weekly operator-report generation for the always-on PAPER loop (issue #48).

The PAPER loop writes a dated, per-ISO-week markdown report stub so an operator
always has a current, human-readable summary file on disk. The real report
bodies (equity, positions, decisions) are a later documentation pass; today the
sections carry a ``No data yet.`` placeholder.
"""

from __future__ import annotations

from windbreak.reports.weekly import maybe_write_weekly, write_weekly_stub

__all__ = ["maybe_write_weekly", "write_weekly_stub"]
