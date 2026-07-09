"""Tests for folding the ledger into read models (issue #13, `rebuild`).

`rebuild` is a pure fold over `read_all()` after `verify_chain()` passes.
These tests pin: exact byte-for-byte determinism of the three read-model
files across repeated rebuilds of the same ledger, that only those three
files are ever written, that `AlertEmitted` and unrecognized event types
are silently skipped rather than erroring, that an empty ledger still
produces valid (empty) read models, and that a tampered ledger's
corruption propagates as `ChainIntegrityError` instead of silently
producing a plausible-looking but wrong read model.

Issue #40 adds a third, always-written read model: `gateway_events.json`,
a chronological `{seq, created_at, event_type, data}` projection of the
seven Order Gateway / crash-recovery event types (`OrderTransitionLedgered`,
`SubmissionRefused`, `ReduceOnlyRefused`, `ReduceOnlyViolation`,
`ReconciliationHalted`, `ReconciliationHealed`, `RecoveryCompleted`),
mirroring `config_versions.json`/`mode_history.json`'s own
always-written-even-when-empty contract. The pre-existing two read models'
content is unaffected by gateway events mixed into the same ledger.

Issue #58 (PR #199 review fix) wires two more, already-defined projection
functions -- `execution_quality_read_model` / `live_divergence_read_model` --
into `rebuild()` itself: today `rebuild()` never calls either one, so
`execution_quality.json` / `live_divergence.json` are never written at all
(not even empty). The tests below pin that both files are always written
(empty-but-valid on an empty ledger) and hold exactly the
`ExecutionQualityRecorded` / `LiveDivergenceSampled` rows, in ledger order,
once `rebuild()` is wired up.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from windbreak.evaluation.execution_quality import (
    ExecutionQualityRecord,
    ExecutionQualityRecorded,
)
from windbreak.evaluation.live_divergence import LiveDivergenceSampled
from windbreak.ledger.events import (
    AlertEmitted,
    ConfigLoaded,
    ModeHeartbeat,
    OrderTransitionLedgered,
    ReconciliationHalted,
    ReconciliationHealed,
    RecoveryCompleted,
    ReduceOnlyRefused,
    ReduceOnlyViolation,
    SubmissionRefused,
    canonical_json,
)
from windbreak.ledger.rebuild import rebuild
from windbreak.ledger.store import (
    ChainIntegrityError,
    SqliteLedgerStore,
    compute_event_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path


def _populate_interleaved_ledger(store: SqliteLedgerStore) -> None:
    """Append a fixed, interleaved sequence of all three M0 event types.

    Sequence numbers: 1=ConfigLoaded, 2=ModeHeartbeat, 3=AlertEmitted,
    4=ModeHeartbeat, 5=ConfigLoaded.
    """
    store.append(
        ConfigLoaded(component="pipeline", config_hash="hash-1", diff={"a": 1})
    )
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.append(AlertEmitted(component="alerts", severity="low", message="noop"))
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=2))
    store.append(
        ConfigLoaded(component="pipeline", config_hash="hash-2", diff={"a": 2})
    )


def test_rebuild_writes_only_the_documented_read_model_files(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """Only the documented read models ever appear in output_dir.

    Every read model is written unconditionally (empty here where no source
    events are present) -- the original three plus the three PAPER-loop read
    models issue #48 adds (`positions.json`, `equity_curve.json`,
    `selector_decisions.json`), plus the two live-divergence read models
    issue #58 wires up (`execution_quality.json`, `live_divergence.json`,
    PR #199 review fix). This currently FAILS: `rebuild()` never calls
    `execution_quality_read_model` / `live_divergence_read_model`, so neither
    new file is produced at all.
    """
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    produced = sorted(path.name for path in output_dir.iterdir())
    assert produced == [
        "config_versions.json",
        "equity_curve.json",
        "execution_quality.json",
        "gateway_events.json",
        "live_divergence.json",
        "mode_history.json",
        "positions.json",
        "selector_decisions.json",
    ]


def test_rebuild_is_byte_for_byte_deterministic_across_repeated_runs(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """Rebuilding the same ledger into two different output dirs is byte-identical."""
    db_path = tmp_path / "ledger.db"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    first_dir = tmp_path / "out1"
    second_dir = tmp_path / "out2"
    rebuild(db_path, first_dir)
    rebuild(db_path, second_dir)

    for name in ("config_versions.json", "gateway_events.json", "mode_history.json"):
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()


def test_rebuild_config_versions_contains_only_config_loaded_projection_in_order(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """config_versions.json holds exactly the ConfigLoaded rows, in seq order."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    config_versions = json.loads((output_dir / "config_versions.json").read_text())
    assert [entry["seq"] for entry in config_versions] == [1, 5]
    assert [entry["config_hash"] for entry in config_versions] == ["hash-1", "hash-2"]
    assert [entry["diff"] for entry in config_versions] == [{"a": 1}, {"a": 2}]
    assert all("created_at" in entry for entry in config_versions)


def test_rebuild_mode_history_contains_only_mode_heartbeat_projection_in_order(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """mode_history.json holds exactly the ModeHeartbeat rows, in seq order."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    mode_history = json.loads((output_dir / "mode_history.json").read_text())
    assert [entry["seq"] for entry in mode_history] == [2, 4]
    assert [entry["mode"] for entry in mode_history] == ["RESEARCH", "RESEARCH"]
    assert [entry["beat"] for entry in mode_history] == [1, 2]
    assert all("created_at" in entry for entry in mode_history)


def test_rebuild_read_models_are_canonical_json_with_one_trailing_newline(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """Each read model is canonical JSON bytes ending in exactly one newline."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    for name in ("config_versions.json", "gateway_events.json", "mode_history.json"):
        raw = (output_dir / name).read_bytes()
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")
        body = raw[:-1].decode("utf-8")
        assert body == canonical_json(json.loads(body))


def test_rebuild_on_empty_ledger_produces_valid_empty_read_models(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """An empty ledger still produces well-formed, empty read models.

    Includes `execution_quality.json` / `live_divergence.json` (issue #58,
    PR #199 review fix): both currently FAIL with `FileNotFoundError` because
    `rebuild()` never writes either file today, empty ledger or not.
    """
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.close()

    rebuild(db_path, output_dir)

    assert json.loads((output_dir / "config_versions.json").read_text()) == []
    assert json.loads((output_dir / "gateway_events.json").read_text()) == []
    assert json.loads((output_dir / "mode_history.json").read_text()) == []
    assert json.loads((output_dir / "execution_quality.json").read_text()) == []
    assert json.loads((output_dir / "live_divergence.json").read_text()) == []


def test_rebuild_skips_unknown_event_types_without_error(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """An event_type absent from EVENT_TYPES is silently skipped, not an error."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.close()

    created_at = "2099-01-01T00:00:00.000000+00:00"
    payload_json = canonical_json(
        {"component": "future", "data": {"whatever": True}, "schema_version": 1}
    )
    conn = sqlite3.connect(db_path)
    try:
        prev_hash = conn.execute(
            "SELECT event_hash FROM ledger WHERE sequence_number = 1"
        ).fetchone()[0]
        event_hash = compute_event_hash(
            2, "FutureEvent", created_at, payload_json, prev_hash
        )
        conn.execute(
            "INSERT INTO ledger ("
            "sequence_number, event_type, created_at, component, "
            "payload_json, payload_schema_version, prev_hash, event_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                "FutureEvent",
                created_at,
                "future",
                payload_json,
                1,
                prev_hash,
                event_hash,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    rebuild(db_path, output_dir)

    assert json.loads((output_dir / "config_versions.json").read_text()) == []
    assert json.loads((output_dir / "gateway_events.json").read_text()) == []
    mode_history = json.loads((output_dir / "mode_history.json").read_text())
    assert len(mode_history) == 1
    assert mode_history[0]["beat"] == 1


def _populate_gateway_and_recovery_events_ledger(store: SqliteLedgerStore) -> None:
    """Append M0 events interleaved with all seven gateway/recovery events.

    Sequence numbers: 1=ConfigLoaded, 2=OrderTransitionLedgered,
    3=ModeHeartbeat, 4=SubmissionRefused, 5=ReduceOnlyRefused, 6=AlertEmitted,
    7=ReduceOnlyViolation, 8=ReconciliationHalted, 9=ReconciliationHealed,
    10=RecoveryCompleted.

    Args:
        store: The ledger store to append the fixed sequence into.
    """
    store.append(ConfigLoaded(component="pipeline", config_hash="hash-1", diff={}))
    store.append(
        OrderTransitionLedgered(
            component="order_gateway",
            client_order_id="coid-1",
            from_state="INTENT_CREATED",
            event="APPROVE",
            to_state="APPROVED",
        )
    )
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.append(
        SubmissionRefused(
            component="order_gateway", client_order_id="coid-2", reason="closed"
        )
    )
    store.append(
        ReduceOnlyRefused(
            component="order_gateway",
            client_order_id="coid-3",
            ticker="MKT-DEEP",
            held_centis=500,
            inflight_closing_centis=0,
            requested_close_centis=600,
            reason="reduce_only",
        )
    )
    store.append(AlertEmitted(component="alerts", severity="low", message="noop"))
    store.append(
        ReduceOnlyViolation(
            component="order_gateway",
            client_order_id="coid-4",
            ticker="MKT-DEEP",
            held_centis=500,
            filled_centis=600,
            net_centis=-100,
        )
    )
    store.append(
        ReconciliationHalted(
            component="order_gateway",
            reason="foreign_open_order",
            ticker="MKT-DEEP",
            venue_order_id="paper-order-9",
            client_order_id="",
            detail="untracked order discovered on the venue",
        )
    )
    store.append(
        ReconciliationHealed(
            component="order_gateway",
            client_order_id="coid-5",
            action="fill_confirmed",
            detail="matched an out-of-band fill",
        )
    )
    store.append(
        RecoveryCompleted(component="order_gateway", orders_reconciled=3, halted=False)
    )


def test_rebuild_gateway_events_contains_only_the_seven_gateway_rows_in_order(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """gateway_events.json holds exactly the 7 gateway/recovery rows, in order.

    Each entry is the `{seq, created_at, event_type, data}` shape; the
    pre-existing `config_versions.json`/`mode_history.json` projections are
    unaffected by the gateway events mixed into the same ledger.
    """
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_gateway_and_recovery_events_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    gateway_events = json.loads((output_dir / "gateway_events.json").read_text())
    assert [entry["seq"] for entry in gateway_events] == [2, 4, 5, 7, 8, 9, 10]
    assert [entry["event_type"] for entry in gateway_events] == [
        "OrderTransitionLedgered",
        "SubmissionRefused",
        "ReduceOnlyRefused",
        "ReduceOnlyViolation",
        "ReconciliationHalted",
        "ReconciliationHealed",
        "RecoveryCompleted",
    ]
    assert all("created_at" in entry for entry in gateway_events)
    assert gateway_events[0]["data"]["client_order_id"] == "coid-1"
    assert gateway_events[-1]["data"] == {"orders_reconciled": 3, "halted": False}

    config_versions = json.loads((output_dir / "config_versions.json").read_text())
    assert [entry["seq"] for entry in config_versions] == [1]
    mode_history = json.loads((output_dir / "mode_history.json").read_text())
    assert [entry["seq"] for entry in mode_history] == [3]


def _populate_execution_quality_and_divergence_ledger(store: SqliteLedgerStore) -> None:
    """Append M0 events interleaved with one execution-quality and one
    live-divergence event (issue #58, PR #199 review fix).

    Sequence numbers: 1=ConfigLoaded, 2=ExecutionQualityRecorded,
    3=ModeHeartbeat, 4=LiveDivergenceSampled.

    Args:
        store: The ledger store to append the fixed sequence into.
    """
    store.append(ConfigLoaded(component="pipeline", config_hash="hash-1", diff={}))
    record = ExecutionQualityRecord(
        fill_id="F-rebuild-1",
        market_ticker="MKT-REBUILD",
        side="YES",
        filled_centis=100,
        actual_cost_micros=1_100_000,
        modeled_cost_micros=1_000_000,
        model_version="pfm-rebuild-test",
        created_sequence=1,
    )
    store.append(ExecutionQualityRecorded(component="evaluation", record=record))
    store.append(ModeHeartbeat(component="pipeline", mode="RESEARCH", beat=1))
    store.append(
        LiveDivergenceSampled(
            component="evaluation",
            sample={
                "live_slippage_ratio_ppm": 1_100_000,
                "live_brier_degradation_ppm": "UNDEFINED",
                "plan_hash": "hash-rebuild-test",
            },
        )
    )


def test_rebuild_execution_quality_and_live_divergence_projections_are_wired(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """`rebuild()` also projects `ExecutionQualityRecorded` /
    `LiveDivergenceSampled` rows into `execution_quality.json` /
    `live_divergence.json`, in ledger order (issue #58, PR #199 review fix).

    Today `rebuild()` never calls `execution_quality_read_model` /
    `live_divergence_read_model` even though both are already defined, so
    reading either file currently raises `FileNotFoundError`.
    """
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_execution_quality_and_divergence_ledger(store)
    store.close()

    rebuild(db_path, output_dir)

    execution_quality = json.loads((output_dir / "execution_quality.json").read_text())
    assert [entry["seq"] for entry in execution_quality] == [2]
    assert execution_quality[0]["event_type"] == "ExecutionQualityRecorded"
    assert execution_quality[0]["data"]["fill_id"] == "F-rebuild-1"
    assert execution_quality[0]["data"]["actual_cost_micros"] == 1_100_000
    assert execution_quality[0]["data"]["modeled_cost_micros"] == 1_000_000
    assert all("created_at" in entry for entry in execution_quality)

    live_divergence = json.loads((output_dir / "live_divergence.json").read_text())
    assert [entry["seq"] for entry in live_divergence] == [4]
    assert live_divergence[0]["event_type"] == "LiveDivergenceSampled"
    assert live_divergence[0]["data"]["live_slippage_ratio_ppm"] == 1_100_000
    assert live_divergence[0]["data"]["plan_hash"] == "hash-rebuild-test"
    assert all("created_at" in entry for entry in live_divergence)

    # The pre-existing read models are unaffected by the two new event types
    # mixed into the same ledger.
    config_versions = json.loads((output_dir / "config_versions.json").read_text())
    assert [entry["seq"] for entry in config_versions] == [1]
    mode_history = json.loads((output_dir / "mode_history.json").read_text())
    assert [entry["seq"] for entry in mode_history] == [3]


def test_rebuild_on_tampered_ledger_raises_chain_integrity_error(
    tmp_path: Path, deterministic_clock: Callable[[], datetime]
) -> None:
    """rebuild verifies the chain first, so a corrupt ledger never projects."""
    db_path = tmp_path / "ledger.db"
    output_dir = tmp_path / "out"
    store = SqliteLedgerStore(db_path, now=deterministic_clock)
    _populate_interleaved_ledger(store)
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE ledger SET event_hash = ? WHERE sequence_number = 3",
            ("0" * 64,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ChainIntegrityError):
        rebuild(db_path, output_dir)
