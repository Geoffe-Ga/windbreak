## Role

You are a senior Python engineer specializing in quantitative evaluation pipelines, working in this repo's `windbreak/evaluation/` package (mypy --strict, Python ≥3.11, no floats on probability/money paths per SPEC §6.1).

## Goal

An end-to-end evaluation pipeline skeleton runs over a synthetic known-answer fixture and emits a typed three-track report (forecast quality / selection quality / execution quality, SPEC §13.1) whose every metric slot is a typed stub — and whose forecast-quality track renders "NO EDGE DEMONSTRATED" bluntly when skill ≤ 0.

## Context

- **Parent epic:** #8
- **Predecessor issue(s):** none — this is the skeleton issue for this epic (requires EPIC_06's paper loop to be merged; evaluation reads its ledger events).
- **SPEC section:** `plans/SPEC_v3.md` §13.1 (three tracks), §13.2 ("the dashboard says so bluntly"), §18 M6.
- **Files involved:**
  - `windbreak/evaluation/__init__.py` — new package
  - `windbreak/evaluation/registry.py` — typed metric registry: metric name → computation callable + observation window + track
  - `windbreak/evaluation/resolution.py` — resolution tracker stub (returns fixture resolutions)
  - `windbreak/evaluation/report.py` — three-track report dataclasses + text renderer
  - `tests/evaluation/test_skeleton.py` — smoke tests over the synthetic fixture
  - `tests/evaluation/fixtures/synthetic_known_answer.json` — tiny hand-built dataset: ~10 forecasts with known resolutions and hand-computed expected metric values (used by every later issue)
- **Prior decisions:** integer fixed-point units everywhere on probability paths (`ProbabilityPpm`, SPEC §6.1); ledger read models are the only data source (SPEC §12); the three tracks are never merged into one number (§13.1).
- **State of the world:** `windbreak/` contains only the generated `main.py` hello-world plus whatever earlier epics have landed. No evaluation code exists.

## Output Format

Deliverable is a single PR containing:

- [ ] New `windbreak/evaluation/` package with typed registry, resolution stub, and report renderer
- [ ] Synthetic known-answer fixture checked in under `tests/evaluation/fixtures/`
- [ ] Smoke tests proving: pipeline runs end-to-end on the fixture; report contains exactly three tracks; every registered metric appears with a typed value or an explicit `NOT_IMPLEMENTED` sentinel (never a silent omission); "NO EDGE DEMONSTRATED" renders when Brier skill stub ≤ 0
- [ ] Docstrings on all public API
- [ ] No drive-by changes unrelated to the goal

## Examples

**Test case that should pass after this issue lands:**

```python
def test_three_track_report_renders_no_edge_bluntly() -> None:
    report = run_evaluation(fixture_path=SYNTHETIC_FIXTURE)
    assert {t.name for t in report.tracks} == {"forecast", "selection", "execution"}
    rendered = report.render_text()
    assert "NO EDGE DEMONSTRATED" in rendered  # stub skill is 0
```

## Constraints

**Scope fence:** Do not implement any real metric math (issue #51), real resolution tracking (#50), or temporal-integrity checks (#52). Stubs return typed sentinels. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

**Fixed-point:** probabilities enter the pipeline as `ProbabilityPpm` integers (SPEC §6.1). Derived statistics may use floats internally, but no float may flow back into any accounting/risk path (§17.3).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥ 90%.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #8` and `Closes #49`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `evaluation`
