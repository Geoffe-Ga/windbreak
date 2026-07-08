## Role

You are a senior Python engineer experienced with hardened third-party API integrations (typed HTTP clients, secret handling, record/replay testing), working in this repo's `hedgekit/forecast/providers/` package.

## Goal

A `FutureSearchProvider` turns a normalized market question into a calibrated probability with rationale, citations, and API-reported cost — as one ensemble member of the research-forecaster family — with every HTTP exchange recorded to cassettes so CI runs the adapter offline and byte-deterministically.

## Context

- **Parent epic:** #183
- **Predecessor issue(s):** #184 (must be merged first — the `ForecastProvider` seam and integer-ppm vote schema are this adapter's contract).
- **SPEC section:** `plans/SPEC_v3.md` §8.6 (pinning, provenance, T14), §8.5 (all returned text is untrusted), §8.8 (citations), §16 budget block. External: https://futuresearch.ai/docs/api/ and https://futuresearch.ai/docs/getting-started/ — `forecast()` returns per-question probabilities + rationale (binary) and p10/p50/p90 (numeric); `multi_agent()` costs $0.30–$2/question (300_000–2_000_000 micros, inside `per_forecast_micros: 3_000_000`); probabilities are clamped to [3%, 97%].
- **Files involved:**
  - `hedgekit/forecast/providers/futuresearch.py` — the adapter (new)
  - `hedgekit/forecast/providers/http_cassettes.py` — HTTP-level record/replay mirroring `cassettes.py` semantics (canonical JSON, float rejection, miss-fails-closed) if the existing `LlmTransport` cassette shape doesn't fit (new, only if needed)
  - `hedgekit/config/` — provider entry: pinned endpoint/forecaster version, API-key env var name (`FUTURESEARCH_API_KEY`), per-call cost ceiling (modify)
  - `hedgekit/forecast/pipeline.py` — thread the provider's citations into the record's citation path (modify, minimal)
  - `tests/forecast/providers/test_futuresearch.py` + recorded cassettes under `tests/forecast/fixtures/` (new)
- **Prior decisions:** floats never enter the probability path — convert the API's probability to integer ppm at the adapter boundary, immediately, with explicit rounding (document the rounding rule); secrets are env-only and redacted by the M0 logging layer; all provider text (rationale, citation quotes) passes through `sanitize.py` screens before any prompt or record field; invalid/unparseable responses are discarded + ledgered, never repaired.
- **State of the world:** after #184 the seam exists with only fixture providers behind it. No live HTTP code exists anywhere in `forecast/`.

## Output Format

Deliverable is a single PR containing:

- [ ] `FutureSearchProvider` implementing `ForecastProvider`: question + resolution criteria + close time in, `ProviderForecast` out (integer ppm, rationale summary, citations with URLs + publication dates where given, API-reported cost in micros)
- [ ] Provenance pinning: the API-reported forecaster/model version lands in `ModelVote.model_version`; a version the config doesn't pin → configurable warn-or-reject
- [ ] Cassette recording script (developer-run, real key) + replay-only CI: a cassette miss raises, exactly like `ReplayCassette`
- [ ] All returned text injection-screened; a screened-out response is discarded + ledgered
- [ ] Cost accounting: reported cost charged against `ResearchBudget`; missing/unparseable cost → charge the configured ceiling (never undercount)
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: adapter boundary**
```python
def test_futuresearch_probability_converts_to_integer_ppm(replay_cassette):
    provider = FutureSearchProvider(transport=replay_cassette, config=PINNED)
    result = provider.forecast(question)
    assert result.probability_ppm == 340000          # 34% → 340_000 ppm, int
    assert 30000 <= result.probability_ppm <= 970000  # API clamps to [3%, 97%]
    assert result.cost_micros > 0
```

**Example: fail-closed on drift**
```python
def test_unpinned_forecaster_version_is_rejected_when_strict(replay_cassette):
    provider = FutureSearchProvider(transport=replay_cassette, config=STRICT_PIN)
    with pytest.raises(ProviderVersionDriftError):
        provider.forecast(question)
```

## Constraints

**Scope fence:** One provider only. No retry/quorum policy (that is #193), no track-record gating (#194), no changes to `LlmTransport` LLM-vote paths (#191). New dependency (SDK vs. thin client over the repo's existing HTTP lib) requires a dependency-review note in the PR; prefer the thinnest pinned surface. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** CI never makes a network call — replay cassettes only; the offline fixture pipeline from #184 keeps passing untouched. Honesty: the adapter must surface the provider's own uncertainty (clamped tails, abstain signals) — never sharpen it.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`), including the injection corpus with FutureSearch-shaped responses added to it.
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%; `mypy --strict` passes; float-lint clean (boundary conversion is the only float contact, contained in one function).
- [ ] Secret-scanning clean; no key material in cassettes (record script must scrub auth headers).
- [ ] PR body includes `Refs #183` and `Closes #189`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `forecast-engine`
