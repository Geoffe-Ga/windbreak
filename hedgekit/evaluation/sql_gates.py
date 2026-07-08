"""Independent SQL reproduction of the evaluation gate metrics (issue #55).

This module is the *second, independent* implementation of the SPEC §13.5
forecast-track and §13.6 selection-track gate metrics: it reproduces every
value that :mod:`hedgekit.evaluation.metrics` /
:mod:`hedgekit.evaluation.cohorts` produce, but from scratch, in SQLite, sharing
*no* computation with the Python reference path.
:mod:`hedgekit.evaluation.crosscheck` runs both paths on every gate evaluation
and alerts loudly when they disagree.

Independence is structural, not merely conventional. This module must NOT import
:mod:`hedgekit.evaluation.metrics`, :mod:`hedgekit.evaluation.bootstrap`, or
:mod:`hedgekit.evaluation.power`, and must NOT use
:func:`hedgekit.numeric.rounding.divide`: every division here is a local integer
expression (:func:`_local_floor_div` / :func:`_local_ceil_div`), and the
fixed-point base-2 logarithm behind the log score is re-derived locally in
:func:`_surprisal_micro_nats` rather than imported. A crosscheck test proves the
independence by corrupting ``metrics.mean_brier`` to garbage and asserting this
path's value is unchanged.

Two numeric hazards drive the architecture:

- **int64 overflow.** SQLite integers are 64-bit; a wide least-squares
  combination such as ``sum_o * var_num - cov_num * sum_p`` can exceed ``2**63``.
  So the wide multiply/divide happens in Python -- inside registered SQLite UDFs
  backed by arbitrary-precision Python ints (:func:`_hk_slope`,
  :func:`_hk_intercept`) -- and SQL computes only the component sums. Those sums
  are bounded *for a realistic resolved-set size*, not unconditionally: a
  squared ppm term is ~1e12, so ``SUM((p-o)^2)`` stays in int64 only up to
  roughly nine million resolved forecasts (``2**63 / 1e12``), an N this harness
  never approaches; it is the far wider products (which would overflow at much
  smaller N) that are correctly deferred to the Python UDFs.
- **rounding skew.** SQLite integer division truncates toward zero; the Python
  reference floors (``UNDERSTATE_EQUITY``) or ceilings (``OVERSTATE_COST``), which
  differ on negatives. Every final division is therefore done in a Python UDF
  with the same conservative direction as the reference, so the two paths agree
  exactly rather than merely within the crosscheck's ``+/-1`` safety tolerance.

Unresolved markets are excluded exactly as the Python path excludes them: every
query is an ``INNER JOIN`` of ``forecasts`` against ``resolutions``, so a
forecast whose market never resolved contributes to no metric.
"""

from __future__ import annotations

import enum
import sqlite3
from typing import TYPE_CHECKING

from hedgekit.evaluation.cohorts import UNDEFINED, UndefinedBrier
from hedgekit.evaluation.registry import NOT_IMPLEMENTED, NotImplementedSentinel
from hedgekit.evaluation.resolution import ResolutionOutcome

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hedgekit.evaluation.preregistration import GatePlan
    from hedgekit.evaluation.registry import EvaluationInputs, FixtureForecast

#: One whole probability expressed in ppm (1.0 == 1_000_000 ppm), and the
#: ppm-scaling factor lifting a mean/ratio back into ppm space. Kept local rather
#: than imported from :mod:`hedgekit.evaluation.metrics` so the two paths share
#: no code (issue #55 independence rule).
_PPM_SCALE = 1_000_000
#: A ``YES`` resolution as a ppm outcome (certainty); ``NO`` is ``0``.
_OUTCOME_YES_PPM = 1_000_000
#: Exact ppm-per-pip factor: one pip is 100 ppm of probability.
_BASELINE_PPM_PER_PIP = 100

#: ``floor(ln(2) * 1e18)`` -- natural log of 2 scaled by 1e18, re-derived here
#: for the integer log score (never imported from the metrics module).
_LN2_SCALED = 693_147_180_559_945_309
#: The decimal scale (1e18) :data:`_LN2_SCALED` is expressed in.
_LN2_DECIMAL_SCALE = 1_000_000_000_000_000_000
#: Fractional bits extracted for the fixed-point ``log2`` mantissa; 64 bits keeps
#: the truncation error far below any ppm-scaled result.
_LOG2_FRACTION_BITS = 64
#: Divisor lifting the log-score result from ``log2 * 1e18``-scaled nats to
#: micro-nats: ``2**_LOG2_FRACTION_BITS * (1e18 / 1e6)``.
_LOG_SCORE_DENOMINATOR = (1 << _LOG2_FRACTION_BITS) * (_LN2_DECIMAL_SCALE // _PPM_SCALE)

#: The registry name of the execution-track stub, reproduced as
#: :data:`~hedgekit.evaluation.registry.NOT_IMPLEMENTED` on both paths.
_FILL_VS_MODEL_SLIPPAGE = "fill_vs_model_slippage"


class SqlGateFailure(enum.Enum):
    """Sentinel marking a gate query that raised rather than returning a value.

    A dedicated enum (rather than ``None`` or an exception escaping the
    computer) lets a raised SQL query be carried into a
    :class:`~hedgekit.evaluation.crosscheck.MetricComparison` as a loud,
    never-silently-swallowed mismatch: it equals no ``int`` and no other
    sentinel, so it always disagrees with the Python reference.
    """

    QUERY_FAILED = "QUERY_FAILED"


#: The sentinel a :class:`SqlGateComputer` returns for a metric whose query
#: raised (e.g. malformed SQL or a divide-by-zero surfaced from a UDF).
SQL_QUERY_FAILED = SqlGateFailure.QUERY_FAILED

#: A value produced by the SQL gate path: a ppm-scaled ``int``, the
#: :data:`~hedgekit.evaluation.registry.NOT_IMPLEMENTED` stub sentinel, the
#: :data:`~hedgekit.evaluation.cohorts.UNDEFINED` empty-cohort sentinel, or
#: :data:`SQL_QUERY_FAILED` when the metric's query raised.
SqlMetricValue = int | NotImplementedSentinel | UndefinedBrier | SqlGateFailure


def _local_floor_div(numerator: int, denominator: int) -> int:
    """Divide two ints rounding toward negative infinity (floor).

    The local, sanctioned-``divide``-free floor used on every equity-side value
    (mirroring ``RoundingDirection.UNDERSTATE_EQUITY``), so this module shares no
    code with :mod:`hedgekit.numeric.rounding` (issue #55 independence rule).

    Args:
        numerator: The dividend.
        denominator: The divisor (non-zero).

    Returns:
        ``numerator // denominator``, floored (sign-safe for any operands).
    """
    return numerator // denominator


def _local_ceil_div(numerator: int, denominator: int) -> int:
    """Divide two ints rounding toward positive infinity (ceiling).

    The local, sanctioned-``divide``-free ceiling used on every cost-side value
    (mirroring ``RoundingDirection.OVERSTATE_COST``).

    Args:
        numerator: The dividend.
        denominator: The divisor (non-zero).

    Returns:
        ``-(-numerator // denominator)``, ceiled (sign-safe for any operands).
    """
    return -(-numerator // denominator)


def _normalise_ratio(numerator: int, denominator: int) -> tuple[int, int, int]:
    """Halve a ``>= 1`` ratio into ``[1, 2)``, counting the integer log2 part.

    Args:
        numerator: The ratio numerator (``>= denominator``).
        denominator: The ratio denominator (positive).

    Returns:
        A ``(numerator, denominator, integer_part)`` triple whose (mutated)
        denominator scales the ratio into ``[1, 2)`` and whose ``integer_part``
        is ``floor(log2(numerator / denominator))``.
    """
    integer_part = 0
    while numerator >= 2 * denominator:
        denominator *= 2
        integer_part += 1
    return numerator, denominator, integer_part


def _log2_fraction_bits(mantissa: int) -> int:
    """Extract the fractional ``log2`` bits of a ``[1, 2)`` fixed-point mantissa.

    Args:
        mantissa: ``m * 2**_LOG2_FRACTION_BITS`` for some ``m`` in ``[1, 2)``.

    Returns:
        The fractional part of ``log2(m)`` scaled by ``2**_LOG2_FRACTION_BITS``,
        built one bit at a time by repeated fixed-point squaring.
    """
    fraction = 0
    for _ in range(_LOG2_FRACTION_BITS):
        mantissa = (mantissa * mantissa) >> _LOG2_FRACTION_BITS
        fraction <<= 1
        if mantissa >= (2 << _LOG2_FRACTION_BITS):
            mantissa >>= 1
            fraction |= 1
    return fraction


def _log2_reciprocal_fixed(arg_ppm: int) -> int:
    """Return ``log2(PPM_SCALE / arg_ppm) * 2**_LOG2_FRACTION_BITS`` as an int.

    Args:
        arg_ppm: A probability in ppm, strictly in ``(0, PPM_SCALE)``.

    Returns:
        The base-2 logarithm of the reciprocal ratio, scaled by
        ``2**_LOG2_FRACTION_BITS`` and truncated to an integer.
    """
    numerator, denominator, integer_part = _normalise_ratio(_PPM_SCALE, arg_ppm)
    mantissa = (numerator << _LOG2_FRACTION_BITS) // denominator
    fraction = _log2_fraction_bits(mantissa)
    return (integer_part << _LOG2_FRACTION_BITS) + fraction


def _surprisal_micro_nats(arg_ppm: int) -> int:
    """Return the surprisal ``-ln(arg_ppm / PPM_SCALE)`` in micro-nats (>= 0).

    A local, from-scratch re-derivation of the metrics module's log-score kernel
    (issue #55 independence rule): the reciprocal ratio is reduced to ``[1, 2)``,
    its fixed-point ``log2`` is extracted, multiplied by ``ln 2``, and reduced to
    micro-nats rounded up so a penalty is never understated.

    Args:
        arg_ppm: The probability of the *observed* outcome, in ppm.

    Returns:
        The surprisal in micro-nats; ``0`` when the observed outcome was forecast
        with certainty (``arg_ppm == PPM_SCALE``).

    Raises:
        ValueError: If ``arg_ppm`` is ``0`` -- a certain-and-wrong forecast has an
            infinite log penalty and cannot be scored.
    """
    if arg_ppm == 0:
        raise ValueError("log-score probability is 0 (certain-wrong); -ln(0) diverges")
    if arg_ppm == _PPM_SCALE:
        return 0
    log2_fixed = _log2_reciprocal_fixed(arg_ppm)
    return _local_ceil_div(log2_fixed * _LN2_SCALED, _LOG_SCORE_DENOMINATOR)


def _hk_ceil_div(numerator: int, denominator: int) -> int:
    """SQLite UDF: ceil-divide two component-sum ints (cost-side rounding).

    Args:
        numerator: The dividend supplied by a SQL aggregate.
        denominator: The divisor supplied by a SQL aggregate (non-zero).

    Returns:
        ``ceil(numerator / denominator)``.
    """
    return _local_ceil_div(numerator, denominator)


def _hk_surprisal(probability_ppm: int, outcome_ppm: int) -> int:
    """SQLite UDF: one forecast's log-score surprisal, in micro-nats.

    Args:
        probability_ppm: The forecast's probability, in ppm.
        outcome_ppm: The resolved outcome as ppm certainty (``0`` or
            :data:`_OUTCOME_YES_PPM`).

    Returns:
        The surprisal of the observed outcome, in micro-nats.
    """
    if outcome_ppm == _OUTCOME_YES_PPM:
        observed_argument = probability_ppm
    else:
        observed_argument = _PPM_SCALE - probability_ppm
    return _surprisal_micro_nats(observed_argument)


def _hk_skill(forecast_sum: int, baseline_sum: int) -> int:
    """SQLite UDF: Brier skill vs baseline, in ppm, from the two term sums.

    Args:
        forecast_sum: Sum of forecast Brier terms, in ppm^2.
        baseline_sum: Sum of baseline Brier terms, in ppm^2.

    Returns:
        ``floor((baseline_sum - forecast_sum) * PPM_SCALE / baseline_sum)``.

    Raises:
        ValueError: If ``baseline_sum`` is zero (skill undefined).
    """
    if baseline_sum == 0:
        raise ValueError("baseline Brier-term sum is zero; skill is undefined")
    return _local_floor_div((baseline_sum - forecast_sum) * _PPM_SCALE, baseline_sum)


def _variance_numerator(count: int, sum_p: int, sum_pp: int) -> int:
    """Return ``n * sum(p^2) - sum(p)^2`` (``n^2`` times the forecast variance).

    Args:
        count: Number of resolved forecasts, ``n``.
        sum_p: ``sum(p)``, in ppm.
        sum_pp: ``sum(p^2)``, in ppm^2.

    Returns:
        The variance numerator.

    Raises:
        ValueError: If the variance numerator is zero (every probability equal),
            leaving the OLS fit undefined.
    """
    variance_numerator = count * sum_pp - sum_p * sum_p
    if variance_numerator == 0:
        raise ValueError("forecast variance is zero; calibration fit is undefined")
    return variance_numerator


def _hk_slope(count: int, sum_p: int, sum_o: int, sum_pp: int, sum_po: int) -> int:
    """SQLite UDF: OLS calibration slope, in ppm, from the five component sums.

    Args:
        count: Number of resolved forecasts, ``n``.
        sum_p: ``sum(p)``, in ppm.
        sum_o: ``sum(o)``, in ppm.
        sum_pp: ``sum(p^2)``, in ppm^2.
        sum_po: ``sum(p * o)``, in ppm^2.

    Returns:
        ``floor(covariance_numerator * PPM_SCALE / variance_numerator)``.

    Raises:
        ValueError: If the forecast variance is zero.
    """
    variance_numerator = _variance_numerator(count, sum_p, sum_pp)
    covariance_numerator = count * sum_po - sum_p * sum_o
    return _local_floor_div(covariance_numerator * _PPM_SCALE, variance_numerator)


def _hk_intercept(count: int, sum_p: int, sum_o: int, sum_pp: int, sum_po: int) -> int:
    """SQLite UDF: OLS calibration intercept, in ppm, from the component sums.

    The wide multiply ``sum_o * var_num - cov_num * sum_p`` exceeds int64 for
    realistic inputs, so it is done here in Python's arbitrary precision rather
    than in SQL.

    Args:
        count: Number of resolved forecasts, ``n``.
        sum_p: ``sum(p)``, in ppm.
        sum_o: ``sum(o)``, in ppm.
        sum_pp: ``sum(p^2)``, in ppm^2.
        sum_po: ``sum(p * o)``, in ppm^2.

    Returns:
        ``floor(numerator / (n * variance_numerator))`` where ``numerator`` is
        ``sum_o * var_num - cov_num * sum_p``.

    Raises:
        ValueError: If the forecast variance is zero.
    """
    variance_numerator = _variance_numerator(count, sum_p, sum_pp)
    covariance_numerator = count * sum_po - sum_p * sum_o
    numerator = sum_o * variance_numerator - covariance_numerator * sum_p
    return _local_floor_div(numerator, count * variance_numerator)


def _hk_sharpness(count: int, sum_p: int, sum_pp: int) -> int:
    """SQLite UDF: sharpness (forecast-probability variance), in ppm.

    Args:
        count: Number of resolved forecasts, ``n``.
        sum_p: ``sum(p)``, in ppm.
        sum_pp: ``sum(p^2)``, in ppm^2.

    Returns:
        ``floor(variance_numerator / (n^2 * PPM_SCALE))``.

    Raises:
        ValueError: If the forecast variance is zero.
    """
    variance_numerator = _variance_numerator(count, sum_p, sum_pp)
    return _local_floor_div(variance_numerator, count * count * _PPM_SCALE)


def _hk_delta(
    skipped_sum: int | None,
    skipped_count: int,
    traded_sum: int | None,
    traded_count: int,
) -> int | None:
    """SQLite UDF: ``mean_brier(SKIPPED) - mean_brier(TRADED)``, in ppm.

    Args:
        skipped_sum: Sum of the SKIPPED cohort's Brier terms, in ppm^2 (``None``
            for an empty cohort).
        skipped_count: Number of resolved SKIPPED forecasts.
        traded_sum: Sum of the TRADED cohort's Brier terms, in ppm^2 (``None``
            for an empty cohort).
        traded_count: Number of resolved TRADED forecasts.

    Returns:
        The signed delta in ppm, or ``None`` (rendered as the ``UNDEFINED``
        sentinel) when either cohort has no resolved records -- mirroring the
        Python path's ``EmptyCohortError`` degradation.
    """
    if skipped_count == 0 or traded_count == 0:
        return None
    skipped_mean = _local_ceil_div(skipped_sum or 0, skipped_count * _PPM_SCALE)
    traded_mean = _local_ceil_div(traded_sum or 0, traded_count * _PPM_SCALE)
    return skipped_mean - traded_mean


def _register_udfs(conn: sqlite3.Connection) -> None:
    """Register every gate-metric UDF on a fresh connection.

    Each function is deterministic (a pure function of its arguments), so SQLite
    may cache and reorder calls freely.

    Args:
        conn: The in-memory connection to register the functions on.
    """
    conn.create_function("hk_ceil_div", 2, _hk_ceil_div, deterministic=True)
    conn.create_function("hk_surprisal", 2, _hk_surprisal, deterministic=True)
    conn.create_function("hk_skill", 2, _hk_skill, deterministic=True)
    conn.create_function("hk_slope", 5, _hk_slope, deterministic=True)
    conn.create_function("hk_intercept", 5, _hk_intercept, deterministic=True)
    conn.create_function("hk_sharpness", 3, _hk_sharpness, deterministic=True)
    conn.create_function("hk_delta", 4, _hk_delta, deterministic=True)


#: DDL for the projected ``forecasts`` table (one row per admitted forecast).
_CREATE_FORECASTS_SQL = (
    "CREATE TABLE forecasts ("
    "market_ticker TEXT NOT NULL, "
    "probability_ppm INTEGER NOT NULL, "
    "baseline_ppm INTEGER NOT NULL, "
    "traded INTEGER NOT NULL"
    ")"
)

#: DDL for the projected ``resolutions`` table (one row per resolved market).
_CREATE_RESOLUTIONS_SQL = (
    "CREATE TABLE resolutions ("
    "market_ticker TEXT NOT NULL, outcome_ppm INTEGER NOT NULL"
    ")"
)

#: Parametrized insert for one projected forecast row.
_INSERT_FORECAST_SQL = (
    "INSERT INTO forecasts (market_ticker, probability_ppm, baseline_ppm, traded) "
    "VALUES (?, ?, ?, ?)"
)

#: Parametrized insert for one projected resolution row.
_INSERT_RESOLUTION_SQL = (
    "INSERT INTO resolutions (market_ticker, outcome_ppm) VALUES (?, ?)"
)


def _forecast_row(forecast: FixtureForecast) -> tuple[str, int, int, int]:
    """Project one forecast into its ``forecasts`` table row.

    Args:
        forecast: The admitted forecast to project.

    Returns:
        The ``(market_ticker, probability_ppm, baseline_ppm, traded)`` row, with
        ``baseline_ppm`` lifted from pips and ``traded`` as ``0``/``1``.
    """
    return (
        forecast.market_ticker,
        forecast.probability_ppm.value,
        forecast.baseline_executable_price_pips * _BASELINE_PPM_PER_PIP,
        1 if forecast.traded else 0,
    )


def _resolution_row(market_ticker: str, outcome: ResolutionOutcome) -> tuple[str, int]:
    """Project one market resolution into its ``resolutions`` table row.

    Args:
        market_ticker: The resolved market's ticker.
        outcome: The market's ground-truth outcome.

    Returns:
        The ``(market_ticker, outcome_ppm)`` row, with ``outcome_ppm`` the ppm
        certainty of the outcome (``_OUTCOME_YES_PPM`` for ``YES`` else ``0``).
    """
    outcome_ppm = _OUTCOME_YES_PPM if outcome is ResolutionOutcome.YES else 0
    return (market_ticker, outcome_ppm)


def create_gate_database(inputs: EvaluationInputs) -> sqlite3.Connection:
    """Project temporally-admitted inputs into an in-memory gate database.

    Builds a fresh ``:memory:`` connection with the metric UDFs registered and
    two tables -- ``forecasts`` and ``resolutions`` -- populated from ``inputs``.
    Every metric query ``INNER JOIN``s them, so unresolved markets are excluded
    exactly as the Python path excludes them.

    Args:
        inputs: The (already temporally-admitted) evaluation inputs to project.

    Returns:
        An open connection the caller is responsible for closing.
    """
    conn = sqlite3.connect(":memory:")
    _register_udfs(conn)
    conn.execute(_CREATE_FORECASTS_SQL)
    conn.execute(_CREATE_RESOLUTIONS_SQL)
    forecast_rows = [_forecast_row(forecast) for forecast in inputs.forecasts]
    resolution_rows = [
        _resolution_row(ticker, outcome)
        for ticker, outcome in inputs.resolutions.items()
    ]
    conn.executemany(_INSERT_FORECAST_SQL, forecast_rows)
    conn.executemany(_INSERT_RESOLUTION_SQL, resolution_rows)
    return conn


#: Per-metric single-scalar SQL, each yielding exactly one row and one column so
#: it can be wrapped as a scalar subquery. No untrusted data is interpolated --
#: the queries are static literals -- keeping bandit B608 trivially clean. Metric
#: names are dict keys, never spliced into SQL. Each query computes only bounded
#: component sums in SQL and defers the wide multiply / directional division to a
#: Python UDF (see the module docstring).
DEFAULT_GATE_QUERIES: Mapping[str, str] = {
    "brier": """
SELECT hk_ceil_div(
  SUM((f.probability_ppm - r.outcome_ppm)
      * (f.probability_ppm - r.outcome_ppm)),
  COUNT(*) * 1000000)
FROM forecasts AS f
JOIN resolutions AS r ON f.market_ticker = r.market_ticker
""",
    "brier_skill_vs_executable_price": """
SELECT hk_skill(
  SUM((f.probability_ppm - r.outcome_ppm)
      * (f.probability_ppm - r.outcome_ppm)),
  SUM((f.baseline_ppm - r.outcome_ppm)
      * (f.baseline_ppm - r.outcome_ppm)))
FROM forecasts AS f
JOIN resolutions AS r ON f.market_ticker = r.market_ticker
""",
    "log_score": """
SELECT hk_ceil_div(
  SUM(hk_surprisal(f.probability_ppm, r.outcome_ppm)),
  COUNT(*))
FROM forecasts AS f
JOIN resolutions AS r ON f.market_ticker = r.market_ticker
""",
    "expected_calibration_error": """
SELECT hk_ceil_div(
  SUM(bin_deviation),
  (SELECT COUNT(*)
   FROM forecasts AS f
   JOIN resolutions AS r ON f.market_ticker = r.market_ticker))
FROM (
  SELECT ABS(
    SUM(f.probability_ppm)
    - SUM(CASE WHEN r.outcome_ppm = 1000000 THEN 1 ELSE 0 END) * 1000000
  ) AS bin_deviation
  FROM forecasts AS f
  JOIN resolutions AS r ON f.market_ticker = r.market_ticker
  GROUP BY MIN(f.probability_ppm / 100000, 9)
)
""",
    "calibration_slope": """
SELECT hk_slope(n, sum_p, sum_o, sum_pp, sum_po)
FROM (
  SELECT
    COUNT(*) AS n,
    SUM(f.probability_ppm) AS sum_p,
    SUM(r.outcome_ppm) AS sum_o,
    SUM(f.probability_ppm * f.probability_ppm) AS sum_pp,
    SUM(f.probability_ppm * r.outcome_ppm) AS sum_po
  FROM forecasts AS f
  JOIN resolutions AS r ON f.market_ticker = r.market_ticker
)
""",
    "calibration_intercept": """
SELECT hk_intercept(n, sum_p, sum_o, sum_pp, sum_po)
FROM (
  SELECT
    COUNT(*) AS n,
    SUM(f.probability_ppm) AS sum_p,
    SUM(r.outcome_ppm) AS sum_o,
    SUM(f.probability_ppm * f.probability_ppm) AS sum_pp,
    SUM(f.probability_ppm * r.outcome_ppm) AS sum_po
  FROM forecasts AS f
  JOIN resolutions AS r ON f.market_ticker = r.market_ticker
)
""",
    "sharpness": """
SELECT hk_sharpness(n, sum_p, sum_pp)
FROM (
  SELECT
    COUNT(*) AS n,
    SUM(f.probability_ppm) AS sum_p,
    SUM(f.probability_ppm * f.probability_ppm) AS sum_pp
  FROM forecasts AS f
  JOIN resolutions AS r ON f.market_ticker = r.market_ticker
)
""",
    "traded_vs_skipped_brier_delta": """
SELECT hk_delta(
  (SELECT SUM((f.probability_ppm - r.outcome_ppm)
              * (f.probability_ppm - r.outcome_ppm))
   FROM forecasts AS f
   JOIN resolutions AS r ON f.market_ticker = r.market_ticker
   WHERE f.traded = 0),
  (SELECT COUNT(*)
   FROM forecasts AS f
   JOIN resolutions AS r ON f.market_ticker = r.market_ticker
   WHERE f.traded = 0),
  (SELECT SUM((f.probability_ppm - r.outcome_ppm)
              * (f.probability_ppm - r.outcome_ppm))
   FROM forecasts AS f
   JOIN resolutions AS r ON f.market_ticker = r.market_ticker
   WHERE f.traded = 1),
  (SELECT COUNT(*)
   FROM forecasts AS f
   JOIN resolutions AS r ON f.market_ticker = r.market_ticker
   WHERE f.traded = 1))
""",
}


class SqlGateComputer:
    """Reproduces every registered gate metric independently, in SQLite.

    The default :data:`DEFAULT_GATE_QUERIES` catalogue is injectable so a
    crosscheck test can corrupt a single query and prove the disagreement is
    caught; every other query stays the trusted default.
    """

    def __init__(self, queries: Mapping[str, str] = DEFAULT_GATE_QUERIES) -> None:
        """Bind the per-metric query catalogue.

        Args:
            queries: The metric-name to single-scalar-SQL mapping to run;
                defaults to :data:`DEFAULT_GATE_QUERIES`.
        """
        self._queries = queries

    def compute(
        self, inputs: EvaluationInputs, plan: GatePlan
    ) -> Mapping[str, SqlMetricValue]:
        """Reproduce every metric in ``plan`` over ``inputs``, in SQL.

        Args:
            inputs: The (temporally-admitted) evaluation inputs to score.
            plan: The gate plan whose ``metric_windows`` names the metrics to
                reproduce.

        Returns:
            One value per ``plan.metric_windows`` name: a ppm-scaled ``int``, the
            :data:`~hedgekit.evaluation.registry.NOT_IMPLEMENTED` sentinel for the
            execution-track stub, the :data:`~hedgekit.evaluation.cohorts.UNDEFINED`
            sentinel for an empty cohort, or :data:`SQL_QUERY_FAILED` for a query
            that raised. This method never raises on a bad query.
        """
        conn = create_gate_database(inputs)
        try:
            return {
                name: self._value_for(conn, name)
                for name, _window in plan.metric_windows
            }
        finally:
            conn.close()

    def _value_for(self, conn: sqlite3.Connection, name: str) -> SqlMetricValue:
        """Compute one metric's value, degrading a raised query to a sentinel.

        Args:
            conn: The open gate database.
            name: The registered metric name to compute.

        Returns:
            The metric's SQL value;
            :data:`~hedgekit.evaluation.registry.NOT_IMPLEMENTED` for the stub,
            :data:`~hedgekit.evaluation.cohorts.UNDEFINED` for a ``NULL``
            (empty-cohort) result, or :data:`SQL_QUERY_FAILED` if the query
            raised *or* no query is catalogued for a planned metric (so a future
            registered metric lacking a reproduction surfaces a loud mismatch,
            never a :class:`KeyError` crash).
        """
        if name == _FILL_VS_MODEL_SLIPPAGE:
            return NOT_IMPLEMENTED
        query = self._queries.get(name)
        if query is None:
            return SQL_QUERY_FAILED
        try:
            scalar = conn.execute(query).fetchone()[0]
        except sqlite3.Error:
            return SQL_QUERY_FAILED
        if scalar is None:
            return UNDEFINED
        return int(scalar)
