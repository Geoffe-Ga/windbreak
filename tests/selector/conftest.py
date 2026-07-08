"""Shared fixtures for hedgekit.selector tests (issue #43, SPEC selector skeleton).

`recorded_inputs_bundle_a` / `recorded_inputs_bundle_b` each load one of the
two committed, distinct fixture bundles (`fixtures/bundle_a.json` /
`bundle_b.json`) into a real `hedgekit.selector.SelectorInputs` via
`fixture_loader.load_inputs`, so every test in this package exercises the
selector's actual input contract rather than a hand-rolled double.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.selector.fixture_loader import load_inputs

if TYPE_CHECKING:
    from hedgekit.selector import SelectorInputs

#: This package's own committed bundle fixtures, resolved relative to this
#: file so fixtures work regardless of pytest's invocation directory.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def recorded_inputs_bundle_a() -> SelectorInputs:
    """Provide the recorded bundle-A `SelectorInputs` (issue #43 golden harness)."""
    return load_inputs(_FIXTURES_DIR / "bundle_a.json")


@pytest.fixture
def recorded_inputs_bundle_b() -> SelectorInputs:
    """Provide the recorded bundle-B `SelectorInputs` (issue #43 golden harness)."""
    return load_inputs(_FIXTURES_DIR / "bundle_b.json")
