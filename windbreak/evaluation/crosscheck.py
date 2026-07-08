"""Dual-path gate crosscheck: SQL vs Python, agreeing or alerting (issue #55).

Calling :func:`crosscheck_gates` *is* running both independent gate-metric
implementations on every gate evaluation (T12): the Python reference path
(:func:`~windbreak.evaluation.registry.registered_metrics`, the #51/#53 machinery)
and the SQL path (:class:`~windbreak.evaluation.sql_gates.SqlGateComputer`) both
score the same temporally-admitted inputs, and every metric's two values are
compared. The crosscheck never blends the two: it carries both raw values
verbatim so an auditor sees exactly what each path produced.

Agreement is deliberately strict. Two ``int``s agree iff they differ by at most
:data:`INTEGER_ROUNDING_TOLERANCE` (a small allowance for a residual rounding
skew between the two arithmetics -- a safety margin, not a licence to drift); two
sentinels agree iff they are the *same* sentinel by identity; an ``int`` against
a sentinel is always a mismatch, and either path *failing* -- a SQL query that
raised (:data:`~windbreak.evaluation.sql_gates.SQL_QUERY_FAILED`) or a Python
reference metric that raised (:data:`PYTHON_COMPUTE_FAILED`) -- is always a
mismatch (the two failure sentinels are distinct, so even a both-paths-failed
metric is flagged). On any disagreement the crosscheck appends exactly one
:class:`GateComputationMismatch` to the ledger, fires one
``AlertSeverity.CRITICAL`` alert naming the disagreeing metrics, and returns
:data:`CrosscheckStatus.MISMATCH`; on full agreement it appends nothing, alerts
nothing, and returns :data:`CrosscheckStatus.MATCH`. It never raises: a failure
on *either* path -- a malformed SQL query or a reference metric that raises on a
degenerate-but-admitted input (the exact pathology a safety crosscheck exists to
flag loudly) -- is captured as a failure sentinel and reported, never swallowed
and never propagated out.

Event naming follows the house convention (and
:mod:`windbreak.evaluation.preregistration`): :class:`GateComputationMismatch`
derives its ``event_type`` from the concrete class name and is deliberately not
yet listed in the ledger's central ``EVENT_TYPES`` map (a follow-up issue).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from windbreak.alerts.registry import AlertSeverity
from windbreak.evaluation.cohorts import UndefinedBrier
from windbreak.evaluation.registry import (
    NotImplementedSentinel,
    gate_evaluation_inputs,
    registered_metrics,
)
from windbreak.evaluation.sql_gates import SqlGateComputer, SqlGateFailure
from windbreak.ledger.events import Event

if TYPE_CHECKING:
    from collections.abc import Mapping

    from windbreak.evaluation.preregistration import GatePlan
    from windbreak.evaluation.registry import EvaluationInputs, MetricSpec, MetricValue
    from windbreak.evaluation.sql_gates import SqlMetricValue
    from windbreak.ledger.store import LedgerStore

#: The inclusive absolute tolerance, in ppm units, within which the SQL and
#: Python paths' ``int`` values are treated as agreeing.
INTEGER_ROUNDING_TOLERANCE: int = 1

#: Payload schema version stamped on this module's events. Replicated locally
#: (rather than imported from the private copy in :mod:`windbreak.ledger.events`)
#: so a payload-shape change here can be versioned independently.
_SCHEMA_VERSION = 1

#: The default producing component recorded on any appended mismatch event.
_DEFAULT_COMPONENT = "evaluation"


class PythonComputeFailure(enum.Enum):
    """Sentinel marking a Python reference metric that raised, not returned.

    The symmetric twin of
    :class:`~windbreak.evaluation.sql_gates.SqlGateFailure`: a reference metric
    that raises on a degenerate-but-admitted input -- zero forecast variance
    (all-equal probabilities), an empty resolved set, a zero baseline-error sum,
    or a certain-and-wrong forecast -- is carried into a
    :class:`MetricComparison` as a loud, never-silently-swallowed value rather
    than crashing the crosscheck. It equals no ``int`` and no other sentinel
    (including :data:`~windbreak.evaluation.sql_gates.SQL_QUERY_FAILED`), so it
    always disagrees with a real SQL value, and two paths that *both* fail
    disagree by sentinel identity too -- a double failure is still flagged.
    """

    COMPUTE_FAILED = "COMPUTE_FAILED"


#: The sentinel the Python reference path yields for a metric whose ``compute``
#: raised on a degenerate-but-admitted input the reference cannot score.
PYTHON_COMPUTE_FAILED = PythonComputeFailure.COMPUTE_FAILED


class CrosscheckStatus(enum.Enum):
    """Whether the two gate paths fully agreed on a crosscheck run."""

    MATCH = "match"
    MISMATCH = "mismatch"


class AlertHook(Protocol):
    """A sink the crosscheck fires a severity/message pair into on a mismatch."""

    def __call__(self, severity: AlertSeverity, message: str) -> None:
        """Emit one alert.

        Args:
            severity: The alert's severity.
            message: The alert's human-readable message.
        """


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """One metric's two independently-computed values, side by side.

    Attributes:
        name: The registered metric name.
        window: The observation-window label the metric is scored under.
        python_value: The Python reference path's value, verbatim, or
            :data:`PYTHON_COMPUTE_FAILED` when the reference metric raised.
        sql_value: The SQL path's value, verbatim (never blended with
            ``python_value``).
        within_tolerance: Whether the two values agree under the crosscheck's
            tolerance / sentinel-identity rules.
    """

    name: str
    window: str
    python_value: MetricValue | PythonComputeFailure
    sql_value: SqlMetricValue
    within_tolerance: bool


@dataclass(frozen=True, slots=True)
class CrosscheckResult:
    """The outcome of one dual-path gate crosscheck.

    Attributes:
        status: Whether every metric agreed.
        comparisons: One :class:`MetricComparison` per registered metric.
        plan_hash: The content hash of the gate plan the run was scored under.
    """

    status: CrosscheckStatus
    comparisons: tuple[MetricComparison, ...]
    plan_hash: str


def _derive_typed_event(event: Event, payload: dict[str, object]) -> None:
    """Populate the derived :class:`~windbreak.ledger.events.Event` fields.

    Replicates the ledger module's private derivation locally (that module is out
    of this issue's scope): sets ``event_type`` to the concrete class name,
    ``payload_schema_version`` to this module's schema version, and ``payload`` to
    the assembled dict, via ``object.__setattr__`` because the events are frozen.

    Args:
        event: The freshly constructed typed event to populate.
        payload: The type-specific payload assembled by the subclass.
    """
    object.__setattr__(event, "event_type", type(event).__name__)
    object.__setattr__(event, "payload_schema_version", _SCHEMA_VERSION)
    object.__setattr__(event, "payload", payload)


@dataclass(frozen=True)
class GateComputationMismatch(Event):
    """Records that the SQL and Python gate paths disagreed on a crosscheck.

    Attributes:
        plan_hash: The gate plan's content hash the run was scored under.
        tolerance: The integer tolerance the comparison used.
        mismatches: One entry per disagreeing metric, each shaped
            ``{"name", "window", "python_value", "sql_value"}`` with any sentinel
            rendered by its ``.name``.
    """

    plan_hash: str
    tolerance: int
    mismatches: list[dict[str, object]]
    event_type: str = field(init=False)
    payload_schema_version: int = field(init=False)
    payload: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        """Assemble the payload and derive the base ``Event`` fields."""
        payload: dict[str, object] = {
            "plan_hash": self.plan_hash,
            "tolerance": self.tolerance,
            "mismatches": self.mismatches,
        }
        _derive_typed_event(self, payload)


def _render_value(
    value: MetricValue | SqlMetricValue | PythonComputeFailure,
) -> object:
    """Render a metric value for a JSON-safe mismatch payload entry.

    Args:
        value: The computed value from either path.

    Returns:
        The sentinel's ``.name`` for any sentinel (mirroring
        :func:`windbreak.evaluation.report._format_value`), else the ``int``
        itself.
    """
    if isinstance(
        value,
        (NotImplementedSentinel, UndefinedBrier, SqlGateFailure, PythonComputeFailure),
    ):
        return value.name
    return value


def _within_tolerance(
    python_value: MetricValue | PythonComputeFailure,
    sql_value: SqlMetricValue,
    tolerance: int,
) -> bool:
    """Report whether the two paths agree on one metric.

    Args:
        python_value: The Python reference value.
        sql_value: The SQL value.
        tolerance: The inclusive absolute tolerance for two ``int`` values.

    Returns:
        ``True`` iff both are ``int`` and within ``tolerance``, or both are the
        same sentinel by identity; ``False`` otherwise (including an ``int``
        against a sentinel, a ``SQL_QUERY_FAILED``, and a
        ``PYTHON_COMPUTE_FAILED`` -- which, being a distinct sentinel from
        ``SQL_QUERY_FAILED``, mismatches even a failed SQL path).
    """
    if (
        isinstance(python_value, int)
        and not isinstance(python_value, bool)
        and isinstance(sql_value, int)
        and not isinstance(sql_value, bool)
    ):
        return abs(sql_value - python_value) <= tolerance
    return python_value is sql_value


def _mismatch_entry(comparison: MetricComparison) -> dict[str, object]:
    """Project one disagreeing comparison into a JSON-safe payload entry.

    Args:
        comparison: The mismatched comparison to project.

    Returns:
        The ``{"name", "window", "python_value", "sql_value"}`` entry, with
        sentinels rendered by name.
    """
    return {
        "name": comparison.name,
        "window": comparison.window,
        "python_value": _render_value(comparison.python_value),
        "sql_value": _render_value(comparison.sql_value),
    }


def _python_value(
    spec: MetricSpec, inputs: EvaluationInputs
) -> MetricValue | PythonComputeFailure:
    """Compute one metric's Python reference value, degrading a raise to a sentinel.

    The symmetric twin of the SQL side's per-query guard (see
    :meth:`~windbreak.evaluation.sql_gates.SqlGateComputer._value_for`): the
    reference metrics raise :class:`ValueError` on degenerate-but-admitted
    inputs (zero forecast variance, an empty resolved set, a zero baseline-error
    sum, or a certain-and-wrong forecast), and that raise is caught here and
    turned into :data:`PYTHON_COMPUTE_FAILED` so a pathological input becomes a
    loud, ledgered mismatch instead of crashing :func:`crosscheck_gates`. The
    catch is narrow -- :class:`ValueError` only (the sole type the reference
    metrics raise) -- so an unexpected fault still surfaces.

    Args:
        spec: The registered metric spec whose ``compute`` is invoked.
        inputs: The raw evaluation inputs; the spec temporally gates them itself.

    Returns:
        The reference metric's value, or :data:`PYTHON_COMPUTE_FAILED` when its
        ``compute`` raised :class:`ValueError`.
    """
    try:
        return spec.compute(inputs)
    except ValueError:
        return PYTHON_COMPUTE_FAILED


def _build_comparisons(
    inputs: EvaluationInputs,
    plan: GatePlan,
    specs: Mapping[str, MetricSpec],
    sql_values: Mapping[str, SqlMetricValue],
    tolerance: int,
) -> tuple[MetricComparison, ...]:
    """Compute both paths' value for every metric and compare them.

    Args:
        inputs: The raw evaluation inputs; each Python spec temporally gates them
            itself, so the two paths score the same admitted set.
        plan: The gate plan naming the metrics and their windows.
        specs: The freshly-resolved Python reference metric registry.
        sql_values: The SQL path's already-computed values, keyed by metric name.
        tolerance: The inclusive ``int`` agreement tolerance.

    Returns:
        One :class:`MetricComparison` per ``plan.metric_windows`` metric.
    """
    comparisons: list[MetricComparison] = []
    for name, window in plan.metric_windows:
        python_value = _python_value(specs[name], inputs)
        sql_value = sql_values[name]
        comparisons.append(
            MetricComparison(
                name=name,
                window=window,
                python_value=python_value,
                sql_value=sql_value,
                within_tolerance=_within_tolerance(python_value, sql_value, tolerance),
            )
        )
    return tuple(comparisons)


def _emit_mismatch(
    mismatches: tuple[MetricComparison, ...],
    plan: GatePlan,
    store: LedgerStore,
    alert: AlertHook,
    tolerance: int,
    component: str,
) -> None:
    """Ledger one mismatch event and fire one critical alert.

    Args:
        mismatches: The disagreeing comparisons (non-empty).
        plan: The gate plan the run was scored under.
        store: The ledger to append the mismatch event to.
        alert: The sink the critical alert is fired into.
        tolerance: The tolerance the comparison used, stamped on the event.
        component: The producing component recorded on the event.
    """
    store.append(
        GateComputationMismatch(
            component=component,
            plan_hash=plan.plan_hash,
            tolerance=tolerance,
            mismatches=[_mismatch_entry(comparison) for comparison in mismatches],
        )
    )
    disagreeing = ", ".join(comparison.name for comparison in mismatches)
    alert(
        AlertSeverity.CRITICAL,
        f"gate computation mismatch (tolerance={tolerance}) on: {disagreeing}",
    )


def crosscheck_gates(
    inputs: EvaluationInputs,
    *,
    plan: GatePlan,
    store: LedgerStore,
    alert: AlertHook,
    sql_path: SqlGateComputer | None = None,
    tolerance: int = INTEGER_ROUNDING_TOLERANCE,
    component: str = _DEFAULT_COMPONENT,
) -> CrosscheckResult:
    """Run the Python and SQL gate paths and compare every metric.

    Args:
        inputs: The raw evaluation inputs to score; both paths gate them for
            temporal integrity so they score the identical admitted set.
        plan: The pre-registered gate plan naming the metrics and windows.
        store: The append-only ledger a mismatch event is written to.
        alert: The sink a single critical alert is fired into on a mismatch.
        sql_path: The SQL computer to run; a default
            :class:`~windbreak.evaluation.sql_gates.SqlGateComputer` when ``None``.
        tolerance: The inclusive absolute tolerance for two ``int`` values.
        component: The producing component recorded on any mismatch event.

    Returns:
        A :class:`CrosscheckResult` carrying the per-metric comparisons and the
        overall status; ``MISMATCH`` (with one ledgered event and one alert) on
        any disagreement, else ``MATCH``.
    """
    computer = sql_path if sql_path is not None else SqlGateComputer()
    admitted, _ = gate_evaluation_inputs(inputs)
    sql_values = computer.compute(admitted, plan)
    specs = registered_metrics()
    comparisons = _build_comparisons(inputs, plan, specs, sql_values, tolerance)
    mismatches = tuple(
        comparison for comparison in comparisons if not comparison.within_tolerance
    )
    if not mismatches:
        return CrosscheckResult(
            status=CrosscheckStatus.MATCH,
            comparisons=comparisons,
            plan_hash=plan.plan_hash,
        )
    _emit_mismatch(mismatches, plan, store, alert, tolerance, component)
    return CrosscheckResult(
        status=CrosscheckStatus.MISMATCH,
        comparisons=comparisons,
        plan_hash=plan.plan_hash,
    )
