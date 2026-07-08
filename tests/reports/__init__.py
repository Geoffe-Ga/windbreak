"""Failing-first tests for the weekly report stub (issue #48, RED).

`hedgekit.reports` does not exist yet, so every test module here fails
collection with `ModuleNotFoundError: No module named 'hedgekit.reports'`,
the expected Gate 1 RED state for issue #48.
"""

from __future__ import annotations
