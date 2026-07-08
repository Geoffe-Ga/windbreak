"""Failing-first tests for the PAPER-loop composition root (issue #48, RED).

`windbreak.scheduler` does not exist yet -- only this test package does -- so
every test module here that imports from it fails collection with
`ModuleNotFoundError: No module named 'windbreak.scheduler'`, the expected
Gate 1 RED state for issue #48.
"""

from __future__ import annotations
