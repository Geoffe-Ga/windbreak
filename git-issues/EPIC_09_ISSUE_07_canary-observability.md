## Role

You are a senior Python engineer with production-observability experience (drift detection, cost dashboards, runbooks) working across `hedgekit/forecast/`, `hedgekit/dashboard/`, and `docs/`.

## Goal

The live provider fleet is observable and self-protecting: the weekly canary set runs against real providers (recorded), API-provider drift (a vendor silently changing their forecaster) trips the existing canary gate, and the dashboard/reports show cost per forecast, abstention rates, and per-provider skill — with a RUNBOOK section for every new operational failure mode.

## Context

- **Parent epic:** #183
- **Predecessor issue(s):** #193 and #194 (merged first — error taxonomy and track-record read model are what this issue surfaces).
- **SPEC section:** `plans/SPEC_v3.md` §8.6 (canary set, drift → `eligible_for_live=false` until operator ack), §16 `canary:` + budget blocks, §19 (RUNBOOK: respond to canary drift), §13 (report surfaces).
- **Files involved:**
  - `hedgekit/forecast/canary.py` — extend the existing `CanaryGate` to cover research-forecaster providers: per-provider canary verdicts; a FutureSearch forecaster-version change or tolerance-exceeding canary shift blocks that provider's live eligibility until ack (modify)
  - `scripts/run-canaries.sh` — operator entry point: runs the canary set live, records cassettes, ledgers verdicts (new)
  - `hedgekit/dashboard/views/` + `hedgekit/reports/` — provider panel: cost per forecast (micros), research cost per resolved forecast, abstention rate by reason, per-provider Brier skill (from #194's read model), canary status (new/modify)
  - `docs/RUNBOOK.md` — sections: rotate provider keys; respond to canary drift / provider version drift; respond to budget exhaustion; add/remove a provider (modify)
  - `tests/forecast/test_canary_providers.py`, `tests/dashboard/`/`tests/reports/` equivalents (new)
- **Prior decisions:** drift handling is *alert + block until operator acknowledges*, never auto-adapt (§8.6); canary questions are stable and answers/dates are fixture-recorded, so CI replays the whole canary path; dashboards read from the ledger/read-models only — no live provider calls from the dashboard process (§5 process boundaries).
- **State of the world:** `CanaryGate` exists and is tested with synthetic drift against the stub ensemble; there is no per-provider dimension, no cost/skill surface anywhere an operator can see, and the RUNBOOK predates live providers.

## Output Format

Deliverable is a single PR containing:

- [ ] Per-provider canary verdicts + gating (version drift and answer drift both covered), replay-tested with synthetic drift fixtures
- [ ] `scripts/run-canaries.sh` with clear operator output and non-zero exit on any drift
- [ ] Dashboard/report surfaces listed above, populated from ledger/read-models
- [ ] RUNBOOK sections for the four new procedures, each with exact commands
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: vendor silently swaps their forecaster**
```python
def test_futuresearch_version_drift_blocks_that_provider_only(canary_env):
    run_canaries(canary_env, futuresearch_version="fs-2.1")  # pinned: fs-2.0
    assert canary_env.gate.is_live_blocked(provider="futuresearch", created_at=NOW)
    assert not canary_env.gate.is_live_blocked(provider="anthropic", created_at=NOW)
```

**Example: report line (weekly)**
```text
provider=futuresearch resolved=212 brier_skill_ppm=+14200 cost_per_forecast=1_340_000µ abstain_rate=9% canary=OK
provider=openai       resolved=198 brier_skill_ppm=-2100  cost_per_forecast=810_000µ  abstain_rate=6% canary=OK
```

## Constraints

**Scope fence:** No new metrics *math* (Brier/skill comes from evaluation read-models); no alert-sink changes beyond wiring existing M0 abstractions; no frontend framework additions — extend the existing dashboard views. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Everything replay-tested in CI; the dashboard renders sensibly with zero live providers configured (empty-state, not error). Honesty: report surfaces show negative skill and losses exactly as prominently as positive — no vanity framing.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] RUNBOOK.md updated; docstrings current.
- [ ] PR body includes `Refs #183` and `Closes #195`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `forecast-engine`
