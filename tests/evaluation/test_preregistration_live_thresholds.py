"""Failing-first tests for issue #58's `GatePlan` live-threshold extension (RED).

Like `test_registry_live_fields.py`, this file extends the already-existing
`windbreak.evaluation.preregistration` module rather than importing a
brand-new one, so there is no clean `ModuleNotFoundError`. Every test instead
fails on a `TypeError: __init__() got an unexpected keyword argument
'live_rolling_window_size'` (or a sibling new field name) at the first
`GatePlan(...)`/`build_gate_plan(...)` construction inside the test body, or on
a plain `AssertionError` once construction accidentally succeeds without the
new fields -- either way the expected Gate 1 RED state for issue #58's
preregistration-level changes.

Pins:

- `GatePlan` gains three new `int` fields --
  `live_rolling_window_size`, `live_slippage_ratio_limit_ppm`,
  `live_brier_degradation_band_ppm` -- populated by `build_gate_plan` off the
  matching new `EvaluationConfig` fields, whose confirmed defaults are `100`,
  `1_500_000`, and `50_000` respectively (issue #58's own worked example uses
  `slippage_multiple_limit_ppm=1_500_000`).
- A pre-#58, 10-canonical-key `GatePlanRegistered` record (no live-threshold
  keys at all) still reads back via `latest_gate_plan_registration`, given
  documented legacy defaults for exactly those three new keys -- content
  addressing stays schema-independent: the stored hash is verified against the
  canonical JSON of the *persisted, stripped* plan dict, not against a
  newly-serialized 13-key plan.
- Registering a new-schema plan on top of that legacy registration ledgers a
  `GatePlanChanged` linking back via `previous_plan_hash` to the legacy hash,
  with a strictly-later `paper_clock_start`.
- A hash-tampered legacy record still fails closed (`ValueError`), proving the
  legacy-read migration path does not accidentally bypass hash verification.
- `from_canonical` still rejects a mapping carrying a genuinely unknown key
  (unrelated to the three new ones), so the legacy-default carve-out is
  narrowly scoped rather than a general "ignore unknown keys" relaxation.

ASSUMPTION this file pins (the architecture plan does not name the exact
legacy default values): the documented legacy default for each of the three
new fields equals today's confirmed threshold default
(`live_rolling_window_size=100`, `live_slippage_ratio_limit_ppm=1_500_000`,
`live_brier_degradation_band_ppm=50_000`) -- a system with no live thresholds
recorded before is assumed to want today's standard defaults applied
retroactively, not some more conservative placeholder. If the implementer
documents a different legacy default, this is a design point to reconcile,
not a signal to silently rename the assertions to match whichever lands first.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from windbreak.config.schema import EvaluationConfig
from windbreak.evaluation.preregistration import build_gate_plan
from windbreak.evaluation.registry import LIVE_ROLLING_WINDOW_SIZE
from windbreak.ledger.events import Event, canonical_json
from windbreak.ledger.store import SqliteLedgerStore

if TYPE_CHECKING:
    from pathlib import Path

#: Confirmed threshold defaults (issue #58).
_LIVE_ROLLING_WINDOW_SIZE = 100
_LIVE_SLIPPAGE_RATIO_LIMIT_PPM = 1_500_000
_LIVE_BRIER_DEGRADATION_BAND_PPM = 50_000

#: A pre-#58, 10-canonical-key plan dict -- exactly the shape
#: `GatePlan.canonical_dict()` produced before issue #58 added the three new
#: fields. `metric_windows` deliberately carries only one entry: `from_canonical`
#: parses metric-window pairs generically and does not cross-check them against
#: the live registry, so a minimal catalogue is a valid, legitimate plan dict.
_LEGACY_PLAN_DICT: dict[str, object] = {
    "metric_windows": [["brier", "latest_before_close"]],
    "min_resolved_for_calibration": 150,
    "promotion_min_resolved": 300,
    "promotion_min_independent_event_groups": 100,
    "brier_skill_required_ppm": 10_000,
    "bootstrap_confidence_ppm": 950_000,
    "observation_window": "latest_before_close",
    "baseline_scheme": "executable_price_at_baseline_snapshot",
    "clustering_scheme": "event_correlation_group",
    "paper_fill_model_version": "pfm-legacy-v0",
}


def _canonical_hash(plan_dict: dict[str, object]) -> str:
    """Return the SHA-256 hex digest of a plan dict's canonical JSON.

    Args:
        plan_dict: The plan dict to hash.

    Returns:
        The 64-character lowercase hex digest.
    """
    return hashlib.sha256(canonical_json(plan_dict).encode("utf-8")).hexdigest()


def _ledger_store(directory: Path) -> SqliteLedgerStore:
    """Build a directory-backed `SqliteLedgerStore` with a deterministic clock.

    Args:
        directory: The directory to root the database file in.

    Returns:
        A fresh `SqliteLedgerStore`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    start = datetime(2024, 1, 1, tzinfo=UTC)

    def _clock() -> datetime:
        return start

    return SqliteLedgerStore(directory / "ledger.db", now=_clock)


def _append_legacy_registration(
    store: SqliteLedgerStore, *, plan_dict: dict[str, object], paper_clock_start: int
) -> str:
    """Append a raw, pre-#58, 10-key `GatePlanRegistered` record directly.

    Bypasses `register_gate_plan`/`GatePlanRegistered` entirely (both now build
    the *new*, 13-key canonical shape) to simulate a record that was actually
    written before issue #58 landed.

    Args:
        store: The ledger to append to.
        plan_dict: The legacy, 10-key canonical plan dict.
        paper_clock_start: The whole-epoch-second paper-clock start to stamp.

    Returns:
        The legacy plan's content hash.
    """
    legacy_hash = _canonical_hash(plan_dict)
    payload = {
        **plan_dict,
        "plan_hash": legacy_hash,
        "paper_clock_start": paper_clock_start,
    }
    store.append(
        Event(
            event_type="GatePlanRegistered",
            component="evaluation",
            payload_schema_version=1,
            payload=payload,
        )
    )
    return legacy_hash


# ---------------------------------------------------------------------------
# 1. GatePlan gains the three new fields, sourced from EvaluationConfig.
# ---------------------------------------------------------------------------


def test_build_gate_plan_carries_the_three_new_live_threshold_fields() -> None:
    """`build_gate_plan` off a stock `EvaluationConfig` carries the confirmed
    default live-threshold values on the resulting `GatePlan`.
    """
    plan = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-test")

    assert plan.live_rolling_window_size == _LIVE_ROLLING_WINDOW_SIZE
    assert plan.live_slippage_ratio_limit_ppm == _LIVE_SLIPPAGE_RATIO_LIMIT_PPM
    assert plan.live_brier_degradation_band_ppm == _LIVE_BRIER_DEGRADATION_BAND_PPM


def test_gate_plan_canonical_dict_round_trips_the_three_new_fields() -> None:
    """`GatePlan.canonical_dict()` / `from_canonical` round-trip the three new
    fields (and only those, on top of the original ten).
    """
    from windbreak.evaluation.preregistration import GatePlan

    plan = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-test")
    canonical = plan.canonical_dict()

    assert canonical["live_rolling_window_size"] == _LIVE_ROLLING_WINDOW_SIZE
    assert canonical["live_slippage_ratio_limit_ppm"] == _LIVE_SLIPPAGE_RATIO_LIMIT_PPM
    assert (
        canonical["live_brier_degradation_band_ppm"] == _LIVE_BRIER_DEGRADATION_BAND_PPM
    )

    rebuilt = GatePlan.from_canonical(canonical)
    assert rebuilt == plan
    assert rebuilt.plan_hash == plan.plan_hash


def test_build_gate_plan_default_window_builds_cleanly() -> None:
    """A stock `EvaluationConfig` (window == the reference constant) builds a
    plan whose `live_rolling_window_size` equals that constant.
    """
    plan = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-test")

    assert plan.live_rolling_window_size == LIVE_ROLLING_WINDOW_SIZE


def test_build_gate_plan_rejects_a_window_diverging_from_the_reference_constant() -> (
    None
):
    """`build_gate_plan` fails closed when the config's `live_rolling_window_size`
    differs from the pinned reference-path constant, so the Python and SQL
    dual-paths cannot silently score different windows.

    The window is pinned to `registry.LIVE_ROLLING_WINDOW_SIZE`; a divergent
    config value would make the reference path (which truncates to the constant)
    and the SQL path (which binds the plan's window into its `LIMIT`) disagree.
    """
    drifted = dataclasses.replace(
        EvaluationConfig(), live_rolling_window_size=LIVE_ROLLING_WINDOW_SIZE + 1
    )

    with pytest.raises(ValueError, match="live_rolling_window_size is pinned"):
        build_gate_plan(drifted, paper_fill_model_version="pfm-test")


# ---------------------------------------------------------------------------
# 2. Legacy 10-key registration still reads, with documented legacy defaults.
# ---------------------------------------------------------------------------


def test_legacy_ten_key_registration_reads_with_documented_legacy_defaults(
    tmp_path: Path,
) -> None:
    """A pre-#58, 10-key `GatePlanRegistered` record round-trips through
    `latest_gate_plan_registration`, with the three new fields defaulted per
    this suite's documented ASSUMPTION.
    """
    from windbreak.evaluation.preregistration import latest_gate_plan_registration

    store = _ledger_store(tmp_path)
    legacy_hash = _append_legacy_registration(
        store, plan_dict=_LEGACY_PLAN_DICT, paper_clock_start=1_700_000_000
    )
    try:
        registration = latest_gate_plan_registration(store)

        assert registration is not None
        assert registration.plan_hash == legacy_hash
        assert registration.plan.live_rolling_window_size == _LIVE_ROLLING_WINDOW_SIZE
        assert (
            registration.plan.live_slippage_ratio_limit_ppm
            == _LIVE_SLIPPAGE_RATIO_LIMIT_PPM
        )
        assert (
            registration.plan.live_brier_degradation_band_ppm
            == _LIVE_BRIER_DEGRADATION_BAND_PPM
        )
    finally:
        store.close()


def test_registering_new_schema_plan_over_legacy_links_previous_hash(
    tmp_path: Path,
) -> None:
    """Registering a new-schema plan on top of a legacy registration ledgers a
    `GatePlanChanged` whose `previous_plan_hash` names the legacy hash, with a
    strictly-later `paper_clock_start`.
    """
    from windbreak.evaluation.preregistration import (
        latest_gate_plan_registration,
        register_gate_plan,
    )

    store = _ledger_store(tmp_path)
    legacy_hash = _append_legacy_registration(
        store, plan_dict=_LEGACY_PLAN_DICT, paper_clock_start=1_700_000_000
    )
    try:
        # A distinct `paper_fill_model_version` from the legacy plan's
        # guarantees the new plan's hash differs, so this is unambiguously a
        # change registration, never an idempotent no-op.
        new_plan = build_gate_plan(
            EvaluationConfig(), paper_fill_model_version="pfm-post-58-v1"
        )
        registration = register_gate_plan(new_plan, store, now=lambda: 1_700_000_100)

        assert registration.event_type == "GatePlanChanged"
        assert registration.previous_plan_hash == legacy_hash
        assert registration.paper_clock_start > 1_700_000_000

        latest = latest_gate_plan_registration(store)
        assert latest is not None
        assert latest.plan_hash == new_plan.plan_hash
        # Forces this test RED against the current (pre-#58) `GatePlan`, which
        # has no `live_rolling_window_size` attribute at all; the assertions
        # above this line already pass unmodified today, so without this line
        # the test would be a false, already-green "RED" test.
        assert latest.plan.live_rolling_window_size == _LIVE_ROLLING_WINDOW_SIZE
    finally:
        store.close()


def test_hash_tampered_post_58_record_still_fails_closed(tmp_path: Path) -> None:
    """A record carrying the three new live-threshold keys, tampered after
    hashing, still fails closed -- proving the (necessarily new, since these
    keys do not exist pre-#58) verification path for the grown 13-key schema
    does not accidentally skip hash checking.

    Today (pre-implementation) `GatePlan.from_canonical` does not yet
    recognize the three new keys at all, so it raises
    `ValueError("unknown gate plan key(s): ...")` rather than the required
    `"hash mismatch"` message this test matches against -- `pytest.raises`'s
    `match=` therefore fails the test today (`Failed: DID NOT MATCH`), the
    expected Gate 1 RED state, rather than a same-exception-type false pass.
    """
    from windbreak.evaluation.preregistration import latest_gate_plan_registration

    full_dict = {
        **_LEGACY_PLAN_DICT,
        "live_rolling_window_size": _LIVE_ROLLING_WINDOW_SIZE,
        "live_slippage_ratio_limit_ppm": _LIVE_SLIPPAGE_RATIO_LIMIT_PPM,
        "live_brier_degradation_band_ppm": _LIVE_BRIER_DEGRADATION_BAND_PPM,
    }
    honest_hash = _canonical_hash(full_dict)
    tampered_dict = {
        **full_dict,
        "live_slippage_ratio_limit_ppm": 999_999,  # mutated AFTER hashing
    }
    store = _ledger_store(tmp_path)
    store.append(
        Event(
            event_type="GatePlanRegistered",
            component="evaluation",
            payload_schema_version=1,
            payload={
                **tampered_dict,
                "plan_hash": honest_hash,
                "paper_clock_start": 1_700_000_000,
            },
        )
    )
    try:
        with pytest.raises(ValueError, match="hash mismatch"):
            latest_gate_plan_registration(store)
    finally:
        store.close()


def test_from_canonical_still_rejects_a_genuinely_unknown_key() -> None:
    """`from_canonical` still rejects a mapping carrying an unrelated unknown
    key -- the legacy-default carve-out covers only the three new field names,
    not a general "ignore anything unrecognized" relaxation.

    Asserts both that the unknown key is named in the raised error AND that
    the three legitimate new field names are NOT named as unknown -- today
    (pre-implementation) `from_canonical` does not yet recognize any of the
    three new names either, so this second assertion is the one that fails
    right now, giving the expected Gate 1 RED state rather than a
    same-exception-type false pass.
    """
    from windbreak.evaluation.preregistration import GatePlan

    bogus = {
        **_LEGACY_PLAN_DICT,
        "live_rolling_window_size": _LIVE_ROLLING_WINDOW_SIZE,
        "live_slippage_ratio_limit_ppm": _LIVE_SLIPPAGE_RATIO_LIMIT_PPM,
        "live_brier_degradation_band_ppm": _LIVE_BRIER_DEGRADATION_BAND_PPM,
        "totally_bogus_key": 1,
    }

    with pytest.raises(ValueError) as excinfo:
        GatePlan.from_canonical(bogus)

    message = str(excinfo.value)
    assert "totally_bogus_key" in message
    assert "live_rolling_window_size" not in message
    assert "live_slippage_ratio_limit_ppm" not in message
    assert "live_brier_degradation_band_ppm" not in message
