"""Tests for the `hedgekit.config` schema (issue #11, SPEC S16).

Pins the shape of `HedgekitConfig`: its defaults are the SPEC S16
example verbatim, it is immutable, it holds no float leaves (SPEC
S6.1: integer units only), and the lone float in the SPEC S16 YAML
(`bootstrap_confidence`) loads as an integer parts-per-million field.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from hedgekit.config import HedgekitConfig, load_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _leaves(value: object) -> Iterator[object]:
    """Yield every non-container leaf value reachable from `value`.

    Recurses through dataclass instances (via their declared fields),
    dict values, and list/tuple elements, so it walks the full config
    tree regardless of nesting depth.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            yield from _leaves(getattr(value, f.name))
    elif isinstance(value, dict):
        for v in value.values():
            yield from _leaves(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _leaves(v)
    else:
        yield value


def test_default_config_matches_spec16_example(spec16_path: Path) -> None:
    """HedgekitConfig()'s defaults ARE the SPEC S16 example, verbatim."""
    assert HedgekitConfig() == load_config(spec16_path)


def test_config_is_immutable() -> None:
    """Mutating a nested config field raises FrozenInstanceError."""
    cfg = HedgekitConfig()

    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.risk.min_net_edge_ppm = 1


def test_no_float_fields_anywhere() -> None:
    """No config value is ever a Python float (SPEC S6.1: integers only)."""
    cfg = HedgekitConfig()

    leaves = list(_leaves(cfg))

    assert leaves
    assert not any(isinstance(leaf, float) for leaf in leaves)


def test_bootstrap_confidence_maps_to_ppm_int(spec16_path: Path) -> None:
    """The lone float in SPEC S16 loads as an integer ppm field."""
    cfg = load_config(spec16_path)

    assert cfg.evaluation.bootstrap_confidence_ppm == 950000
    assert isinstance(cfg.evaluation.bootstrap_confidence_ppm, int)
    assert not isinstance(cfg.evaluation.bootstrap_confidence_ppm, bool)
