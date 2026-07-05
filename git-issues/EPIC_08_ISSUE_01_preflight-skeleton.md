## Role

You are a senior Python engineer specializing in credential hygiene and fail-closed operational tooling, working in this repo's `hedgekit/` package (Python ≥3.11, mypy --strict).

## Goal

`hedgekit preflight` runs a typed checklist of production-readiness checks — exchange read-only reachability, trade-key scope validation, jurisdiction eligibility, and secrets hygiene — and exits non-zero if any check is not `PASS`, with every check individually fixture-tested for both pass and fail paths.

## Context

- **Parent epic:** #9
- **Predecessor issue(s):** none — this is the skeleton issue for this epic (it does assume EPIC_07/M6 has merged; see epic Sequencing Notes).
- **SPEC section:** `plans/SPEC_v3.md` §18 M7 ("Production read-only validation; trade-key scope validation; jurisdiction preflight"), §15 (Security: startup-failure conditions), §1.1-3 (withdrawal-capable credentials forbidden system-wide; startup fails if detected), §5.2 (credential boundaries table), §6.2 (`jurisdiction_status`).
- **Files involved:**
  - `hedgekit/preflight/__init__.py` — new package: check definitions + runner (create).
  - `hedgekit/preflight/checks.py` — individual check implementations returning a typed result (create).
  - `hedgekit/main.py` / the CLI entrypoint — register the `preflight` subcommand.
  - `tests/preflight/` — fixture-driven tests per check (create).
- **Prior decisions:** the §5.2 credential-boundary table is load-bearing: preflight itself must run with *read-only* credentials only; it verifies the Gateway's trade key scope via the exchange's scope self-test where the API supports it, never by holding the key outside the Gateway process environment.
- **State of the world:** connector (EPIC_02), Risk Kernel (EPIC_04), Gateway (EPIC_05), and evaluation (EPIC_07) exist and run in PAPER. Nothing validates production readiness yet; `hedgekit preflight` does not exist.

## Output Format

Deliverable is a single PR containing:

- [ ] `hedgekit/preflight/` with a `PreflightReport` model: an ordered list of `PreflightCheck {check_id, description, status: PASS|FAIL|SKIP, detail, spec_ref}` — no free-form-only output.
- [ ] Checks implemented (each independently testable): exchange status + read-only balance fetch succeeds; trade-key withdrawal capability → `FAIL` (hard, §1.1-3); unverifiable key scope where self-tests exist → `FAIL` (§15); jurisdiction status for every screener-eligible market is `"eligible"` (§6.2 — `"unknown"` → `FAIL` + alert); secrets files not world-readable; LLM keys have configured budgets; trade key not readable outside the Gateway process environment.
- [ ] CLI wiring: `hedgekit preflight [--json]`, exit 0 only if every non-SKIP check is PASS.
- [ ] Tests in `tests/preflight/` proving each check's pass AND fail behavior against recorded fixtures/fakes — no live network in CI.
- [ ] Docstrings citing the SPEC § for each check.
- [ ] No drive-by changes unrelated to the goal.

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_withdrawal_capable_key_fails_preflight(fake_exchange_with_withdraw_scope):
    report = run_preflight(exchange=fake_exchange_with_withdraw_scope)
    check = report["credentials.no_withdrawal_scope"]
    assert check.status is CheckStatus.FAIL
    assert report.exit_code == 1  # fail-closed: any FAIL blocks live modes
```

**Example: CLI output shape**

```
$ hedgekit preflight
PASS  exchange.reachable_readonly     Exchange status ok, balances fetched (§7.2)
FAIL  credentials.no_withdrawal_scope Trade key has withdrawal capability (§1.1-3)
SKIP  jurisdiction.markets_eligible   No screener-eligible markets cached
→ preflight FAILED (1 failure); live modes remain unavailable
```

## Constraints

**Scope fence:** Do not implement LIVE_MICRO deployment wiring, the micro-cap enforcement, or the allowlist proxy — that belongs to issue #57. Do not write RUNBOOK/SECURITY docs — that is #60. If you find yourself touching Risk Kernel internals or Gateway submission code, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. `hedgekit run` in PAPER mode must behave exactly as before; preflight is additive. If your change breaks an unrelated CLI surface, you have gone outside scope — revert and re-plan.

**Fail-closed rule (§3.3):** an *errored* check (exception, timeout) reports FAIL, never PASS or silent SKIP.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%; `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #9` and `Closes #56`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `live-micro`
