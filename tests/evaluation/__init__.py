"""Failing-first tests for the evaluation three-track report skeleton
(issue #49, RED).

`hedgekit.evaluation` does not exist yet, so every test module here fails
collection with `ModuleNotFoundError: No module named 'hedgekit.evaluation'`,
the expected Gate 1 RED state for issue #49.
"""

from __future__ import annotations
