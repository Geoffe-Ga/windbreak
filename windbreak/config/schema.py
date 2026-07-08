"""Typed, immutable configuration schema for windbreak (SPEC §16).

Every configuration section is a frozen dataclass whose field defaults are
the SPEC §16 example verbatim, so ``WindbreakConfig()`` is a complete, valid,
production-shaped configuration on its own. All leaf values are integers,
strings, booleans, ``None``, or tuples of those, never floats (SPEC §6.1's
integer-units invariant). The single fractional value in SPEC §16,
``bootstrap_confidence``, is stored as an integer parts-per-million field and
converted by the loader; see :attr:`EvaluationConfig.bootstrap_confidence_ppm`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Metadata tag naming the YAML key a field is read from when it differs from
#: the field's own name (e.g. ``bootstrap_confidence`` -> ``*_ppm``).
_YAML_KEY = "yaml_key"

#: Metadata tag naming the loader converter applied to a field's raw value.
_CONVERT = "convert"


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A pinned reference to a single forecasting model.

    Attributes:
        provider: The model provider (e.g. ``anthropic``, ``openai``).
        model: The provider-specific, operator-pinned model identifier.
    """

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class AlertSink:
    """A destination for operator alerts.

    Attributes:
        type: The sink transport (e.g. ``ntfy``).
        topic: The operator-configured topic or channel to publish to.
    """

    type: str
    topic: str


@dataclass(frozen=True, slots=True)
class HorizonDays:
    """Inclusive bounds, in days, on tradeable market resolution horizons."""

    min: int = 2
    max: int = 120


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    """Exchange connectivity and product-eligibility policy."""

    provider: str = "kalshi"
    environment: str = "demo"
    product_allowlist: tuple[str, ...] = ("predictions",)
    product_blocklist: tuple[str, ...] = ("perps", "margin")
    require_jurisdiction_eligible: bool = True


@dataclass(frozen=True, slots=True)
class CapitalConfig:
    """Capital floor, ratchet, and deployment limits (micro-dollar units)."""

    floor_micros: int = 1000000000
    floor_ratchet_ppm_of_new_profits: int = 500000
    profit_sweep_threshold_micros: int = 250000000
    max_deploy_pct_above_floor_ppm: int = 500000
    micro_cap_micros: int = 100000000


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Risk-kernel thresholds and time-to-live limits.

    Attributes:
        require_human_ack_above_micros: Notional above which a trade needs
            human acknowledgement, or ``None`` to require none (paper mode).
        kill_after_consecutive_mismatches: Number of consecutive reconciliation
            ``BREACH`` outcomes that auto-engages the kill switch (issue #35).
    """

    min_net_edge_ppm: int = 30000
    annualized_hurdle_ppm: int = 200000
    idle_cash_apr_ppm: int = 40000
    kelly_fraction_ppm: int = 100000
    dispersion_zero_ceiling_ppm: int = 200000
    min_open_price_pips: int = 500
    max_open_price_pips: int = 9500
    max_participation_ppm: int = 250000
    max_pos_market_pct_ppm: int = 20000
    max_pos_event_pct_ppm: int = 40000
    max_pos_bucket_pct_ppm: int = 100000
    daily_loss_limit_pct_ppm: int = 20000
    max_drawdown_pct_ppm: int = 100000
    max_orders_per_hour: int = 20
    max_notional_per_day_micros: int = 500000000
    quote_ttl_seconds: int = 10
    approval_ttl_seconds: int = 60
    resting_order_ttl_seconds: int = 900
    cancel_on_move_ticks: int = 2
    clock_skew_max_seconds: int = 2
    require_human_ack_above_micros: int | None = None
    kill_after_consecutive_mismatches: int = 3


@dataclass(frozen=True, slots=True)
class ScreenerConfig:
    """Market-screening filters applied before forecasting."""

    category_blocklist: tuple[str, ...] = (
        "sports",
        "crypto_price",
        "celebrity",
        "insider_prone",
    )
    min_volume_24h_micros: int = 5000000000
    min_depth_contract_centis: int = 10000
    horizon_days: HorizonDays = field(default_factory=HorizonDays)


@dataclass(frozen=True, slots=True)
class ForecastBudget:
    """Per-forecast and per-day research spend caps (micro-dollar units)."""

    per_forecast_micros: int = 3000000
    per_day_micros: int = 20000000
    max_pages: int = 20


@dataclass(frozen=True, slots=True)
class CanaryConfig:
    """Cadence policy for canary forecasts that probe calibration drift."""

    enabled: bool = True
    cadence_days: int = 7


def _default_ensemble() -> tuple[ModelRef, ...]:
    """Return the SPEC §16 default two-model forecasting ensemble."""
    return (
        ModelRef("anthropic", "pinned-by-operator"),
        ModelRef("openai", "pinned-by-operator"),
    )


def _default_triage_model() -> ModelRef:
    """Return the SPEC §16 default triage model reference."""
    return ModelRef("cheapest-adequate", "pinned-by-operator")


@dataclass(frozen=True, slots=True)
class ForecastConfig:
    """Ensemble, triage, budget, and calibration-canary forecasting policy."""

    ensemble: tuple[ModelRef, ...] = field(default_factory=_default_ensemble)
    triage_model: ModelRef = field(default_factory=_default_triage_model)
    triage_threshold_ppm: int = 50000
    shrink_to_market_lambda_ppm: int = 250000
    budget: ForecastBudget = field(default_factory=ForecastBudget)
    min_verified_citations: int = 3
    canary: CanaryConfig = field(default_factory=CanaryConfig)


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """Calibration and model-promotion evaluation thresholds.

    Attributes:
        bootstrap_confidence_ppm: The bootstrap confidence level expressed in
            parts-per-million (an integer), not a probability float; SPEC
            §16's ``bootstrap_confidence: 0.95`` maps here to ``950000`` via
            the loader's converter, preserving the integer-units invariant.
    """

    min_resolved_for_calibration: int = 150
    promotion_min_resolved: int = 300
    promotion_min_independent_event_groups: int = 100
    brier_skill_required_ppm: int = 10000
    bootstrap_confidence_ppm: int = field(
        default=950000,
        metadata={_YAML_KEY: "bootstrap_confidence", _CONVERT: "confidence_to_ppm"},
    )
    observation_window: str = "latest_before_close"


@dataclass(frozen=True, slots=True)
class OpsConfig:
    """Operational filesystem, disk-headroom, and shutdown policy."""

    state_dir: str = "~/.local/share/windbreak"
    backup_dir: str = "~/windbreak-backups"
    min_free_disk_mb: int = 1000
    cancel_open_orders_on_shutdown: bool = True


def _default_alert_sinks() -> tuple[AlertSink, ...]:
    """Return the SPEC §16 default single ntfy alert sink."""
    return (AlertSink("ntfy", "configured-by-operator"),)


@dataclass(frozen=True, slots=True)
class AlertsConfig:
    """Operator alert-sink fan-out configuration."""

    sinks: tuple[AlertSink, ...] = field(default_factory=_default_alert_sinks)


@dataclass(frozen=True, slots=True)
class WindbreakConfig:
    """The complete, immutable windbreak configuration (SPEC §16 root).

    Attributes:
        mode_ceiling: The highest operating mode the runtime may ever reach.
    """

    mode_ceiling: str = "paper"
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    capital: CapitalConfig = field(default_factory=CapitalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
    ops: OpsConfig = field(default_factory=OpsConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
