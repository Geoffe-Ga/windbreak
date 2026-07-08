"""Failing-first tests for the weekly report stub (issue #48, RED).

`windbreak.reports` does not exist yet, so every test module here fails
collection with `ModuleNotFoundError: No module named 'windbreak.reports'`,
the expected Gate 1 RED state for issue #48.
"""

from __future__ import annotations
