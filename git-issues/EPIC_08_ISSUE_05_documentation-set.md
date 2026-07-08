## Role

You are a senior technical writer with production trading-systems and security-documentation experience, working at the root of this repo with full read access to `windbreak/` and `plans/SPEC_v3.md`.

## Goal

All eight §19 documents exist at the repo root, are accurate against the shipped code (every referenced command, config key, and procedure actually exists), and the README carries every §19-mandated plain statement — verified by a docs-consistency test that greps referenced CLI commands and config keys against the implementation.

## Context

- **Parent epic:** #9
- **Predecessor issue(s):** #59 (must be merged first — the RUNBOOK documents the drills as they actually behave).
- **SPEC section:** `plans/SPEC_v3.md` §19 (full document list + mandated README statements + RUNBOOK contents), §2 (residual risks that must appear in OPERATOR_WARNINGS.md), §10.7 (floor governance procedures), §12 (audit-bundle export), §16 (config reference to document).
- **Files involved:**
  - `SECURITY.md` — §15 posture: secrets handling, credential boundaries (§5.2 table), network allowlist, reporting a vulnerability.
  - `RUNBOOK.md` — §19's explicit list: start/stop/pause/kill/re-arm; restore from backup; rotate keys; raise floor; request floor lowering; respond to reconciliation-mismatch, canary-drift, and schema-anomaly halts; export audit bundle; export tax records. Each procedure references its drill (`windbreak drill …`) where one exists.
  - `ARCHITECTURE.md` — §5 topology (reuse the mermaid diagram), process isolation, order flow, import-boundary rules.
  - `ACCOUNTING.md` — §6.1 fixed-point units, conservative rounding, §10.4 floor formula, balance semantics (§7.3), settlement lag (T18).
  - `EVALUATION.md` — §13 three tracks, baselines, windows, clustered bootstrap, pre-registration, power analysis, temporal integrity.
  - `LEGAL_AND_COMPLIANCE.md` — jurisdiction/product eligibility (§6.2, §16), sports-block acknowledgement (§1.2), no-investment-advice, record export.
  - `OPERATOR_WARNINGS.md` — §2 residual risks verbatim in substance: exchange insolvency/freeze; resolution disputes/reversals; fee-schedule changes; alpha decay; silent LLM drift; plus the mitigation of last resort (fund only `total risk budget − external floor`).
  - `README.md` — add the §19 plain statements (not investment advice; expect no durable edge — the public claims were a paper portfolio and an unaudited anecdote; paper failure is a valid success state; live trading disabled by default; bounded-loss contracts only; jurisdiction varies; the truest floor is money never deposited).
  - `tests/docs/test_docs_consistency.py` — referenced commands/config keys exist (create).
- **Prior decisions:** measurements outrank narratives (§3.7) — docs must state the "no edge is the default expectation" framing plainly, not bury it. The dashboard mutation forbid-list (§14) and floor-lowering governance (§10.7) must be documented exactly as implemented.
- **State of the world:** README exists with the high-level overview and some §19 statements; the other seven documents do not exist. All features being documented are merged (issues 01–04 and prior epics).

## Output Format

Deliverable is a single PR containing:

- [ ] The seven new documents + README updates, each opening with a one-paragraph scope statement and citing SPEC §s.
- [ ] Every RUNBOOK procedure is numbered, copy-pasteable, and names the exact CLI commands and expected ledger/alert evidence.
- [ ] `tests/docs/test_docs_consistency.py`: every `windbreak <subcommand>` and `config.key` referenced in the docs exists in the CLI registry / config schema — CI fails on drift.
- [ ] No production-code changes (docs + docs-test only).
- [ ] No drive-by changes unrelated to the goal.

## Examples

**Example: RUNBOOK procedure shape**

```markdown
### Respond to a reconciliation-mismatch halt

1. Confirm the halt: `windbreak status` → mode `HALT`, alert `RECONCILIATION_MISMATCH`.
2. Export the evidence bundle: `windbreak audit-bundle --since <event-id>`.
3. Compare exchange truth vs ledger: `windbreak reconcile --dry-run` …
4. Only after the mismatch is explained: `windbreak rearm` (typed confirmation).
   The system will NOT resume on its own — fail-closed is by design (SPEC §3.2).
```

**Example: docs-consistency test that should pass after this issue lands**

```python
def test_runbook_commands_exist(cli_registry):
    for cmd in extract_cli_invocations("RUNBOOK.md"):
        assert cmd.subcommand in cli_registry, f"RUNBOOK references missing command: {cmd}"
```

## Constraints

**Scope fence:** No behavior changes. If documenting a procedure reveals the implementation is wrong or missing, file a bug referencing this issue — do not "fix" code here. Tax *logic* stays out of scope (§1.2): document record export only.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — trivially satisfied (docs-only), but the docs-consistency test must not flake on unrelated changes: parse deliberately, not with brittle regexes over prose.

**Tone constraint (§19):** the README statements are plain declarations, not hedged marketing. "Most operators should expect no durable edge" appears verbatim in substance.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90% (the docs-consistency test module); `mypy --strict` clean.
- [ ] PR body includes `Refs #9` and `Closes #60`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `live-micro`, `documentation`
