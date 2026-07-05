"""Tests for hedgekit.main module constants and the console entry point.

The CLI parsing/loop behavior itself lives in test_cli.py and
test_run_loop.py; this module only pins the MODE_RESEARCH constant and
that `python -m hedgekit` resolves to a callable `main`.
"""

from __future__ import annotations

import importlib

from hedgekit.main import MODE_RESEARCH


def test_mode_research_constant_matches_spec_mode_name() -> None:
    """MODE_RESEARCH matches the RESEARCH state in the SPEC mode machine."""
    assert MODE_RESEARCH == "RESEARCH"


def test_dunder_main_module_exposes_callable_main() -> None:
    """`python -m hedgekit` resolves to hedgekit.__main__ with a callable main."""
    dunder_main = importlib.import_module("hedgekit.__main__")

    assert callable(dunder_main.main)
