"""Shared component: typed configuration loading and validation (SPEC §16).

Centralizes the single-source-of-truth configuration for all four processes.
:func:`load_config` parses a SPEC §16 YAML file into an immutable
:class:`HedgekitConfig` (unknown keys fatal, integer-units enforced), while
:func:`config_hash` and :func:`diff_configs` make every version ledgerable.
The credential-boundary and budget checks enforced at startup (SPEC §5.2)
build on this typed foundation in later issues.
"""

from __future__ import annotations

from hedgekit.config.loader import (
    ConfigError,
    confidence_to_ppm,
    load_config,
    load_default_config,
)
from hedgekit.config.recorder import (
    ConfigEventRecorder,
    ConfigLoadEvent,
    InMemoryConfigEventRecorder,
)
from hedgekit.config.schema import (
    AlertsConfig,
    AlertSink,
    CanaryConfig,
    CapitalConfig,
    EvaluationConfig,
    ExchangeConfig,
    ForecastBudget,
    ForecastConfig,
    HedgekitConfig,
    HorizonDays,
    ModelRef,
    OpsConfig,
    RiskConfig,
    ScreenerConfig,
)
from hedgekit.config.versioning import (
    ConfigDiff,
    canonical_json,
    config_hash,
    diff_configs,
    flatten,
    format_diff,
)

__all__ = [
    "AlertSink",
    "AlertsConfig",
    "CanaryConfig",
    "CapitalConfig",
    "ConfigDiff",
    "ConfigError",
    "ConfigEventRecorder",
    "ConfigLoadEvent",
    "EvaluationConfig",
    "ExchangeConfig",
    "ForecastBudget",
    "ForecastConfig",
    "HedgekitConfig",
    "HorizonDays",
    "InMemoryConfigEventRecorder",
    "ModelRef",
    "OpsConfig",
    "RiskConfig",
    "ScreenerConfig",
    "canonical_json",
    "confidence_to_ppm",
    "config_hash",
    "diff_configs",
    "flatten",
    "format_diff",
    "load_config",
    "load_default_config",
]
