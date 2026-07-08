## Role

You are a senior Python engineer working in this repo's `windbreak/selector/` package on portfolio-concentration controls (data modeling + enforcement, with an LLM-assisted tagging pipeline treated as untrusted input).

## Goal

Every market carries structured correlation-driver tags from the seed taxonomy, stored as data and human-overridable; the selector enforces per-bucket exposure caps so that "independent" positions sharing one driver cannot exceed `max_pos_bucket_pct_ppm` (T10), with tagging provenance (LLM-suggested vs. human-set) ledgerable.

## Context

- **Parent epic:** #7
- **Predecessor issue(s):** #45 (must be merged first — the cap-clipping pipeline this issue's bucket arithmetic plugs into). Independent of #46; may land before or after it.
- **SPEC section:** `plans/SPEC_v3.md` §9.9 (correlation buckets, seed taxonomy), §4 row T10, §16 key `max_pos_bucket_pct_ppm`; §20 Open Question 5 (taxonomy governance — do not resolve it here; implement seed list + override mechanics and leave governance to the operator docs)
- **Files involved:**
  - `windbreak/selector/correlation.py` — tag data model, seed taxonomy constants (`us-election`, `fed-policy`, `inflation`, `weather`, `geopolitics-<region>`, `ai-regulation`, `company-specific`, `legal-case`), bucket-exposure aggregation (new)
  - `windbreak/selector/sizing.py` — wire real bucket exposure into the existing per-bucket cap stage
  - `tests/selector/test_correlation_buckets.py`
- **Prior decisions:** tags are *data*, not code — LLM-assisted tagging happens upstream (forecast/screening pipeline) and arrives in `SelectorInputs.correlation_tags`; the selector treats tags as given and never calls an LLM (§9.1 purity). The Kernel enforces the same caps independently (EPIC_04) — defense in depth means this issue must NOT assume the Kernel catches anything, and the Kernel must not assume the selector does; both enforce fully.
- **State of the world:** sizing clips to per-bucket caps but bucket exposure is computed from a placeholder that treats every market as its own bucket (no correlation grouping).

## Output Format

Deliverable is a single PR containing:

- [ ] `correlation.py`: `CorrelationTag` (bucket id, source: `llm | human`, timestamp), parameterized `geopolitics-<region>` handling, exposure aggregation across open positions + pending intents within a bucket
- [ ] Sizing integration: bucket exposure includes existing positions AND the intent being sized, so a passing intent can never tip a bucket over its cap
- [ ] Human-override representation: a human tag for a market supersedes LLM tags for the same market; both retained for the ledger
- [ ] Tests: multi-market same-bucket scenario where the second intent is clipped and the third rejected; override precedence; region-parameterized buckets are distinct (`geopolitics-mideast` ≠ `geopolitics-taiwan`)
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_second_position_in_bucket_is_clipped():
    # Two markets tagged `fed-policy`; bucket cap = 10% of above-floor capital.
    # An existing position consumes 8%; a new intent that would add 5% must be
    # clipped to 2% — and the clip reason must name the bucket.
    decision = select(inputs_with_bucket_exposure("fed-policy", existing_ppm=80_000))
    (intent,) = decision.intents
    assert bucket_exposure_after(intent) <= inputs.risk.max_pos_bucket_pct_ppm
    assert any(r.code == "clipped_by_bucket_cap:fed-policy" for r in decision.reasons)

def test_human_tag_overrides_llm_tag():
    tags = resolve_tags([llm_tag("weather"), human_tag("fed-policy")])
    assert effective_bucket(tags) == "fed-policy"
```

## Constraints

**Scope fence:** Do not implement the LLM tagging pipeline itself (upstream, EPIC_03's screening/forecast surface), Kernel-side bucket enforcement (EPIC_04), or taxonomy governance workflow (§20 OQ5 — operator docs). If you find yourself calling any network or LLM API from selector code, stop: the selector is pure.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges; golden determinism tests still pass (documented regeneration only).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Seed taxonomy and override semantics documented with §9.9 citation.
- [ ] PR body includes `Refs #7` and `Closes #47`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `selector`
