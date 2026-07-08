"""Failing-first tests wiring `monitor_live_divergence` into a REAL `RiskKernel`
(issue #58, RED) -- the issue's own headline scenario.

`windbreak.evaluation.live_divergence` does not exist yet, so every test below
imports `monitor_live_divergence` as the FIRST statement inside the test body,
matching this package's RED convention, so each test fails independently on
its own
`ModuleNotFoundError: No module named 'windbreak.evaluation.live_divergence'`.
Symbols from already-existing modules (`windbreak.riskkernel.demotion`,
`windbreak.riskkernel.modes`, `windbreak.riskkernel.process`, the evaluation
fixture-building helpers) are imported at module scope or reused from
`tests/evaluation/test_live_divergence.py` (DRY, mirroring
`test_app_scheduler_routes.py`'s reuse of `test_app.py`'s helpers).

Pins the concrete promotion-ladder consequence of a live-divergence breach
(SPEC §10.9/§10.10): a real `RiskKernel` parked at `LIVE_MICRO`, wired so
`monitor_live_divergence`'s `fire_trigger` parameter is the kernel's own
`fire_demotion_trigger` bound method, demotes exactly one ladder rung
(`LIVE_MICRO -> PAPER`) per breached series, and the *same* ledger holds the
`LiveDivergenceBreached` event immediately followed by the resulting
`DemotionTriggerFired` event -- proving the monitor's breach and the kernel's
reaction land in one auditable, ordered trail rather than two disconnected
logs.

ASSUMPTION this file pins (mirroring `tests/riskkernel/test_demotion.py`'s own
ASSUMPTION convention): `windbreak.riskkernel.process.KernelLedgerWriter` is a
structural `Protocol` (`.record(event) -> None`), while
`windbreak.ledger.store.LedgerStore` is a structural `Protocol` with a
different shape (`.append(event) -> int`). For a `LiveDivergenceBreached` and
the `DemotionTriggerFired` it causes to land in one ordered ledger, the kernel
must be constructed with a `KernelLedgerWriter` adapter that forwards
`.record(event)` into the *same* `SqliteLedgerStore` `monitor_live_divergence`
appends to. `_StoreBackedKernelLedgerWriter` below is that adapter -- test-only
scaffolding, not production code, mirroring `test_dual_path.py`'s local
`_ledger_store` / `_recording_alert_hook` helpers.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.evaluation.test_live_divergence import (
    _both_breach_inputs,
    _brier_breach_only_inputs,
    _built_plan,
    _ledger_store,
    _recording_alert_hook,
    _slippage_breach_only_inputs,
)
from windbreak.riskkernel.modes import Mode, ModeStateMachine
from windbreak.riskkernel.process import RiskKernel

if TYPE_CHECKING:
    from pathlib import Path

    from windbreak.ledger.events import Event
    from windbreak.ledger.store import LedgerStore


class _StoreBackedKernelLedgerWriter:
    """A `KernelLedgerWriter` that forwards every recorded event into a
    `LedgerStore`, so a kernel's events land in the same ledger a
    `monitor_live_divergence` call writes to.
    """

    def __init__(self, store: LedgerStore) -> None:
        """Bind the backing store.

        Args:
            store: The ledger store to append every recorded event to.
        """
        self._store = store

    def record(self, event: Event) -> None:
        """Forward one kernel event into the backing store.

        Args:
            event: The event to persist.
        """
        self._store.append(event)


def _kernel_at_live_micro(store: LedgerStore) -> RiskKernel:
    """Build a `RiskKernel` parked at `LIVE_MICRO`, ledgering into `store`.

    Args:
        store: The ledger store the kernel's `KernelLedgerWriter` forwards into.

    Returns:
        A `RiskKernel` ceilinged at `LIVE` (so `LIVE_MICRO` is not itself the
        ceiling) and parked at `LIVE_MICRO`.
    """
    machine = ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE_MICRO)
    return RiskKernel(_StoreBackedKernelLedgerWriter(store), mode_machine=machine)


def _breach_and_trigger_records(store: LedgerStore) -> tuple[list, list]:
    """Split a store's records into `LiveDivergenceBreached` and
    `DemotionTriggerFired` sublists, each in ledger order.

    Args:
        store: The ledger store to read.

    Returns:
        A `(breached, fired)` pair of record lists.
    """
    records = store.read_all()
    breached = [r for r in records if r.event_type == "LiveDivergenceBreached"]
    fired = [r for r in records if r.event_type == "DemotionTriggerFired"]
    return breached, fired


def test_slippage_divergence_demotes_to_paper(tmp_path: Path) -> None:
    """A slippage-only breach fired against a real `LIVE_MICRO` kernel demotes
    it exactly one rung to `PAPER`. The ledger holds `LiveDivergenceBreached`
    (full series snapshot + threshold + `plan_hash`) immediately followed by
    `DemotionTriggerFired(trigger="LIVE_PAPER_SLIPPAGE_DIVERGENCE",
    transitioned=True)`. Exactly one `AlertSeverity.CRITICAL` alert fires.
    """
    from windbreak.evaluation.live_divergence import monitor_live_divergence
    from windbreak.evaluation.registry import gate_evaluation_inputs

    store = _ledger_store(tmp_path)
    kernel = _kernel_at_live_micro(store)
    inputs, _rejections = gate_evaluation_inputs(_slippage_breach_only_inputs())
    plan = _built_plan()
    alert_calls, alert_hook = _recording_alert_hook()
    try:
        monitor_live_divergence(
            inputs,
            plan=plan,
            store=store,
            alert=alert_hook,
            fire_trigger=kernel.fire_demotion_trigger,
            component="evaluation",
        )

        assert kernel.mode is Mode.PAPER

        records = store.read_all()
        event_types = [record.event_type for record in records]
        breach_index = event_types.index("LiveDivergenceBreached")
        fired_index = event_types.index("DemotionTriggerFired")
        assert breach_index < fired_index

        breached, fired = _breach_and_trigger_records(store)
        assert len(breached) == 1
        assert len(fired) == 1

        breach_payload = json.loads(breached[0].payload_json)["data"]
        assert breach_payload["trigger"] == "LIVE_PAPER_SLIPPAGE_DIVERGENCE"
        assert breach_payload["plan_hash"] == plan.plan_hash

        fired_payload = json.loads(fired[0].payload_json)["data"]
        assert fired_payload["trigger"] == "LIVE_PAPER_SLIPPAGE_DIVERGENCE"
        assert fired_payload["transitioned"] is True
        assert fired_payload["from_mode"] == "LIVE_MICRO"
        assert fired_payload["to_mode"] == "PAPER"

        assert len(alert_calls) == 1
        assert alert_calls[0][0].name == "CRITICAL"
    finally:
        store.close()


def test_brier_degradation_divergence_demotes_to_paper(tmp_path: Path) -> None:
    """The Brier-degradation twin of `test_slippage_divergence_demotes_to_paper`:
    a Brier-only breach demotes `LIVE_MICRO -> PAPER` and ledgers
    `DemotionTriggerFired(trigger="ROLLING_BRIER_DEGRADATION")`.
    """
    from windbreak.evaluation.live_divergence import monitor_live_divergence
    from windbreak.evaluation.registry import gate_evaluation_inputs

    store = _ledger_store(tmp_path)
    kernel = _kernel_at_live_micro(store)
    inputs, _rejections = gate_evaluation_inputs(_brier_breach_only_inputs())
    plan = _built_plan()
    alert_calls, alert_hook = _recording_alert_hook()
    try:
        monitor_live_divergence(
            inputs,
            plan=plan,
            store=store,
            alert=alert_hook,
            fire_trigger=kernel.fire_demotion_trigger,
            component="evaluation",
        )

        assert kernel.mode is Mode.PAPER

        breached, fired = _breach_and_trigger_records(store)
        assert len(breached) == 1
        assert len(fired) == 1
        assert (
            json.loads(fired[0].payload_json)["data"]["trigger"]
            == "ROLLING_BRIER_DEGRADATION"
        )
        assert len(alert_calls) == 1
    finally:
        store.close()


def test_both_series_breaching_demotes_two_rungs(tmp_path: Path) -> None:
    """When both series breach in the same run, the kernel is demoted twice --
    the fail-safe "double one-rung demotion" reading: `LIVE_MICRO -> PAPER ->
    RESEARCH`. Two `DemotionTriggerFired` events are ledgered, both
    `transitioned=True`.
    """
    from windbreak.evaluation.live_divergence import monitor_live_divergence
    from windbreak.evaluation.registry import gate_evaluation_inputs

    store = _ledger_store(tmp_path)
    kernel = _kernel_at_live_micro(store)
    inputs, _rejections = gate_evaluation_inputs(_both_breach_inputs())
    plan = _built_plan()
    alert_calls, alert_hook = _recording_alert_hook()
    try:
        monitor_live_divergence(
            inputs,
            plan=plan,
            store=store,
            alert=alert_hook,
            fire_trigger=kernel.fire_demotion_trigger,
            component="evaluation",
        )

        assert kernel.mode is Mode.RESEARCH

        breached, fired = _breach_and_trigger_records(store)
        assert len(breached) == 2
        assert len(fired) == 2
        assert all(
            json.loads(record.payload_json)["data"]["transitioned"] is True
            for record in fired
        )
        assert len(alert_calls) == 2
    finally:
        store.close()
