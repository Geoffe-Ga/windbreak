## Role

You are a senior Python engineer with trading-systems risk-control experience, working across this repo's `windbreak/riskkernel/` and `windbreak/order_gateway/` packages (Python ≥3.11, mypy --strict).

## Goal

LIVE_MICRO mode deploys real capital against production APIs while total deployed capital is provably capped at `micro_cap_micros` regardless of every other setting, orders above `require_human_ack_above_micros` block on a ledgered operator acknowledgement, and all outbound traffic is restricted to the configured allowlist.

## Context

- **Parent epic:** #9
- **Predecessor issue(s):** #56 (must be merged first — LIVE_MICRO must be unreachable unless preflight passes).
- **SPEC section:** `plans/SPEC_v3.md` §10.2 ("LIVE_MICRO caps deployed capital at `micro_cap_micros` regardless of all other settings"), §10.8 (human-ack thresholds), §10.9 (PAPER→LIVE_MICRO gate — including the ledgered-override path that caps the system at LIVE_MICRO permanently), §15 (Network: outbound allowlist only), §16 (`capital.micro_cap_micros`, `risk.require_human_ack_above_micros`).
- **Files involved:**
  - `windbreak/riskkernel/` — micro-cap check in the per-order check list (§10.3); human-ack hold state; mode-transition wiring.
  - `windbreak/order_gateway/` — refuse submission while a required ack is pending or lapsed.
  - `windbreak/net/` (or the existing HTTP client layer) — outbound allowlist enforcement at the client-construction boundary.
  - `windbreak/dashboard/`, CLI — ack surfaces (§10.8: dashboard or CLI; ack events ledgered).
  - `tests/riskkernel/`, `tests/gateway/` — property + fixture tests.
- **Prior decisions:** all money math is fixed-point integers (§6.1) — the cap comparison is integer arithmetic with conservative rounding (§17.3). The Kernel is single-writer over reservations (§10.5): the micro-cap check must be computed inside the same serialized reservation path, or T4-style races reappear.
- **State of the world:** `windbreak preflight` exists (issue 01). Mode state machine, floor checks, reservations, and tokens exist from EPIC_04; the Gateway from EPIC_05. LIVE_MICRO is defined in the mode enum but nothing enforces the micro cap or human-ack in a live setting yet.

## Output Format

Deliverable is a single PR containing:

- [ ] Kernel per-order check: `sum(open positions at cost) + pending reservations + worst_case_cost(new order) ≤ micro_cap_micros` in LIVE_MICRO — evaluated inside the serialized reservation ledger.
- [ ] Human-ack flow: orders with `worst_case_cost > require_human_ack_above_micros` enter a held state with expiry; CLI + dashboard ack paths; ack/lapse events ledgered (§10.8).
- [ ] Outbound allowlist: HTTP clients constructible only against the configured allowlist (exchange, LLM providers, search/fetch, alert sinks); any other destination raises before a connection is attempted (§15).
- [ ] Property test: for random order streams and adversarial configs (huge caps elsewhere, floor lowered, ceilings raised), deployed capital in LIVE_MICRO never exceeds `micro_cap_micros`.
- [ ] Tests proving: ack expiry lapses the approval; unacked order never reaches the Gateway; allowlist violation fails closed and is ledgered.
- [ ] No drive-by changes unrelated to the goal.

## Examples

**Example: property test that should pass after this issue lands**

```python
@given(order_streams(), adversarial_configs())
def test_micro_cap_never_exceeded(orders, config):
    kernel = kernel_in_mode(Mode.LIVE_MICRO, config)
    for intent in orders:
        kernel.consider(intent)  # approve or veto
    assert kernel.deployed_capital_micros() <= config.micro_cap_micros
```

**Example: veto reason surfaced on the dashboard**

```
VETO  intent 01J...  micro_cap: worst_case deployment 101_250_000 µ$ >
      micro_cap_micros 100_000_000 µ$ (mode=LIVE_MICRO, SPEC §10.2)
```

## Constraints

**Scope fence:** Do not implement slippage/Brier monitoring — that belongs to issue #58. Do not build new promotion-gate metrics (EPIC_07 owns gate math; you only consume mode state). The dashboard may gain the ack surface only — no other dashboard mutations (§14 allow/forbid lists are fixed).

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges: PAPER mode behavior is untouched, and RESEARCH/PAPER runs must not require any production credential. If your change breaks an unrelated surface, revert and re-plan.

**Safety invariants:** every new check fails closed (§3.3); no float touches the cap math (§6.1, §17.3); the operator's `mode_ceiling` still bounds everything (§1.1-4).

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%; `mypy --strict` clean; mutation score on touched `riskkernel` modules ≥90% (§17.6).
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #9` and `Closes #57`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer GitHub Action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `live-micro`
