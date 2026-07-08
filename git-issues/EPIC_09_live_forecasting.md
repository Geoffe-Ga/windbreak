## Epic Summary

Replace the Forecast Engine's deterministic stub stages with **real forecasting**: hosted superforecasting APIs (FutureSearch-class research forecasters) and live pinned-LLM structured votes, wired behind the exact seams M2 built (`LlmTransport` cassettes, `ResearchTools` sandbox, injection screens, budgets, canaries). Today the engine cannot disagree with the market — every vote is the baseline ± a fixed offset (`hedgekit/forecast/pipeline.py::_VOTE_OFFSETS_PPM`) and the ensemble members are fictional (`_VOTE_MODELS`). After this epic, probabilities come from independent, provenance-pinned, injection-screened providers; the engine becomes *capable* of edge, and M6 evaluation decides whether edge *exists*. SPEC_v3 §8 (engine), §8.6 (pinning/canaries), §16 `forecast:` block, §19 (honesty requirements).

## Honesty & risk stance (binding)

Per SPEC §19, most operators should expect *no durable edge* and paper failure is a valid success state. This epic must not claim or encode unmeasured edge:

- Abstention remains first-class and scored; a provider that cannot support a probability abstains — it never bluffs, and neither do we.
- Shrinkage toward market baseline (λ), 5¢–95¢ price bands, dispersion-scaled Kelly, and M6 promotion gates are retained **unchanged**.
- Every vote carries provider/model/version provenance and a response fingerprint (T14); a provider's votes count toward live eligibility only after its resolved paper track record beats the market baseline (Brier skill) — trust is earned per provider, on data.
- All spend is ledgered in micros against per-forecast/per-day budgets; a budget overrun fails closed.

Risk is managed, not avoided: the point is real, calibrated disagreement with the market, sized by conviction and dispersion — inside every existing cap and gate.

## Scope

**In scope:**
- Provider seam (`ForecastProvider`) + ADR amending §8.2's ensemble into two member families: research forecasters (tools fused) and no-tools LLM votes; probabilities parsed from structured responses (integer ppm), never derived from the baseline.
- FutureSearch adapter: question → calibrated probability + rationale + citations + reported cost; HTTP-level cassette record/replay so CI stays offline and byte-deterministic.
- Live pinned Anthropic/OpenAI ensemble transports replacing `_VOTE_MODELS`, with structured integer-ppm vote schema and discard-and-ledger on invalid output.
- Live `SearchTransport`/`FetchTransport` behind the egress allowlist, real publication dates, and a real cheap triage stage-0 model.
- Failure hardening: provider error taxonomy, bounded retries, ensemble quorum, new registered abstention reasons, real per-provider price tables.
- Honest-edge gating: versioned calibration-map loader (identity v0 until M6 fits one), per-provider track-record gate consuming M6 outputs.
- Live canary runs, provider drift alerting, cost/skill observability, RUNBOOK updates.

**Out of scope:**
- Calibration-map *fitting*, Brier/gate computation — epic #8 (M6 Evaluation); this epic only loads/applies versioned maps and consumes track-record outputs.
- Trade selection/sizing changes — epic #7 (M5 Selector) consumes `ForecastRecord`s unchanged.
- Any live *order* path — M3/M7 own that; this epic never touches credentials or order APIs.
- Local/self-hosted model ensemble members — SPEC §18 M8 (post-v1).

## Success Criteria

The epic is done when:

- [ ] A screened market produces a `ForecastRecord` whose probability comes from live providers (recorded to cassettes), with ≥ 2 independent member families voting, verified citations, and full cost accounting — and CI replays the same run byte-deterministically offline.
- [ ] The engine can *disagree with the market*: recorded fixtures demonstrate |forecast − baseline| beyond the stub's fixed offsets, with rationale and citations explaining why.
- [ ] Provider failure of any single vendor degrades to quorum-abstention, never to a silent stub value or an unledgered error.
- [ ] The injection corpus stays green end-to-end with real provider text in the loop (§8.5 release blocker).
- [ ] No unmeasured-edge claims: README/RUNBOOK language per §19; per-provider live eligibility gated on resolved paper track record.
- [ ] All child issues are closed.
- [ ] Smoke tests for the full epic surface pass on `main`.

## Child Issues



- [ ] #184 — Skeleton: provider seam + ADR; probabilities from responses, not baseline offsets
- [ ] #189 — Core: FutureSearch research-forecaster adapter with cassette record/replay
- [ ] #191 — Core: live pinned-LLM ensemble members with structured integer votes
- [ ] #192 — Core: live web research and triage transports
- [ ] #193 — Edges: provider failure hardening, quorum, and real cost accounting
- [ ] #194 — Edges: honest-edge gating — provider track records and calibration versioning
- [ ] #195 — Polish: live canaries, drift alerts, cost/skill observability, runbook

## Sequencing Notes

- **Blocked by:** epic #4 (M2) — all children (#22–#28) are closed; the seams this epic fills are on `main`.
- **Internal order:** #184 first (skeleton); #189/#191/#192 parallel-safe after it; then #193 → #194 → #195.
- **Unblocks:** meaningful M5 paper operation (paper trades on real forecasts) and M6 gate evaluation on real data — the M6 done-gate is only *measurable* once forecasts are real.
- **Parallel-safe:** all M3/M4 work — this epic never crosses the research/execution firewall (§1.1-5).

## SPEC Reference

`plans/SPEC_v3.md` — §8 (Forecast Engine, entire), §8.2 (stage list; amended by ADR in #184), §8.4 (triage), §8.5 (injection defense, release blocker), §8.6 (pinning/canaries/T14), §8.8 (citations/abstention), §16 `forecast:` block, §18 M2/M8, §19 (README honesty). External: https://futuresearch.ai/docs/api/ (FutureSearch API), `git-issues/2026-07-08_live_forecasting_decomposition.md` (research notes).

## Labels

`epic`, `spec-decomposition`, `forecast-engine`
