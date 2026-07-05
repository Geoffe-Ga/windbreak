## Role

You are a senior Python engineer with security-engineering experience (process isolation, capability-based tool design), working in this repo's `hedgekit/forecast/` package.

## Goal

The research stage can reach exactly three capabilities — `search`, `fetch`, and citation verification against an outbound allowlist — and is structurally incapable (not prompt-incapable) of touching the ledger, config, balances, positions, order books past the baseline snapshot, risk/order APIs, shell, or any filesystem path outside its dedicated research cache (SPEC §8.3).

## Context

- **Parent epic:** #4
- **Predecessor issue(s):** #22 (must be merged first). Independent of #23 (triage) — parallel-safe.
- **SPEC section:** `plans/SPEC_v3.md` §8.3 (research tool boundary, "enforced structurally, not by prompt"), §1.1-5 (research/execution firewall), §15 (research cache disjoint from config/ledger/secrets/code; outbound allowlist), §5.3 CI import-boundary note.
- **Files involved:**
  - `hedgekit/forecast/sandbox.py` — tool registry, allowlist enforcement, research-cache path jail (new)
  - `hedgekit/forecast/pipeline.py` — research stage acquires tools only through the sandbox (modify)
  - `plans/architecture/.importlinter` — contract: `hedgekit.forecast` may not import ledger-write, connector-order, kernel, or gateway modules (modify)
  - `tests/forecast/test_sandbox.py` — boundary tests (new)
- **Prior decisions:** the boundary is structural: the research stage receives a `ResearchTools` object exposing only the three allowed capabilities; there is no ambient import path to forbidden modules (enforced by import-linter in CI, §5.3). Network egress goes through a single client that rejects non-allowlisted hosts. Fetched content lands only under the configured research cache dir.
- **State of the world:** pipeline skeleton exists; the research stage is a stub that reads cassette data directly with no tool abstraction and no isolation.

## Output Format

Deliverable is a single PR containing:

- [ ] Production code in `hedgekit/forecast/sandbox.py` + research-stage wiring
- [ ] Import-linter contract update in `plans/architecture/.importlinter` that fails CI on a violating import
- [ ] Tests in `tests/forecast/test_sandbox.py` proving: non-allowlisted host fetch raises; write outside research cache raises; the tool registry exposes exactly {search, fetch, verify_citation}; the research stage cannot obtain a ledger/config/connector handle through the sandbox
- [ ] Docstrings documenting the boundary and why it exists (cite §8.3)
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_fetch_rejects_non_allowlisted_host(sandbox_tools):
    with pytest.raises(EgressDenied):
        sandbox_tools.fetch("https://evil.example.com/page")

def test_research_cache_is_a_jail(sandbox_tools, tmp_path):
    with pytest.raises(SandboxPathViolation):
        sandbox_tools.store_evidence(Path("~/.local/share/hedgekit/ledger.db"), b"x")

def test_tool_surface_is_exactly_three(sandbox_tools):
    assert public_capabilities(sandbox_tools) == {"search", "fetch", "verify_citation"}
```

## Constraints

**Scope fence:** Do not implement injection-defense content handling (delimiting, quote extraction — #27) or citation verification *logic* (#26; this issue only reserves the capability slot). OS-level namespace isolation may land as a documented follow-up if the process-level structural boundary is complete and CI-enforced. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — the cassette-driven pipeline must still run offline (cassette replay counts as allowlisted).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes.
- [ ] Import-boundary check (`plans/architecture/run-check.sh`) passes and demonstrably fails on a seeded violation.
- [ ] PR body includes `Refs #4` and `Closes #24`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `forecast-engine`, `security`
