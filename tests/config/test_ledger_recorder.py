"""Tests for wiring the config loader to the real ledger (issue #74).

`windbreak.config.ledger_recorder` bridges the SPEC §16 config loader
(issue #11) to the hash-chained ledger (issue #13): `diff_payload`
turns a `ConfigDiff` into the JSON-safe shape the `ConfigLoaded` event
persists, and `LedgerConfigEventRecorder` is the `ConfigEventRecorder`
that appends exactly one `ConfigLoaded` event per load. This module
does not exist yet, so every test here is expected to fail on
`ImportError` until the implementation specialist adds it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from windbreak.config import (
    ConfigDiff,
    config_hash,
    load_config,
    load_default_config,
)
from windbreak.config.ledger_recorder import (
    LedgerConfigEventRecorder,
    diff_payload,
)
from windbreak.ledger.events import EVENT_TYPES, canonical_json
from windbreak.ledger.store import SqliteLedgerStore

if TYPE_CHECKING:
    from pathlib import Path


def _sample_diff() -> ConfigDiff:
    """Build a `ConfigDiff` exercising all three of added/removed/changed."""
    return ConfigDiff(
        added={"screener.new_flag": True},
        removed={"risk.retired_limit_ppm": 5000},
        changed={"risk.min_net_edge_ppm": (30000, 40000)},
    )


def test_diff_payload_shapes_added_removed_and_changed_as_lists() -> None:
    """diff_payload renders added/removed verbatim and changed tuples as lists."""
    diff = _sample_diff()

    payload = diff_payload(diff)

    assert payload == {
        "added": {"screener.new_flag": True},
        "removed": {"risk.retired_limit_ppm": 5000},
        "changed": {"risk.min_net_edge_ppm": [30000, 40000]},
    }
    assert isinstance(payload["changed"]["risk.min_net_edge_ppm"], list)


def test_diff_payload_on_empty_diff_returns_empty_dicts() -> None:
    """An empty ConfigDiff renders to three empty dicts, not omitted keys."""
    payload = diff_payload(ConfigDiff())

    assert payload == {"added": {}, "removed": {}, "changed": {}}


def test_diff_payload_output_is_deterministic_and_json_serializable() -> None:
    """diff_payload's output canonicalizes identically across repeated calls."""
    diff = _sample_diff()

    first = canonical_json(diff_payload(diff))
    second = canonical_json(diff_payload(diff))

    assert first == second
    # Round-tripping through json.loads/dumps must not raise and must
    # preserve the exact shape (lists, not tuples, for `changed`).
    assert json.loads(first)["changed"]["risk.min_net_edge_ppm"] == [30000, 40000]


def test_ledger_config_event_recorder_appends_exactly_one_config_loaded_row(
    tmp_path: Path,
) -> None:
    """record_config_loaded appends one ConfigLoaded row with hash, diff, component."""
    store = SqliteLedgerStore(tmp_path / "ledger.db")
    recorder = LedgerConfigEventRecorder(store, component="pipeline")
    diff = _sample_diff()

    recorder.record_config_loaded(
        config_hash="deadbeef" * 8, diff=diff, source="ignored-source-path"
    )

    records = store.read_all()
    store.close()
    assert len(records) == 1
    record = records[0]
    assert record.event_type == "ConfigLoaded"
    assert record.component == "pipeline"
    data = json.loads(record.payload_json)["data"]
    assert data["config_hash"] == "deadbeef" * 8
    assert data["diff"] == diff_payload(diff)


def test_ledger_config_event_recorder_never_persists_source(
    tmp_path: Path,
) -> None:
    """The ConfigLoaded schema has no source field, so source is dropped."""
    store = SqliteLedgerStore(tmp_path / "ledger.db")
    recorder = LedgerConfigEventRecorder(store, component="pipeline")

    recorder.record_config_loaded(
        config_hash="abc123", diff=ConfigDiff(), source="/etc/windbreak/config.yaml"
    )

    records = store.read_all()
    store.close()
    data = json.loads(records[0].payload_json)["data"]
    assert "source" not in data
    assert "/etc/windbreak/config.yaml" not in json.dumps(data)


def test_ledger_config_event_recorder_round_trips_through_event_types(
    tmp_path: Path,
) -> None:
    """The persisted envelope reconstructs the exact ConfigLoaded appended.

    Pins that `diff_payload`'s `changed` values are lists (not tuples): a
    tuple would round-trip through JSON as a list and break dataclass
    equality against the originally-constructed event.
    """
    store = SqliteLedgerStore(tmp_path / "ledger.db")
    recorder = LedgerConfigEventRecorder(store, component="riskkernel")
    diff = _sample_diff()

    recorder.record_config_loaded(config_hash="cafef00d", diff=diff, source="x")

    record = store.read_all()[0]
    store.close()
    envelope = json.loads(record.payload_json)
    reconstructed = EVENT_TYPES[record.event_type](
        component=envelope["component"], **envelope["data"]
    )

    expected = EVENT_TYPES["ConfigLoaded"](
        component="riskkernel", config_hash="cafef00d", diff=diff_payload(diff)
    )
    assert reconstructed == expected


def test_load_config_with_ledger_recorder_ledgers_the_loaded_hash(
    spec16_path: Path, tmp_path: Path
) -> None:
    """load_config(recorder=LedgerConfigEventRecorder(...)) appends one row.

    Exercises the `ConfigEventRecorder` protocol end-to-end: the loader
    (issue #11) calls the recorder, which appends to the real ledger
    (issue #13), with the persisted `config_hash` matching an independent
    load of the same file.
    """
    store = SqliteLedgerStore(tmp_path / "ledger.db")
    recorder = LedgerConfigEventRecorder(store, component="pipeline")

    loaded = load_config(spec16_path, recorder=recorder)

    records = store.read_all()
    store.verify_chain()
    store.close()
    assert len(records) == 1
    data = json.loads(records[0].payload_json)["data"]
    assert data["config_hash"] == config_hash(loaded)


def test_ledger_config_event_recorder_multiple_loads_verify_chain(
    spec16_path: Path, tmp_path: Path
) -> None:
    """Two consecutive loads through the recorder produce a verifiable chain."""
    store = SqliteLedgerStore(tmp_path / "ledger.db")
    recorder = LedgerConfigEventRecorder(store, component="pipeline")

    load_config(spec16_path, recorder=recorder)
    load_default_config(recorder=recorder)

    records = store.read_all()
    store.verify_chain()
    store.close()
    assert len(records) == 2
    assert [record.sequence_number for record in records] == [1, 2]
