# Evaluation

This document describes windbreak's evaluation and calibration methodology as
specified in SPEC §13: three separate tracks that are never merged into one
vanity number, precommitted baselines and observation windows, a clustered
bootstrap, a pre-registered gate plan hashed at PAPER entry, and a documented
power analysis — all implemented in `windbreak/evaluation/`.

## Three separate tracks (SPEC §13.1)

Evaluation never merges these into a single headline number:

- **Forecast quality** — did probabilities beat baselines?
- **Selection quality** — did traded forecasts outperform skipped ones?
- **Execution quality** — did fills match modeled prices, fees, and slippage?

The default expectation is plain: **no edge is the default expectation.**
Discovering that and stopping at paper trading is a success state, not a
failure — measurements outrank narratives (SPEC §3.7): if the evaluation
harness says "no edge," the harness wins, not the operator's read of the
rationale text.

## Baselines (SPEC §13.2)

The primary baseline is the **executable market price at the forecast's
baseline snapshot** — if the forecast can't beat the crowd's own price at the
same instant, no edge exists by construction. Secondary baselines (midpoint,
uniform 50%, base-rate where known, previous forecast) are also computed for
context. `windbreak/evaluation/baselines.py` derives all of these from a
forecast's own referenced quote snapshot using only exact integer arithmetic
(a pip is exactly 100 ppm; no division, no rounding decision, no float).

## Precommitted observation windows (SPEC §13.4)

Multiple forecasts per market are handled by declared windows (first-per-market,
latest-before-close, daily snapshots, trade-triggering); mixing windows in one
metric is a test failure, not a stylistic choice. `config.evaluation.observation_window`
names the window the headline Brier metric uses
(`windbreak/evaluation/windows.py`).

## Clustered bootstrap (SPEC §13.5)

Confidence intervals are computed by a bootstrap **clustered by
event/correlation group**, so related markets never masquerade as independent
observations (`windbreak/evaluation/bootstrap.py`). The confidence level is
`config.evaluation.bootstrap_confidence_ppm`, expressed as an integer
parts-per-million (never a raw float) — the loader converts the SPEC §16 YAML
`bootstrap_confidence: 0.95` into this field at load time. A companion power
analysis (`windbreak/evaluation/power.py`) states the minimum detectable Brier
skill at N=300 with observed clustering, so an underpowered pass is never
mistaken for proof.

## Pre-registration (SPEC §13.6, anti-Goodhart)

At PAPER entry, the complete gate plan — every metric, window, threshold,
baseline, and clustering scheme — is canonically serialized, SHA-256 hashed,
and ledgered (`windbreak/evaluation/preregistration.py`). A byte-identical
re-registration is idempotent; any actual change to the plan resets the PAPER
evaluation clock. Promotion thresholds referenced by the plan include
`config.evaluation.promotion_min_resolved`,
`config.evaluation.promotion_min_independent_event_groups`, and
`config.evaluation.brier_skill_required_ppm`.

## Temporal integrity (SPEC §1.1-6)

Only forecasts made in real time, on then-unresolved questions, ever count
toward a gate (`windbreak/evaluation/temporal.py`): a forecast is rejected at
ingestion if it predates deployment, was created at or after its market's
resolution, or its market never resolved at all. This guards against an LLM
recalling a resolved outcome from its training data.

## Live-vs-paper divergence (SPEC §10.9, §10.10)

Once a run has both LIVE forecasts and LIVE execution records,
`windbreak.evaluation.live_divergence.monitor_live_divergence` scores two
series against the pre-registered plan's thresholds:
`config.evaluation.live_slippage_ratio_limit_ppm` (cost slippage of real fills
vs. the paper model) and `config.evaluation.live_brier_degradation_band_ppm`
(rolling LIVE-over-PAPER forecast-skill decay), each scored over the most
recent `config.evaluation.live_rolling_window_size` resolved records. A breach
fires a critical alert and an automatic one-rung demotion.

**Known limitation.** `monitor_live_divergence` exists and is fully tested, but
it is not yet wired into any scheduler — nothing calls it automatically on a
cadence today. Tracked in issue #200, alongside the rest of the live-divergence
fast-follow hardening.
