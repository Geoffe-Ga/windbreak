"""Failing-first tests for `windbreak.evaluation.preregistration` (#54, RED).

`windbreak.evaluation.preregistration` does not exist yet, so every test below
imports its new symbols from that module as the FIRST statement inside the
test body (matching this package's established RED convention in
`test_windows.py` and `test_cohorts.py`) so each test collects and fails
independently on its own
`ModuleNotFoundError: No module named 'windbreak.evaluation.preregistration'`
rather than one collection-time explosion. Symbols from already-existing
modules (`windbreak.config.schema`, `windbreak.evaluation.registry`,
`windbreak.ledger.store`, `windbreak.ledger.events`) are imported at module
scope, since those modules already exist and importing them cannot hide which
new behavior a given test covers.

Pins SPEC §13.6 / T15's pre-registered gate plan:

- A `GatePlan` is a frozen, all-int-or-str-or-tuple (never float) snapshot of
  the evaluation gate's configuration: the metric/window catalogue, the five
  promotion/calibration thresholds, the observation window, the two named
  schemes (executable-price baseline, event-correlation clustering), and the
  paper fill-model version.
- `GatePlan.plan_hash` is the 64-hex-char SHA-256 of `canonical_json_str`
  (the ledger's `canonical_json` over `canonical_dict()`), and is
  **order-independent**: `metric_windows` is normalized to sorted-by-name
  order in `__post_init__`, so two plans built from the same metric set in
  different input orders hash identically (ACCEPTANCE #1). Any single-field
  change -- including a metric's window, and including
  `paper_fill_model_version` alone (SPEC S17.4, ACCEPTANCE #3) -- changes the
  hash (ACCEPTANCE #2a).
- `register_gate_plan` ledgers a `GatePlanRegistered` on first registration
  (`paper_clock_start = now()`), is idempotent on a byte-identical
  re-registration (no new event, clock not reset), and ledgers a
  `GatePlanChanged` carrying `previous_plan_hash` with a strictly later
  `paper_clock_start` on any change -- fail-closed (raising, appending
  nothing) if the injected clock is not strictly monotonic.
- `latest_gate_plan_registration` reconstructs the most recent registration's
  `GatePlan` from the ledger alone.

Note on the ACCEPTANCE #1 known-answer pin: this authoring session has no
code-execution channel to hand-compute a SHA-256 hex digest and transcribe it
as a separate literal (the risk of a manual bit-level SHA-256 transcription
error is real and would falsely fail a *correct* implementation). The
canonical JSON string below is instead pinned as an exact literal -- pure
string formatting, independently hand-verified -- and `plan_hash` is checked
against `hashlib.sha256` of that exact literal, computed once at test time.
`hashlib.sha256` is a pure, deterministic, unseeded function, so this is
equivalent in rigor to a hard-coded hex constant while removing transcription
risk; see `test_gate_plan_canonical_json_and_hash_pin_known_answer`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest

from windbreak.config.schema import EvaluationConfig
from windbreak.evaluation import registry
from windbreak.ledger.events import canonical_json
from windbreak.ledger.store import SqliteLedgerStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: The exact sorted-keys, whitespace-free canonical JSON for a minimal
#: one-metric `GatePlan` (see
#: `test_gate_plan_canonical_json_and_hash_pin_known_answer`'s docstring for
#: the field values this renders).
_KNOWN_ANSWER_JSON = (
    '{"baseline_scheme":"executable_price_at_baseline_snapshot",'
    '"bootstrap_confidence_ppm":1,"brier_skill_required_ppm":1,'
    '"clustering_scheme":"event_correlation_group",'
    '"live_brier_degradation_band_ppm":50000,'
    '"live_rolling_window_size":100,'
    '"live_slippage_ratio_limit_ppm":1500000,'
    '"metric_windows":[["brier","latest_before_close"]],'
    '"min_resolved_for_calibration":1,'
    '"observation_window":"latest_before_close",'
    '"paper_fill_model_version":"pfm-v1",'
    '"promotion_min_independent_event_groups":1,'
    '"promotion_min_resolved":1}'
)


class _DeterministicUtcClock:
    """A minimal, self-contained deterministic UTC clock for a ledger store.

    Mirrors `tests/ledger/conftest.py`'s `DeterministicClock` (fixed
    2024-01-01T00:00:00+00:00 UTC start, one-second steps) without importing
    across this suite's package boundary, so every ledger row's `created_at`
    (and therefore its `event_hash`) is fully reproducible.
    """

    def __init__(self) -> None:
        """Initialize the clock at a fixed 2024-01-01T00:00:00+00:00 UTC."""
        self._current = datetime(2024, 1, 1, tzinfo=UTC)
        self._calls = 0

    def __call__(self) -> datetime:
        """Return the next deterministic UTC datetime.

        Returns:
            The fixed start time on the first call, then a value advanced by
            one second on every subsequent call.
        """
        if self._calls > 0:
            self._current = self._current + timedelta(seconds=1)
        self._calls += 1
        return self._current


def _ledger_store(tmp_path: Path) -> SqliteLedgerStore:
    """Build a tmp-path-backed `SqliteLedgerStore` with a deterministic clock.

    Args:
        tmp_path: The pytest-provided temporary directory to root the
            database file in.

    Returns:
        A fresh `SqliteLedgerStore` whose `created_at` values are fully
        reproducible via `_DeterministicUtcClock`.
    """
    return SqliteLedgerStore(tmp_path / "ledger.db", now=_DeterministicUtcClock())


def _sequence_clock(*epochs: int) -> Callable[[], int]:
    """Build a `now` callable returning each of `epochs`, once, in order.

    Args:
        epochs: The whole-epoch-second values to return, one per call.

    Returns:
        A callable that pops the next scripted epoch on every call.
    """
    iterator = iter(epochs)

    def _next_epoch() -> int:
        """Return the next scripted epoch."""
        return next(iterator)

    return _next_epoch


def _constant_clock(epoch: int) -> Callable[[], int]:
    """Build a `now` callable returning the same fixed `epoch` every call.

    Args:
        epoch: The whole-epoch-second value returned on every call.

    Returns:
        A callable that always returns `epoch`.
    """

    def _same_epoch() -> int:
        """Return the fixed epoch on every call."""
        return epoch

    return _same_epoch


# ---------------------------------------------------------------------------
# 1. build_gate_plan: derives the full metric/window catalogue and the five
#    thresholds off a real EvaluationConfig and the live metric registry.
# ---------------------------------------------------------------------------


def test_build_gate_plan_derives_all_eleven_registry_metric_windows() -> None:
    """`build_gate_plan` derives `metric_windows` from the live registry.

    Every metric in `windbreak.evaluation.registry.registered_metrics()` maps
    to `(name, window.value)`, sorted by name -- re-derived here from the
    registry itself (immune to registry churn) and cross-checked against a
    hand-verified literal tuple, so a silent registry-shape change is still
    caught. The five thresholds and `observation_window` are copied exactly
    off `EvaluationConfig()`'s SPEC-§16 defaults, and the two scheme fields
    default to the two named constants.
    """
    from windbreak.evaluation.preregistration import (
        CORRELATION_GROUP_CLUSTERING_SCHEME,
        EXECUTABLE_PRICE_BASELINE_SCHEME,
        build_gate_plan,
    )

    expected_from_registry = tuple(
        sorted(
            (name, spec.window.value)
            for name, spec in registry.registered_metrics().items()
        )
    )
    expected_literal = (
        ("brier", "latest_before_close"),
        ("brier_skill_vs_executable_price", "latest_before_close"),
        ("calibration_intercept", "latest_before_close"),
        ("calibration_slope", "latest_before_close"),
        ("expected_calibration_error", "latest_before_close"),
        ("fill_vs_model_slippage", "trade_triggering"),
        ("live_brier_degradation", "latest_before_close"),
        ("live_slippage_ratio", "trade_triggering"),
        ("log_score", "latest_before_close"),
        ("sharpness", "latest_before_close"),
        ("traded_vs_skipped_brier_delta", "latest_before_close"),
    )
    assert expected_from_registry == expected_literal

    plan = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")

    assert plan.metric_windows == expected_literal
    assert len(plan.metric_windows) == 11
    assert plan.min_resolved_for_calibration == 150
    assert plan.promotion_min_resolved == 300
    assert plan.promotion_min_independent_event_groups == 100
    assert plan.brier_skill_required_ppm == 10_000
    assert plan.bootstrap_confidence_ppm == 950_000
    assert plan.observation_window == "latest_before_close"
    assert plan.baseline_scheme == EXECUTABLE_PRICE_BASELINE_SCHEME
    assert plan.clustering_scheme == CORRELATION_GROUP_CLUSTERING_SCHEME
    assert plan.paper_fill_model_version == "pfm-v1"


# ---------------------------------------------------------------------------
# 2. ACCEPTANCE #1: same plan -> same hash, order-independent; known-answer
#    pin of the exact canonical JSON string and its SHA-256.
# ---------------------------------------------------------------------------


def test_gate_plan_hash_is_order_independent_of_metric_windows_input_order() -> None:
    """ACCEPTANCE #1: metric-window input order never affects identity.

    Two plans built from the identical three-metric set, one given in
    already-sorted order and one given fully reversed, must normalize (via
    `__post_init__`) to the same sorted tuple and therefore be
    byte-identical in `canonical_json_str` and `plan_hash`.
    """
    from windbreak.evaluation.preregistration import GatePlan

    plan_sorted_input = GatePlan(
        metric_windows=(
            ("brier", "latest_before_close"),
            ("log_score", "latest_before_close"),
            ("sharpness", "latest_before_close"),
        ),
        min_resolved_for_calibration=150,
        promotion_min_resolved=300,
        promotion_min_independent_event_groups=100,
        brier_skill_required_ppm=10_000,
        bootstrap_confidence_ppm=950_000,
        observation_window="latest_before_close",
        baseline_scheme="executable_price_at_baseline_snapshot",
        clustering_scheme="event_correlation_group",
        paper_fill_model_version="pfm-v1",
    )
    plan_reversed_input = GatePlan(
        metric_windows=(
            ("sharpness", "latest_before_close"),
            ("log_score", "latest_before_close"),
            ("brier", "latest_before_close"),
        ),
        min_resolved_for_calibration=150,
        promotion_min_resolved=300,
        promotion_min_independent_event_groups=100,
        brier_skill_required_ppm=10_000,
        bootstrap_confidence_ppm=950_000,
        observation_window="latest_before_close",
        baseline_scheme="executable_price_at_baseline_snapshot",
        clustering_scheme="event_correlation_group",
        paper_fill_model_version="pfm-v1",
    )

    assert plan_sorted_input.metric_windows == plan_reversed_input.metric_windows
    assert plan_sorted_input.metric_windows == (
        ("brier", "latest_before_close"),
        ("log_score", "latest_before_close"),
        ("sharpness", "latest_before_close"),
    )
    assert (
        plan_sorted_input.canonical_json_str == plan_reversed_input.canonical_json_str
    )
    assert plan_sorted_input.plan_hash == plan_reversed_input.plan_hash


def test_gate_plan_canonical_json_and_hash_pin_known_answer() -> None:
    """Known-answer pin for a minimal one-metric `GatePlan`.

    Fields: a single `("brier", "latest_before_close")` metric window, every
    threshold set to `1`, `observation_window="latest_before_close"`, the two
    default scheme constants, and `paper_fill_model_version="pfm-v1"`. The three
    issue-#58 live-threshold fields are left unset, so they carry their defaulted
    values (`100`, `1_500_000`, `50_000`) in the canonical JSON.
    `canonical_json_str` must equal `_KNOWN_ANSWER_JSON` byte-for-byte (sorted
    keys, no whitespace); `plan_hash` must be exactly the 64-lowercase-hex
    SHA-256 digest of that literal (see module docstring for why this is
    computed via `hashlib` here rather than a separately hand-transcribed hex
    constant).
    """
    from windbreak.evaluation.preregistration import GatePlan

    plan = GatePlan(
        metric_windows=(("brier", "latest_before_close"),),
        min_resolved_for_calibration=1,
        promotion_min_resolved=1,
        promotion_min_independent_event_groups=1,
        brier_skill_required_ppm=1,
        bootstrap_confidence_ppm=1,
        observation_window="latest_before_close",
        baseline_scheme="executable_price_at_baseline_snapshot",
        clustering_scheme="event_correlation_group",
        paper_fill_model_version="pfm-v1",
    )

    assert plan.canonical_json_str == _KNOWN_ANSWER_JSON

    expected_hash = hashlib.sha256(_KNOWN_ANSWER_JSON.encode("utf-8")).hexdigest()
    assert plan.plan_hash == expected_hash
    assert len(plan.plan_hash) == 64
    assert plan.plan_hash == plan.plan_hash.lower()


# ---------------------------------------------------------------------------
# 3. ACCEPTANCE #2a: any single-field change (every field, one at a time)
#    changes plan_hash.
# ---------------------------------------------------------------------------


def test_gate_plan_single_field_mutation_always_changes_plan_hash() -> None:
    """ACCEPTANCE #2a: every field, mutated alone, changes `plan_hash`.

    Covers every `GatePlan` field: each of the eight int thresholds (+1), the
    `observation_window` string, both scheme strings,
    `paper_fill_model_version`, and one `metric_windows` entry's window
    value -- each mutated alone off a shared baseline built by
    `build_gate_plan`. The baseline is built via `build_gate_plan`, but each
    variant is produced with `dataclasses.replace`, which bypasses that
    function's window-pinning guard, so mutating `live_rolling_window_size`
    to prove it is content-addressed is safe here.
    """
    from windbreak.evaluation.preregistration import build_gate_plan

    baseline = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")

    mutated_metric_windows = tuple(
        (name, "daily_snapshots") if name == "brier" else (name, window)
        for name, window in baseline.metric_windows
    )

    variants = {
        "min_resolved_for_calibration": dataclasses.replace(
            baseline,
            min_resolved_for_calibration=baseline.min_resolved_for_calibration + 1,
        ),
        "promotion_min_resolved": dataclasses.replace(
            baseline, promotion_min_resolved=baseline.promotion_min_resolved + 1
        ),
        "promotion_min_independent_event_groups": dataclasses.replace(
            baseline,
            promotion_min_independent_event_groups=(
                baseline.promotion_min_independent_event_groups + 1
            ),
        ),
        "brier_skill_required_ppm": dataclasses.replace(
            baseline, brier_skill_required_ppm=baseline.brier_skill_required_ppm + 1
        ),
        "bootstrap_confidence_ppm": dataclasses.replace(
            baseline, bootstrap_confidence_ppm=baseline.bootstrap_confidence_ppm + 1
        ),
        "live_rolling_window_size": dataclasses.replace(
            baseline, live_rolling_window_size=baseline.live_rolling_window_size + 1
        ),
        "live_slippage_ratio_limit_ppm": dataclasses.replace(
            baseline,
            live_slippage_ratio_limit_ppm=baseline.live_slippage_ratio_limit_ppm + 1,
        ),
        "live_brier_degradation_band_ppm": dataclasses.replace(
            baseline,
            live_brier_degradation_band_ppm=(
                baseline.live_brier_degradation_band_ppm + 1
            ),
        ),
        "observation_window": dataclasses.replace(
            baseline, observation_window="daily_snapshots"
        ),
        "baseline_scheme": dataclasses.replace(
            baseline, baseline_scheme="some_other_baseline_scheme"
        ),
        "clustering_scheme": dataclasses.replace(
            baseline, clustering_scheme="some_other_clustering_scheme"
        ),
        "paper_fill_model_version": dataclasses.replace(
            baseline, paper_fill_model_version="pfm-v2"
        ),
        "metric_windows": dataclasses.replace(
            baseline, metric_windows=mutated_metric_windows
        ),
    }

    for label, variant in variants.items():
        assert variant.plan_hash != baseline.plan_hash, label


# ---------------------------------------------------------------------------
# 4. Construction guards: float/bool int fields, non-str scheme fields,
#    duplicate metric names.
# ---------------------------------------------------------------------------


def test_gate_plan_rejects_float_threshold_with_type_error() -> None:
    """A `float` threshold (`10000.0`, not an `int`) raises `TypeError`.

    `cast` (not `# type: ignore`) supplies the deliberately-invalid literal,
    matching this repo's convention (see `test_correlation_buckets.py`) for
    modeling a value the runtime guard, not the type checker, must catch.
    """
    from windbreak.evaluation.preregistration import GatePlan

    with pytest.raises(TypeError, match="brier_skill_required_ppm"):
        GatePlan(
            metric_windows=(("brier", "latest_before_close"),),
            min_resolved_for_calibration=150,
            promotion_min_resolved=300,
            promotion_min_independent_event_groups=100,
            brier_skill_required_ppm=cast("int", 10_000.0),
            bootstrap_confidence_ppm=950_000,
            observation_window="latest_before_close",
            baseline_scheme="executable_price_at_baseline_snapshot",
            clustering_scheme="event_correlation_group",
            paper_fill_model_version="pfm-v1",
        )


def test_gate_plan_rejects_bool_masquerading_as_int_threshold() -> None:
    """A `bool` threshold (an `int` subclass) raises `TypeError`.

    Per the repo-wide "no bool-as-int" rule (see
    `windbreak.numeric.types._IntUnit` and `FixtureForecast.__post_init__`).
    No `cast` is needed: `bool` is a structural subtype of `int`, so this is
    already statically valid, exactly like production code that receives an
    untrusted `bool` where an `int` is expected.
    """
    from windbreak.evaluation.preregistration import GatePlan

    with pytest.raises(TypeError, match="promotion_min_resolved"):
        GatePlan(
            metric_windows=(("brier", "latest_before_close"),),
            min_resolved_for_calibration=150,
            promotion_min_resolved=True,
            promotion_min_independent_event_groups=100,
            brier_skill_required_ppm=10_000,
            bootstrap_confidence_ppm=950_000,
            observation_window="latest_before_close",
            baseline_scheme="executable_price_at_baseline_snapshot",
            clustering_scheme="event_correlation_group",
            paper_fill_model_version="pfm-v1",
        )


def test_gate_plan_rejects_non_str_baseline_scheme_with_type_error() -> None:
    """A non-`str` `baseline_scheme` raises `TypeError` naming the field."""
    from windbreak.evaluation.preregistration import GatePlan

    with pytest.raises(TypeError, match="baseline_scheme"):
        GatePlan(
            metric_windows=(("brier", "latest_before_close"),),
            min_resolved_for_calibration=150,
            promotion_min_resolved=300,
            promotion_min_independent_event_groups=100,
            brier_skill_required_ppm=10_000,
            bootstrap_confidence_ppm=950_000,
            observation_window="latest_before_close",
            baseline_scheme=cast("str", 123),
            clustering_scheme="event_correlation_group",
            paper_fill_model_version="pfm-v1",
        )


def test_gate_plan_rejects_duplicate_metric_names_with_value_error() -> None:
    """Two `metric_windows` entries naming the same metric raise `ValueError`.

    A duplicate metric name would make the plan's identity ambiguous (which
    window applies?), so it is a construction-time invariant violation, not a
    silently-resolved "last one wins".
    """
    from windbreak.evaluation.preregistration import GatePlan

    with pytest.raises(ValueError, match="brier"):
        GatePlan(
            metric_windows=(
                ("brier", "latest_before_close"),
                ("brier", "first_per_market"),
            ),
            min_resolved_for_calibration=150,
            promotion_min_resolved=300,
            promotion_min_independent_event_groups=100,
            brier_skill_required_ppm=10_000,
            bootstrap_confidence_ppm=950_000,
            observation_window="latest_before_close",
            baseline_scheme="executable_price_at_baseline_snapshot",
            clustering_scheme="event_correlation_group",
            paper_fill_model_version="pfm-v1",
        )


# ---------------------------------------------------------------------------
# 5. register_gate_plan: first registration, idempotent re-registration,
#    change resets the clock (the issue's own example, and the
#    paper_fill_model_version-only ACCEPTANCE #3 variant), fail-closed on a
#    non-monotonic clock.
# ---------------------------------------------------------------------------


def test_register_gate_plan_first_registration_appends_one_event(
    tmp_path: Path,
) -> None:
    """First-ever registration appends exactly one `GatePlanRegistered`.

    The returned registration carries `previous_plan_hash=None` and the
    injected clock's epoch as `paper_clock_start`; the persisted envelope's
    `data` carries the full canonical plan dict plus `plan_hash` and
    `paper_clock_start`; the chain verifies.
    """
    from windbreak.evaluation.preregistration import build_gate_plan, register_gate_plan

    store = _ledger_store(tmp_path)
    try:
        plan = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")

        registration = register_gate_plan(
            plan, store, now=_constant_clock(1_700_000_000)
        )

        records = store.read_all()
        assert len(records) == 1
        last = records[-1]
        assert last.event_type == "GatePlanRegistered"
        assert last.component == "evaluation"

        envelope = json.loads(last.payload_json)
        data = envelope["data"]
        assert data["plan_hash"] == plan.plan_hash
        assert data["paper_clock_start"] == 1_700_000_000
        for key, value in json.loads(plan.canonical_json_str).items():
            assert data[key] == value

        assert registration.plan == plan
        assert registration.plan_hash == plan.plan_hash
        assert registration.previous_plan_hash is None
        assert registration.paper_clock_start == 1_700_000_000
        assert registration.event_type == "GatePlanRegistered"

        store.verify_chain()
    finally:
        store.close()


def test_register_gate_plan_reregistering_identical_plan_is_idempotent(
    tmp_path: Path,
) -> None:
    """Re-registering a byte-identical plan appends no new event.

    The returned registration equals the first one exactly (same
    `paper_clock_start` -- the clock is not reset for a no-op
    re-registration), whether or not the implementation calls `now()` again
    on the no-op path (a constant clock makes both possibilities agree).
    """
    from windbreak.evaluation.preregistration import build_gate_plan, register_gate_plan

    store = _ledger_store(tmp_path)
    try:
        plan = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")
        clock = _constant_clock(1_700_000_000)

        first = register_gate_plan(plan, store, now=clock)
        second = register_gate_plan(plan, store, now=clock)

        assert len(store.read_all()) == 1
        assert second == first
        assert second.paper_clock_start == 1_700_000_000
        assert second.event_type == "GatePlanRegistered"
        store.verify_chain()
    finally:
        store.close()


def test_register_gate_plan_change_resets_paper_clock_and_links_previous_hash(
    tmp_path: Path,
) -> None:
    """ACCEPTANCE (issue example): any threshold change resets the clock.

    Registering a plan that differs from the currently-registered plan by
    `brier_skill_required_ppm + 1` alone appends a `GatePlanChanged` whose
    `previous_plan_hash` links back to the prior plan's hash and whose
    `paper_clock_start` is strictly later than the prior registration's.
    """
    from windbreak.evaluation.preregistration import build_gate_plan, register_gate_plan

    store = _ledger_store(tmp_path)
    try:
        plan_a = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")
        clock = _sequence_clock(1_700_000_000, 1_700_000_500)

        reg_a = register_gate_plan(plan_a, store, now=clock)

        plan_b = dataclasses.replace(
            plan_a, brier_skill_required_ppm=plan_a.brier_skill_required_ppm + 1
        )
        reg_b = register_gate_plan(plan_b, store, now=clock)

        assert reg_b.plan_hash != reg_a.plan_hash
        assert reg_b.paper_clock_start > reg_a.paper_clock_start
        assert store.read_all()[-1].event_type == "GatePlanChanged"
        assert reg_b.previous_plan_hash == reg_a.plan_hash
        store.verify_chain()
    finally:
        store.close()


def test_register_gate_plan_fill_model_version_change_alone_resets_clock(
    tmp_path: Path,
) -> None:
    """ACCEPTANCE #3 (SPEC S17.4): a fill-model-version-only change resets
    the paper clock exactly like a threshold change --
    `paper_fill_model_version` is part of the plan's identity, not
    incidental metadata.
    """
    from windbreak.evaluation.preregistration import build_gate_plan, register_gate_plan

    store = _ledger_store(tmp_path)
    try:
        plan_a = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")
        clock = _sequence_clock(1_700_000_000, 1_700_001_000)

        reg_a = register_gate_plan(plan_a, store, now=clock)

        plan_b = dataclasses.replace(plan_a, paper_fill_model_version="pfm-v2")
        reg_b = register_gate_plan(plan_b, store, now=clock)

        assert reg_b.plan_hash != reg_a.plan_hash
        assert reg_b.paper_clock_start > reg_a.paper_clock_start
        assert store.read_all()[-1].event_type == "GatePlanChanged"
        assert reg_b.previous_plan_hash == reg_a.plan_hash
        store.verify_chain()
    finally:
        store.close()


def test_register_gate_plan_raises_on_non_monotonic_clock_and_appends_nothing(
    tmp_path: Path,
) -> None:
    """Fail-closed: a non-strictly-later clock on a change raises and appends
    no event.

    A changed plan registered with `now()` returning an epoch equal to (not
    strictly greater than) the prior `paper_clock_start` must raise
    `ValueError` -- and must not append a `GatePlanChanged` record, leaving
    the ledger exactly as it was before the failed call.
    """
    from windbreak.evaluation.preregistration import build_gate_plan, register_gate_plan

    store = _ledger_store(tmp_path)
    try:
        plan_a = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")
        register_gate_plan(plan_a, store, now=_constant_clock(1_700_000_000))
        assert len(store.read_all()) == 1

        plan_b = dataclasses.replace(
            plan_a, brier_skill_required_ppm=plan_a.brier_skill_required_ppm + 1
        )

        with pytest.raises(ValueError):
            register_gate_plan(plan_b, store, now=_constant_clock(1_700_000_000))

        assert len(store.read_all()) == 1
        store.verify_chain()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 6. latest_gate_plan_registration: read-model round trip; GatePlan.
#    from_canonical rejects an unknown key.
# ---------------------------------------------------------------------------


def test_latest_gate_plan_registration_round_trips_through_the_ledger(
    tmp_path: Path,
) -> None:
    """The read model reconstructs the most recently registered plan.

    Empty store -> `None`. After a first registration, the reconstructed
    plan equals the original (both by `==` and by `plan_hash`) and carries
    that registration's `paper_clock_start`. After a change, the read model
    reflects the *changed* plan and its own, later `paper_clock_start`.
    """
    from windbreak.evaluation.preregistration import (
        build_gate_plan,
        latest_gate_plan_registration,
        register_gate_plan,
    )

    store = _ledger_store(tmp_path)
    try:
        assert latest_gate_plan_registration(store) is None

        plan_a = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")
        reg_a = register_gate_plan(plan_a, store, now=_sequence_clock(1_700_000_000))

        latest_a = latest_gate_plan_registration(store)
        assert latest_a is not None
        assert latest_a.plan == plan_a
        assert latest_a.plan.plan_hash == plan_a.plan_hash
        assert latest_a.paper_clock_start == reg_a.paper_clock_start

        plan_b = dataclasses.replace(
            plan_a, brier_skill_required_ppm=plan_a.brier_skill_required_ppm + 1
        )
        reg_b = register_gate_plan(plan_b, store, now=_sequence_clock(1_700_000_100))

        latest_b = latest_gate_plan_registration(store)
        assert latest_b is not None
        assert latest_b.plan.plan_hash == plan_b.plan_hash
        assert latest_b.plan.plan_hash != plan_a.plan_hash
        assert latest_b.paper_clock_start == reg_b.paper_clock_start
        assert latest_b.event_type == "GatePlanChanged"
        assert latest_b.previous_plan_hash == plan_a.plan_hash
    finally:
        store.close()


def test_latest_gate_plan_registration_fails_closed_on_hash_mismatch(
    tmp_path: Path,
) -> None:
    """A ledgered plan whose stored `plan_hash` disagrees with its `plan_dict`
    is rejected on read.

    The read model recomputes the content hash from the reconstructed plan and
    must fail closed (raise `ValueError`) rather than trust a stored hash that
    does not match its own plan -- the tamper-evidence guarantee the anti-Goodhart
    control (SPEC §13.6) depends on. A `GatePlanRegistered` is appended directly
    with a deliberately wrong `plan_hash` to model a corrupted/tampered payload.
    """
    from windbreak.evaluation.preregistration import (
        GatePlanRegistered,
        build_gate_plan,
        latest_gate_plan_registration,
    )

    store = _ledger_store(tmp_path)
    try:
        plan = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")
        store.append(
            GatePlanRegistered(
                component="evaluation",
                plan_dict=plan.canonical_dict(),
                plan_hash="0" * 64,
                paper_clock_start=1_700_000_000,
            )
        )

        with pytest.raises(ValueError, match="hash mismatch"):
            latest_gate_plan_registration(store)
    finally:
        store.close()


def test_gate_plan_from_canonical_rejects_unknown_key_with_value_error() -> None:
    """An unrecognized key in the mapping raises `ValueError` (fatal, house
    config style -- see `windbreak.config.loader`'s unknown-key handling).
    """
    from windbreak.evaluation.preregistration import GatePlan

    mapping: dict[str, object] = {
        "metric_windows": [["brier", "latest_before_close"]],
        "min_resolved_for_calibration": 150,
        "promotion_min_resolved": 300,
        "promotion_min_independent_event_groups": 100,
        "brier_skill_required_ppm": 10_000,
        "bootstrap_confidence_ppm": 950_000,
        "observation_window": "latest_before_close",
        "baseline_scheme": "executable_price_at_baseline_snapshot",
        "clustering_scheme": "event_correlation_group",
        "paper_fill_model_version": "pfm-v1",
        "unknown_key": 1,
    }

    with pytest.raises(ValueError, match="unknown_key"):
        GatePlan.from_canonical(mapping)


def test_register_gate_plan_persists_byte_identical_canonical_plan_dict(
    tmp_path: Path,
) -> None:
    """The ledgered plan dict is byte-identical, re-serialized, to
    `plan.canonical_json_str`.

    Strips the two registration-only keys (`plan_hash`, `paper_clock_start`)
    off the persisted envelope's `data`, re-serializes the remainder through
    the ledger's own `canonical_json`, and asserts it equals
    `plan.canonical_json_str` exactly -- the "ledgered canonicality"
    guarantee that the persisted plan is not a lossy or reordered copy.
    """
    from windbreak.evaluation.preregistration import build_gate_plan, register_gate_plan

    store = _ledger_store(tmp_path)
    try:
        plan = build_gate_plan(EvaluationConfig(), paper_fill_model_version="pfm-v1")
        register_gate_plan(plan, store, now=_constant_clock(1_700_000_000))

        record = store.read_all()[-1]
        envelope = json.loads(record.payload_json)
        persisted_plan_fields = {
            key: value
            for key, value in envelope["data"].items()
            if key not in {"plan_hash", "paper_clock_start"}
        }

        assert canonical_json(persisted_plan_fields) == plan.canonical_json_str
        store.verify_chain()
    finally:
        store.close()
