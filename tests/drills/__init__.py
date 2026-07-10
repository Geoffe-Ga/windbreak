"""Failing-first tests for `windbreak.drills` (issue #59, operational drills).

`windbreak.drills` does not exist yet; every test module in this package
fails collection with `ModuleNotFoundError` until the implementation
specialist builds the package described in `tests/drills/conftest.py` and
each test module's own docstring.
"""
