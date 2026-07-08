## Role

You are a senior Python engineer experienced with hardened web-facing clients (HTTP fetching, search APIs, content extraction) working in this repo's `hedgekit/forecast/sandbox.py` and `triage.py` surfaces.

## Goal

Stage 5's bounded web research runs against the real web — a live `SearchTransport` (hosted search API) and a live `FetchTransport` (HTTP with timeouts and size caps) behind the existing egress allowlist — with real publication-date extraction, and triage stage-0 gets a real pinned cheap model, all cassette/fixture-replayable in CI.

## Context

- **Parent epic:** #183
- **Predecessor issue(s):** #184 (must be merged first). Parallel-safe with #189 and #191.
- **SPEC section:** `plans/SPEC_v3.md` §8.3 (tool boundary — structural, not prompt-level), §8.4 (triage, T11), §8.5 (fetched content is untrusted), §8.8 (publication dates where available), §16 `forecast.triage_model` + `budget.max_pages`.
- **Files involved:**
  - `hedgekit/forecast/providers/search_live.py`, `hedgekit/forecast/providers/fetch_live.py` — live transports implementing the existing `SearchTransport`/`FetchTransport` protocols (new)
  - `hedgekit/forecast/sandbox.py` — no boundary changes; live transports plug into `build_research_tools` exactly like fixtures (reference only; modify only if a seam gap is proven)
  - `hedgekit/forecast/pipeline.py` — `bounded_web_research` stamps real publication dates (replacing the fixed `_CITATION_PUBLICATION_DATE`) with an explicit `None`-handling rule when no date is extractable (modify)
  - `hedgekit/forecast/triage.py` — stage-0 prior from the pinned cheap model via live transport + cassettes (modify)
  - `hedgekit/config/` — search-provider key env name, egress allowlist entries, fetch timeout/size caps, pinned triage model (modify)
  - `tests/forecast/` — recorded search/fetch fixtures; date-extraction tests; triage cassettes (modify/new)
- **Prior decisions:** the allowlist is enforced structurally in `ResearchTools.fetch` — live transports must NOT re-implement policy, they are dumb pipes; a dead link is a skipped citation while an off-allowlist URL raises `EgressDeniedError` (fail-closed) — preserve that distinction exactly; every fetch (including failures) counts against `max_pages`; raw snapshots go to the `ResearchCache`, only sanitized quotes travel onward.
- **State of the world:** transports are fixture-only; `_CITATION_PUBLICATION_DATE` is a hardcoded constant; `run_stage0_prior` parses ppm from a cassette response but no real cheap model has ever been wired.

## Output Format

Deliverable is a single PR containing:

- [ ] Live search transport (pinned provider + key via env) returning candidate URLs; live fetch transport with hard timeout, max-bytes cap, and content-type screening — both raising plain `OSError` subtypes for unreachability so existing skip semantics hold
- [ ] Publication-date extraction (HTML meta/JSON-LD, best-effort) with truthful `None` when absent — never a fabricated date; `Citation.publication_date` handling updated accordingly
- [ ] Triage stage-0 wired to the pinned cheap model from config, cassette-recorded, cost ledgered per §8.4
- [ ] Recorded fixtures proving an end-to-end research pass over real (recorded) pages produces verified citations with real dates
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: allowlist discipline survives live transports**
```python
def test_live_fetch_off_allowlist_fails_closed(recorded_search):
    tools = build_research_tools(search=recorded_search, fetch=live_fetch_replay,
                                 allowlist=("example.com",), cache=cache)
    with pytest.raises(EgressDeniedError):
        tools.fetch("https://evil.example.net/page")
```

**Example: honest dates**
```python
def test_dateless_page_yields_none_publication_date(replay_fixtures):
    citations = bounded_web_research(subqs, tools=tools_over(replay_fixtures))
    assert citations[0].publication_date is None  # never a fabricated constant
```

## Constraints

**Scope fence:** Do not alter the sandbox policy surface (allowlist checks, cache path rules — #24's design stands). No retry policy (#193). No vote-stage changes. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** CI stays network-free (replay fixtures only); the injection corpus — which attacks exactly this fetched-content path — must stay green with the live-transport code shape in place.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`), including the full injection corpus.
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes; float-lint clean.
- [ ] Secret-scanning clean; recorded fixtures scrubbed.
- [ ] PR body includes `Refs #183` and `Closes #192`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `forecast-engine`, `security`
