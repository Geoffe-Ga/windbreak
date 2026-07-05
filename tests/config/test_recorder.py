"""Tests for the config-loading event recorder (issue #11, SPEC S16).

`load_config`/`load_default_config` must call a `ConfigEventRecorder`
on every load with the resulting hash and diff, so a later issue can
back it with a real ledger. This module pins that contract against
the in-memory implementation shipped alongside the protocol.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from hedgekit.config import (
    InMemoryConfigEventRecorder,
    config_hash,
    load_config,
    load_default_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any


def test_load_config_records_hash_and_diff(
    spec16_path: Path,
    spec16_dict: dict[str, Any],
    tmp_path: Path,
    write_config: Callable[[Path, dict[str, Any]], Path],
) -> None:
    """load_config records exactly one event per call, with hash + diff."""
    recorder = InMemoryConfigEventRecorder()

    loaded = load_config(spec16_path, recorder=recorder)

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.config_hash == config_hash(loaded)
    assert event.diff.is_empty
    assert str(spec16_path) in event.source

    modified_mapping = copy.deepcopy(spec16_dict)
    modified_mapping["risk"]["min_net_edge_ppm"] = 40000
    modified_path = write_config(tmp_path, modified_mapping)

    load_config(modified_path, recorder=recorder)

    assert len(recorder.events) == 2
    modified_event = recorder.events[1]
    assert not modified_event.diff.is_empty
    assert "risk.min_net_edge_ppm" in modified_event.diff.changed


def test_load_default_config_records_event() -> None:
    """load_default_config records one event sourced from '<defaults>'."""
    recorder = InMemoryConfigEventRecorder()

    load_default_config(recorder=recorder)

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.diff.is_empty
    assert event.source == "<defaults>"


def test_in_memory_recorder_accumulates_events(spec16_path: Path) -> None:
    """The in-memory recorder accumulates events across loads, in order."""
    recorder = InMemoryConfigEventRecorder()

    load_config(spec16_path, recorder=recorder)
    load_default_config(recorder=recorder)

    assert len(recorder.events) == 2
    assert recorder.events[0].source == str(spec16_path)
    assert recorder.events[1].source == "<defaults>"
