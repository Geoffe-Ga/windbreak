"""Failing-first tests for the evaluation three-track report skeleton
(issue #49, RED).

`windbreak.evaluation` does not exist yet, so every test module here fails
collection with `ModuleNotFoundError: No module named 'windbreak.evaluation'`,
the expected Gate 1 RED state for issue #49.
"""

from __future__ import annotations
