## Role

You are a senior Python engineer building reconciliation systems, working in this repo's `windbreak/riskkernel/` package against the Market Connector interfaces from EPIC_02 (M1).

## Goal

Every Kernel cycle independently cross-checks exchange-verified balances and positions (via its own read-only credentials) against the ledger, HALTs on mismatch beyond tolerance, and refuses live trading while any `BalanceSemantics` field is `unknown`.

## Context

- **Parent epic:** #5
- **Predecessor issue(s):** #31 (must be merged first); consumes EPIC_02 (M1) connector fixtures and the `BalanceSemantics` record
- **SPEC section:** `plans/SPEC_v3.md` §10.1 (read-only verification is a Kernel responsibility), §10.4 last paragraph (cross-check every cycle; mismatch beyond tolerance → HALT), §7.3 (balance-semantics contract: "the Risk Kernel refuses live trading while any field is `unknown`"), §5.2 (Kernel: read-only creds), §10.3 (balance/position/open-order reconciliation checks), T18
- **Files involved:**
  - `windbreak/riskkernel/verification.py` — new: periodic verification loop
  - `windbreak/riskkernel/checks.py` — replace balance/position/open-order reconciliation VETO stubs with real checks fed by verification state
  - `windbreak/riskkernel/process.py` — schedule the verification cycle
  - `tests/riskkernel/test_verification.py` — against recorded connector fixtures, including drifted/mismatched ones
- **Prior decisions:** The Kernel holds **read-only** exchange credentials only (§5.2); startup fails if a trade-capable key is readable outside the Gateway (§5.2, §15). Deployable cash = exchange-verified *available* cash per the balance-semantics mapping; unsettled proceeds are excluded until credited (T18). The adapter states whether open-order collateral is already excluded from available balance, to avoid double-counting against reservations (§10.4 comment).
- **State of the world:** Floor math consumes injected fakes for balances. The connector (EPIC_02) provides `get_balances()`, `get_positions()`, `get_open_orders()`, `get_balance_semantics()` plus recorded fixtures. Reservation ledger exists.

## Output Format

Deliverable is a single PR containing:

- [ ] `verification.py`: fetch balances/positions/open orders read-only each cycle, diff against ledger-derived expectations, classify within-tolerance vs breach, ledger every verification result
- [ ] Mismatch beyond tolerance transitions mode → `HALT` and fires an alert via the EPIC_01 alert-sink abstraction
- [ ] `BalanceSemantics` gate: any `unknown` field ⇒ live-mode checks veto (PAPER may proceed); jurisdiction `unknown` raises an alert (§6.2)
- [ ] Floor computation now consumes verification snapshots (with `reconciliation_uncertainty_buffer` reflecting observed drift) instead of raw fakes
- [ ] Fixture tests: clean pass, tolerable drift, breach → HALT, unknown semantics → live veto, stale verification → fail closed
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_balance_mismatch_beyond_tolerance_halts():
    kernel = kernel_with_fixture("balances_drifted_beyond_tolerance.json")
    kernel.run_verification_cycle()
    assert kernel.mode is Mode.HALT
    assert ledger.last_event().event_type == "VERIFICATION_MISMATCH_HALT"

def test_unknown_balance_semantics_blocks_live_only():
    kernel = kernel_with_fixture("semantics_unknown_field.json", mode=Mode.LIVE_MICRO)
    assert kernel.evaluate_intent(make_intent()).vetoed
```

## Constraints

**Scope fence:** Do not implement the Gateway-side Reconciler or order-state healing (EPIC_05 M4). Do not add exchange adapter code — consume EPIC_02 interfaces/fixtures only. Trade-scope credentials must not appear anywhere in this PR. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges: with clean fixtures the Kernel idles verified; with drifted fixtures it HALTs loudly. API down → no trading, never blind trading (§3.3).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] 100% branch coverage on `riskkernel` (§17.6); ≥90% on other changed lines.
- [ ] `mypy --strict` clean; public APIs documented.
- [ ] PR body includes `Refs #5` and `Closes #32`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer Action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `risk-kernel`
