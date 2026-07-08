"""Shared fixtures for `windbreak.config` tests (issue #11).

Provides the checked-in SPEC S16 example config, both as a filesystem
path and as a parsed mapping, plus a helper for writing arbitrary
config mappings to a temporary YAML file so individual tests can
mutate a deep copy of the example to exercise validation failures.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

#: The checked-in copy of the SPEC S16 example YAML, resolved relative
#: to this conftest's own directory so it works regardless of cwd.
_SPEC16_YAML = Path(__file__).parent / "spec16_example.yaml"


@pytest.fixture
def spec16_path() -> Path:
    """Return the path to the checked-in SPEC S16 example YAML fixture."""
    return _SPEC16_YAML


@pytest.fixture
def spec16_dict(spec16_path: Path) -> dict[str, Any]:
    """Return the SPEC S16 example YAML, parsed into a plain mapping."""
    return yaml.safe_load(spec16_path.read_text(encoding="utf-8"))


@pytest.fixture
def write_config() -> Callable[[Path, dict[str, Any]], Path]:
    """Return a helper that writes a mapping to a temp dir as YAML.

    The returned callable takes ``(tmp_path, mapping)`` explicitly so
    callers can build a fresh mutated mapping (e.g. a deep copy of
    ``spec16_dict`` with an injected bad key) and control which
    ``tmp_path`` it lands under.
    """

    def _write(tmp_path: Path, mapping: dict[str, Any]) -> Path:
        """Dump `mapping` as YAML to a fresh file under `tmp_path`."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(mapping), encoding="utf-8")
        return config_path

    return _write
