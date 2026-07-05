## Role

You are a senior Python engineer with quantitative-sizing experience, working in this repo's `hedgekit/selector/` package, fluent in `hypothesis` property-based testing.

## Goal

Intents are sized by fractional Kelly on above-floor capital using the calibrated, shrunk probability, scaled down by ensemble dispersion via a property-tested `g()` function, then clipped by every configured cap — per-market, per-event, per-correlation-bucket, total-deployed, daily-notional, live-micro cap, mode ceiling, exchange minimum order size, and book participation ≤ `max_participation_ppm`.

## Context

- **Parent epic:** #EPIC_06_NUMBER
- **Predecessor issue(s):** #EPIC_06_ISSUE_02_NUMBER (must be merged first — provides edge computation and the 1-contract placeholder this issue replaces)
- **SPEC section:** `plans/SPEC_v3.md` §9.5 (sizing), §9.6 (disagreement-scaled sizing, T2), §4 row T2, §16 `risk:` keys `kelly_fraction_ppm`, `dispersion_zero_ceiling_ppm`, `max_participation_ppm`, `max_pos_market_pct_ppm`, `max_pos_event_pct_ppm`, `max_pos_bucket_pct_ppm`, `max_notional_per_day_micros`, `micro_cap_micros`
- **Files involved:**
  - `hedgekit/selector/sizing.py` — Kelly, dispersion scaling `g()`, cap clipping pipeline (new)
  - `hedgekit/selector/__init__.py` — replace the 1-contract placeholder with real sizing
  - `tests/selector/test_sizing_properties.py` — hypothesis property suite
  - `tests/selector/test_sizing_examples.py` — hand-computed example cases
- **Prior decisions:** `g()` is monotone non-increasing, `g(0)=1`, and reaches 0 at `dispersion_zero_ceiling_ppm`; the exact functional form is config-selected but those three properties are invariants (§9.6). Sizing operates only on above-floor capital as reported in the risk-config snapshot — the selector never computes the floor itself (that is the Kernel's job; the selector just respects the snapshot). All arithmetic fixed-point; rounding on size always rounds *down*.
- **State of the world:** edge computation and entry conditions work; every passing intent is sized at the 1-contract placeholder from #EPIC_06_ISSUE_02_NUMBER.

## Output Format

Deliverable is a single PR containing:

- [ ] `hedgekit/selector/sizing.py`: `kelly_size()`, `dispersion_scale()`, and `clip_to_caps()` as separately testable stages; the cap pipeline records *which* cap bound the final size in `SelectorDecision.reasons`
- [ ] Property tests (hypothesis): sizing monotone non-decreasing in edge; exactly zero below `min_net_edge_ppm`; never exceeds any cap or the participation limit for any generated book; never negative-EV-after-fees; `g` monotone non-increasing with `g(0)=1` and `g(ceiling)=0`
- [ ] Example tests with hand-computed Kelly sizes at known probabilities/prices
- [ ] No drive-by changes unrelated to the goal

## Examples

**Property test that should pass after this issue lands:**

```python
@given(inputs=selector_inputs_strategy())
def test_size_never_exceeds_participation_cap(inputs):
    decision = select(inputs)
    for intent in decision.intents:
        resting = resting_depth_at_or_better(inputs.book, intent.limit_price_pips,
                                             intent.outcome)
        assert intent.count_centis * 1_000_000 <= resting * inputs.risk.max_participation_ppm

@given(dispersion=st.integers(min_value=0, max_value=200_000))
def test_dispersion_scaling_monotone(dispersion):
    assert g(dispersion) >= g(dispersion + 1_000)
```

## Constraints

**Scope fence:** Do not implement price-band or execution-style logic (#EPIC_06_ISSUE_04_NUMBER) or correlation-bucket *tagging* (#EPIC_06_ISSUE_05_NUMBER — this issue consumes bucket tags from `SelectorInputs` and enforces the per-bucket cap arithmetic only). Do not modify Kernel-side caps.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges; golden determinism tests still pass (regenerate goldens via the documented script if sizing changes recorded outputs, explaining the diff in the PR).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Sizing pipeline stages documented with SPEC § citations.
- [ ] PR body includes `Refs #EPIC_06_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `selector`
