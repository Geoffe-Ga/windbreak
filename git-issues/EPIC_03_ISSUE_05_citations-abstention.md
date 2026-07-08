## Role

You are a senior Python engineer working in this repo's `windbreak/forecast/` package, experienced with content verification pipelines and evidence-grade data handling.

## Goal

Every citation is verified at forecast time (URL reachability, retrieved-content hash, quoted-text presence, publication date where available, source type), records below `min_verified_citations` are stored but live-ineligible, and the engine can abstain with a first-class `abstention_reason` that downstream evaluation scores rather than drops (SPEC §8.8).

## Context

- **Parent epic:** #4
- **Predecessor issue(s):** #24 (must be merged first — verification runs through the sandbox's `verify_citation` capability and evidence lands in the research cache).
- **SPEC section:** `plans/SPEC_v3.md` §8.8 (citation verification & abstention), §6.3 (`citations`, `source_quality_notes`, `abstention_reason`, `eligible_for_live`), §16 `forecast.min_verified_citations` (default 3), §13.3 (abstentions evaluated counterfactually — downstream consumer).
- **Files involved:**
  - `windbreak/forecast/citations.py` — verification checks + verdict per citation (new)
  - `windbreak/forecast/records.py` — live-eligibility rule: verified-citation count ≥ config minimum (modify)
  - `windbreak/forecast/pipeline.py` — abstention path produces a schema-valid record with `abstention_reason` set and no probability requirements relaxed elsewhere (modify)
  - `tests/forecast/test_citations.py`, `tests/forecast/test_abstention.py` (new)
- **Prior decisions:** verification evidence (raw snapshot + hash) is stored in the research cache, never inline in the ledger; a citation whose quoted text is absent from the retrieved content is *unverified*, not an error; abstention is a terminal, scored outcome — not an exception path.
- **State of the world:** `Citation` schema exists from the skeleton; verification is stubbed to always-verified; abstention is unreachable.

## Output Format

Deliverable is a single PR containing:

- [ ] Production code in `windbreak/forecast/citations.py` + record/pipeline wiring
- [ ] Tests proving: each verification check (reachability, content hash, quote presence, pub date, source type) evaluated independently on fixtures; count < `min_verified_citations` ⇒ record stored with `eligible_for_live=False`; abstention produces a valid record with `abstention_reason` and `eligible_for_live=False`; abstained records are ledgered like any forecast
- [ ] Fixture set covering: dead URL, changed content (hash mismatch), quote absent, all-green citation
- [ ] Docstring / doc updates
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_below_min_verified_citations_is_stored_but_live_ineligible(pipeline_env):
    record = run_pipeline_with_citations(pipeline_env, verified_count=2)  # min is 3
    assert record.eligible_for_live is False
    assert ledger_contains(record.forecast_id)

def test_quote_absent_from_fetched_content_fails_verification(sandbox_tools):
    verdict = verify_citation(sandbox_tools, url=FIXTURE_URL,
                              quoted_text="text that is not on the page")
    assert verdict.verified is False
    assert verdict.failure == "quote_not_found"
```

## Constraints

**Scope fence:** Do not implement source-reliability *scoring* (post-v1, §18 M8) or the evaluation-side counterfactual scoring of abstentions (EPIC_07 / M6). Do not touch injection-defense content sanitization (#27). If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — the all-green-citation fixture path still yields a live-eligible record end-to-end.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #4` and `Closes #26`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `forecast-engine`
