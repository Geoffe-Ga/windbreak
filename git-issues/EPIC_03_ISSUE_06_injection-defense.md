## Role

You are a senior Python engineer with adversarial-ML/security experience (prompt-injection hardening, untrusted-content pipelines), working in this repo's `windbreak/forecast/` package.

## Goal

Fetched web content is handled as untrusted data end-to-end — wrapped in delimited data blocks, scripts/hidden text stripped, synthesis prompts receiving only extracted quotes ≤ 25 words with URLs, invalid structured output discarded-and-ledgered (never repaired by a more privileged call) — and a CI adversarial corpus of poisoned pages proves zero effect on anything but probability/rationale fields and zero tool calls outside the allowlist (SPEC §8.5, T1).

## Context

- **Parent epic:** #4
- **Predecessor issue(s):** #24 and #26 (must be merged first — sandbox boundary and citation path are the surfaces under attack).
- **SPEC section:** `plans/SPEC_v3.md` §8.5 (prompt-injection defense), §4 T1 (threat + mitigation row), §1.1-5 (firewall invariant), §17.1 (prompt-injection suite is CI-gating), §8.9 ("injection corpus green" acceptance).
- **Files involved:**
  - `windbreak/forecast/sanitize.py` — data-block wrapping, script/hidden-text stripping, quote extraction ≤ 25 words (new)
  - `windbreak/forecast/pipeline.py` — synthesis stages consume only sanitized quote sets; invalid model output → discard + ledger event (modify)
  - `tests/forecast/injection/corpus/` — poisoned-page fixtures: embedded instructions, fake JSON, role impersonation, tool-call lures (new)
  - `tests/forecast/injection/test_injection_corpus.py` — the release-blocker suite (new)
- **Prior decisions:** raw source snapshots are stored separately as evidence (research cache) and never enter prompts; "repair" of malformed model output by re-asking a more privileged model is forbidden — discard and ledger; corpus failures block release (CI-gating, not advisory).
- **State of the world:** sandbox and citation verification exist; fetched content currently flows to synthesis unsanitized in the fixture path; no adversarial corpus.

## Output Format

Deliverable is a single PR containing:

- [ ] Production code in `windbreak/forecast/sanitize.py` + pipeline wiring
- [ ] Adversarial corpus (≥ 8 poisoned pages spanning: direct instructions ("buy X", "ignore limits"), fake tool-call JSON, role impersonation, hidden text/CSS tricks, oversized quotes, citation-URL spoofing)
- [ ] Corpus test asserting, for every page: resulting record differs from a clean-page baseline only in probability/rationale/citation fields; zero tool invocations outside {search, fetch, verify_citation}; zero writes outside the research cache
- [ ] Test proving invalid structured output is discarded and a ledger event written — with no retry at elevated privilege
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
@pytest.mark.parametrize("page", load_corpus())
def test_poisoned_page_cannot_escape_probability_fields(page, pipeline_env):
    baseline = run_pipeline_on(pipeline_env, CLEAN_PAGE)
    attacked = run_pipeline_on(pipeline_env, page)
    assert non_probability_surface(attacked) == non_probability_surface(baseline)
    assert pipeline_env.tool_log.calls_outside({"search", "fetch", "verify_citation"}) == []
```

```python
def test_invalid_model_output_is_discarded_not_repaired(pipeline_env):
    run_pipeline_with_malformed_vote(pipeline_env)
    assert pipeline_env.ledger.has_event("FORECAST_OUTPUT_DISCARDED")
    assert pipeline_env.llm_log.privileged_retry_count == 0
```

## Constraints

**Scope fence:** Do not modify the sandbox tool registry (#24 owns it) or citation verification logic (#26). Corpus pages are fixtures — no live URLs. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — clean-page fixtures still produce live-eligible records; any corpus escape is a release blocker, so the suite must be green before merge, not quarantined.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`), including the full injection corpus.
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #4` and `Closes #27`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `forecast-engine`, `security`
