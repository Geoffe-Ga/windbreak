# Live Forecasting Providers — Decomposition Summary (2026-07-08)

## Why this epic exists

M2 (epic #4, children #22–#28, all closed) delivered the Forecast Engine's
*infrastructure*: the twelve-stage pipeline wiring, the `LlmTransport`
cassette seam, the sandboxed `ResearchTools` boundary, citation
verification, injection defense, triage, budgets, and canaries. What it
deliberately did **not** deliver is modeling: today every "vote" is the
market baseline ± a fixed 1-point offset (`pipeline.py::_VOTE_OFFSETS_PPM`),
the ensemble members are hardcoded fictional models (`_VOTE_MODELS`),
research is fixture-derived, and the calibration map is the identity. The
system cannot, even in principle, disagree with the market — which means it
can never find an edge.

This decomposition replaces the stub brains with real forecasting behind the
exact seams M2 built, using **hosted superforecasting APIs**
(FutureSearch-class research forecasters) *plus* direct pinned-LLM
structured votes as independent ensemble member families. Provider-agnostic
by design: FutureSearch is the first adapter, not a marriage.

## Honesty stance (binding on every child issue)

Per SPEC §19: most operators should expect *no durable edge*; paper failure
is a valid success state. Nothing in this epic may claim, imply, or encode
an edge that has not been measured on resolved forecasts. Concretely:

- Abstention stays a first-class, *scored* outcome — a provider that can't
  support a probability abstains; it never bluffs.
- Shrinkage toward the market baseline (λ), price bands, dispersion-scaled
  sizing, and the M6 promotion gates are retained unchanged — the epic makes
  the engine *capable* of edge, evaluation decides whether edge *exists*.
- A provider's votes gate into live eligibility only after a resolved paper
  track record beats the market baseline (Brier skill) — trust is earned per
  provider, on data, not vibes.

## External research (2026-07-08)

- FutureSearch (https://futuresearch.ai/) — hosted AI research forecasters:
  Python SDK (`futuresearch-python`) + REST API; `forecast()` returns
  per-question calibrated probabilities with rationale (binary) or
  p10/p50/p90 percentiles (numeric); `multi_agent()` runs parallel research
  on one question ($0.30–$2/question ≈ 300k–2M micros — inside our
  `per_forecast_micros: 3_000_000` budget); probabilities clamped to
  [3%, 97%] (aligns with our 5¢–95¢ price bands); pay-as-you-go, $20 free
  credit. They publish a Kalshi/Polymarket trade-finding workflow in their
  docs. API docs: https://futuresearch.ai/docs/api/
- Design consequence: a research forecaster is *not* a "no-tools structured
  vote" (§8.2) — it is research+vote fused. ADR in ISSUE_01 amends the
  ensemble model to two member *families* (research forecasters; no-tools
  LLM votes) with per-member provenance and independent injection screening.

## Files in this batch

- `EPIC_09_live_forecasting.md` — the epic
- `EPIC_09_ISSUE_01_provider-seam.md` — skeleton: provider seam + ADR;
  probabilities parsed from responses, not baseline offsets
- `EPIC_09_ISSUE_02_futuresearch-adapter.md` — core: FutureSearch adapter
- `EPIC_09_ISSUE_03_llm-ensemble-live.md` — core: live pinned-LLM votes
- `EPIC_09_ISSUE_04_live-research-triage.md` — core: live web research +
  triage transports
- `EPIC_09_ISSUE_05_failure-hardening.md` — edges: failure taxonomy, quorum,
  real cost accounting
- `EPIC_09_ISSUE_06_honest-edge-gating.md` — edges: provider track-record
  gate + calibration-map versioning
- `EPIC_09_ISSUE_07_canary-observability.md` — polish: live canaries, drift
  alerts, cost/skill dashboards, runbook

## Sequencing

```
ISSUE_01 (skeleton)
   ├─→ ISSUE_02 (FutureSearch)   ─┐
   ├─→ ISSUE_03 (LLM ensemble)   ─┼─→ ISSUE_05 (hardening) → ISSUE_06 (gating) → ISSUE_07 (polish)
   └─→ ISSUE_04 (research/triage)─┘
```

02/03/04 are parallel-safe after 01. The system stays demoable at every
step: cassette replay keeps CI offline and byte-deterministic throughout.
