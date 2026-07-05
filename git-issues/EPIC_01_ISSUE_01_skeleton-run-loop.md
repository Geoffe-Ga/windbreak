## Role

You are a senior Python engineer working in the `hedgekit` repo, experienced in structuring multi-process daemons with clean package boundaries (Python ≥3.11, mypy --strict).

## Goal

`hedgekit run` starts, enters RESEARCH mode, and emits a heartbeat log line at a fixed interval until interrupted — with the full four-process package skeleton in place so every later issue has a home.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** none — this is the skeleton issue.
- **SPEC section:** plans/SPEC_v3.md §18 M0 ("`hedgekit run` idles in RESEARCH with visible heartbeats"); §5.1 component & trust topology; §10.2 mode names (`RESEARCH → PAPER → LIVE_MICRO → LIVE`).
- **Files involved:**
  - `hedgekit/main.py` — currently the generated hello-world; becomes the CLI entrypoint (`hedgekit run`).
  - `hedgekit/pipeline/__init__.py` — Process A stub (market connector / screener / forecast / selector homes).
  - `hedgekit/riskkernel/__init__.py` — Process B stub.
  - `hedgekit/order_gateway/__init__.py` — Process C stub.
  - `hedgekit/dashboard/__init__.py` — Process D stub.
  - `hedgekit/ledger/__init__.py`, `hedgekit/config/__init__.py`, `hedgekit/numeric/__init__.py`, `hedgekit/alerts/__init__.py` — shared-component stubs.
  - `tests/test_main.py` — replace hello-world test with run-loop smoke tests.
- **Prior decisions:** process isolation is mandatory (§5.1); only `order_gateway` may ever import the exchange order-submission client and only `riskkernel` the signing key handle (§5.3) — the package layout must make that import-boundary rule expressible later.
- **State of the world:** fresh Start Green Stay Green scaffold; `hedgekit/main.py` prints "Hello from hedgekit!"; no subpackages exist.

## Output Format

Deliverable is a single PR containing:

- [ ] Subpackages listed above, each with a module docstring stating its SPEC role and credential boundary (§5.2)
- [ ] A `hedgekit run` CLI (argparse or typer, whichever `requirements.txt` already carries) that logs `mode=RESEARCH heartbeat seq=<n>` at a configurable interval (default 5s) and exits cleanly on SIGINT/SIGTERM
- [ ] Smoke tests in `tests/` proving: CLI parses, heartbeat emits ≥2 beats with monotonic `seq`, clean shutdown
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: expected terminal output**
```
$ hedgekit run --heartbeat-interval 1
2026-07-04T21:00:00Z INFO hedgekit mode=RESEARCH heartbeat seq=1
2026-07-04T21:00:01Z INFO hedgekit mode=RESEARCH heartbeat seq=2
^C
2026-07-04T21:00:02Z INFO hedgekit shutdown reason=SIGINT
```

**Example: test case that should pass after this issue lands**
```python
def test_heartbeat_emits_monotonic_sequence(capsys):
    run_loop(interval_seconds=0, max_beats=3)
    seqs = [parse_seq(line) for line in capsys.readouterr().err.splitlines() if "heartbeat" in line]
    assert seqs == [1, 2, 3]
```

## Constraints

**Scope fence:** Do not implement config loading (issue #EPIC_01_ISSUE_02_NUMBER), ledger writes (#EPIC_01_ISSUE_04_NUMBER), or any exchange/LLM code. Heartbeat interval comes from a CLI flag, not the §16 config file. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines meets the repo threshold (90%).
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` from the Claude reviewer Action on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `foundations`
