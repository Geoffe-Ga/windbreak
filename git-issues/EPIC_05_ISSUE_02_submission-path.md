## Role

You are a senior Python engineer working in this repo's `hedgekit/order_gateway/` package, experienced with idempotent distributed-systems write paths and append-only ledgers.

## Goal

Replace the stubbed submitter with the real submission path: limit orders only, deterministic client order IDs (hash of the intent) making resubmission idempotent, every state transition ledgered before the next action, and submission refused whenever exchange status is paused or unknown — all exercised against PaperExchange.

## Context

- **Parent epic:** #EPIC_05_NUMBER
- **Predecessor issue(s):** #EPIC_05_ISSUE_01_NUMBER (must be merged first — skeleton, token verification, state machine)
- **SPEC section:** `plans/SPEC_v3.md` §11.2 (requirements: limit orders only, deterministic client order IDs, ledger every transition before the next action, refuse when paused/unknown), §11.3 (state machine), §5.3 (order flow), §4 row T3 (runaway order loop)
- **Files involved:**
  - `hedgekit/order_gateway/gateway.py` — replace stub submitter with real path through the connector's `place_order(normalized_intent, approval_token)` / `cancel_order(id)` interface (§7.2)
  - `hedgekit/order_gateway/client_order_id.py` — new: deterministic ID = hash of canonical intent serialization
  - `hedgekit/order_gateway/ledger_writer.py` — transition ledgering helper enforcing write-before-next-action ordering
  - `tests/order_gateway/test_submission.py` — happy path, duplicate submission, paused exchange, unknown status
  - `tests/order_gateway/test_idempotency.py` — same intent resubmitted N times → exactly one exchange order
- **Prior decisions:** Client order ID derivation must be pure and reproducible across process restarts (crash recovery in #EPIC_05_ISSUE_04_NUMBER depends on it to match in-flight orders to intents). Exchange status comes from `get_exchange_status()` (§7.2); maintenance windows suspend submission (§7.4).
- **State of the world:** After the skeleton issue, `gateway.py` verifies tokens and walks the state machine but submits to a typed stub; no ledger writes happen on transitions yet.

## Output Format

Deliverable is a single PR containing:

- [ ] Real submission path in `gateway.py` targeting PaperExchange via the connector interface
- [ ] `client_order_id.py` with property test: equal intents → equal IDs; any field change → different ID
- [ ] Ledger-before-next-action enforced and tested (a transition with a failed ledger write must not proceed)
- [ ] Refusal paths for paused/unknown exchange status, ledgered as explicit events
- [ ] Tests in `tests/order_gateway/` proving idempotent resubmission and refusal behavior
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test case that should pass after this issue lands**

```python
def test_resubmission_is_idempotent(gateway, paper_exchange, approved_intent):
    gateway.submit(approved_intent)
    gateway.submit(approved_intent)          # retry storm / duplicate delivery
    assert paper_exchange.order_count() == 1  # same client order ID → one order

def test_paused_exchange_refuses(gateway, paper_exchange, approved_intent):
    paper_exchange.set_status("paused")
    result = gateway.submit(approved_intent)
    assert result is SubmitResult.REFUSED_EXCHANGE_STATUS
    assert ledger.last_event().event_type == "SUBMISSION_REFUSED"
```

## Constraints

**Scope fence:** Do not implement reduce-only validation (issue #EPIC_05_ISSUE_03_NUMBER), crash recovery / startup reconciliation (issue #EPIC_05_ISSUE_04_NUMBER), or the sweeper (issue #EPIC_05_ISSUE_05_NUMBER). Market orders must be structurally unrepresentable, not merely rejected at runtime. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. The skeleton's token-verification and state-machine surfaces keep passing; only the stub submitter is replaced.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%.
- [ ] `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #EPIC_05_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `order-gateway`
