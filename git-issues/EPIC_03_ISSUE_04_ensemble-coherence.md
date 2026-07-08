## Role

You are a senior Python engineer with quantitative-modeling experience (probabilistic aggregation, integer fixed-point math), working in this repo's `windbreak/forecast/` package.

## Goal

Independent structured model votes are aggregated by median into `probability_ppm` with `vote_dispersion_ppm` (IQR), each `ModelVote` records pinned provider/model-version/training-cutoff/response-fingerprint, and probabilities within a `mutually_exclusive_group_id` are jointly normalized — with out-of-tolerance raw sums setting `coherence_flag=True` and forcing live-ineligibility (SPEC §8.6–§8.7).

## Context

- **Parent epic:** #4
- **Predecessor issue(s):** #22 (must be merged first). Parallel-safe with #23 and #24.
- **SPEC section:** `plans/SPEC_v3.md` §8.6 (model pinning, fingerprints, temporal integrity), §8.7 (coherence across mutually exclusive outcomes), §6.3 (`ModelVote`, `vote_dispersion_ppm`, `coherence_group_sum_ppm`, `coherence_flag`), §4 T2/T14/T17, §16 `forecast.ensemble` config.
- **Files involved:**
  - `windbreak/forecast/ensemble.py` — vote collection, median aggregation, dispersion (new)
  - `windbreak/forecast/coherence.py` — group normalization + tolerance flagging (new)
  - `windbreak/forecast/records.py` — enforce `coherence_flag=True` ⇒ `eligible_for_live=False` (modify)
  - `windbreak/forecast/pipeline.py` — replace stub vote/aggregation/coherence stages (modify)
  - `tests/forecast/test_ensemble.py`, `tests/forecast/test_coherence.py` (new)
- **Prior decisions:** all math in `ProbabilityPpm` ints — median and IQR computed without float intermediaries; rounding conservative per §6.1. Votes are independent (no tools at vote time, §8.2). Model versions come pinned from config; a missing pin is a construction error, not a warning. Incoherence is treated as confusion, never as an arbitrage prompt (§8.7).
- **State of the world:** vote/aggregation/coherence stages are pass-through stubs emitting fixture values; `ModelVote` exists as a schema with no enforcement.

## Output Format

Deliverable is a single PR containing:

- [ ] Production code in `windbreak/forecast/{ensemble,coherence}.py` + pipeline wiring
- [ ] Tests proving: median of odd/even vote counts correct in ppm; IQR dispersion correct; unpinned model version raises; fingerprint recorded per vote; group sum within tolerance → normalized ppm values summing to 1_000_000 (respecting a residual "other" bucket when configured); out-of-tolerance → every record in the group flagged and live-ineligible
- [ ] Property tests (hypothesis): normalization preserves rank order; output sum always exactly 1_000_000 for exhaustive groups; no float appears in any accounting path (§17.3 AST lint applies)
- [ ] Docstring / doc updates
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_incoherent_group_is_flagged_and_live_ineligible():
    # three mutually exclusive outcomes voted at 60%/60%/30% → raw sum 1.5
    records = forecast_group(votes_ppm={"A": 600_000, "B": 600_000, "C": 300_000},
                             tolerance_ppm=100_000)
    assert all(r.coherence_flag for r in records)
    assert all(not r.eligible_for_live for r in records)
    assert records[0].coherence_group_sum_ppm == 1_500_000
```

```python
def test_median_and_dispersion_are_integer_ppm():
    agg = aggregate_votes([mk_vote(410_000), mk_vote(450_000), mk_vote(520_000)])
    assert agg.probability_ppm == 450_000
    assert isinstance(agg.vote_dispersion_ppm, int)
```

## Constraints

**Scope fence:** Do not implement calibration-map fitting or shrinkage-λ tuning (EPIC_07 / M6 — this issue applies the versioned map and configured λ as given), canary drift (#28), or dutch-book arbitrage detection (post-v1, §19). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — cassette pipeline still green end-to-end.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #4` and `Closes #25`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `forecast-engine`
