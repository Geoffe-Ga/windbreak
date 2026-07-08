"""Failing-first end-to-end tests for the always-on PAPER loop (issue #48, RED).

`windbreak.scheduler.loop` does not exist yet, so every test module here fails
collection with `ModuleNotFoundError: No module named 'windbreak.scheduler'`,
the expected Gate 1 RED state for issue #48.
"""

from __future__ import annotations
