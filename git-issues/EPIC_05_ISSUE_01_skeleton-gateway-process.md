## Role

You are a senior Python systems engineer working in this repo's `hedgekit/` package, experienced with process isolation, HMAC/signature verification, and typed state machines under `mypy --strict`.

## Goal

A credential-isolated `hedgekit/order_gateway/` package runs as its own process, verifies Kernel-signed approval tokens (signature, intent-hash match, expiry, single-use), models the full §11.3 order state machine as typed transitions, and routes every "submission" to PaperExchange stubs — with an import-boundary test proving only `order_gateway` may import the exchange order-submission client.

## Context

- **Parent epic:** #6
- **Predecessor issue(s):** none — this is the skeleton issue for this epic. (Cross-epic: requires the M3 Risk Kernel token format and the M1 PaperExchange to be merged.)
- **SPEC section:** `plans/SPEC_v3.md` §11.1–§11.3 (responsibility, requirements, state machine), §10.6 (approval-token fields the Gateway must verify), §5.2 (credential boundaries: trade-only creds + verification key live here), §5.3 (import-boundary CI rule)
- **Files involved:**
  - `hedgekit/order_gateway/__init__.py` — new package; the ONLY package allowed to import the exchange order-submission client
  - `hedgekit/order_gateway/tokens.py` — token verification: signature over canonical serialization, intent-hash match, expiry, single-use ledger
  - `hedgekit/order_gateway/state_machine.py` — §11.3 states and legal transitions as typed enums/functions; illegal transitions raise
  - `hedgekit/order_gateway/gateway.py` — process entrypoint; accepts (intent, token) pairs, verifies, walks state machine, calls stubbed submitter
  - `tests/order_gateway/test_tokens.py` — verification matrix against Kernel-signed fixtures
  - `tests/order_gateway/test_state_machine.py` — every legal and illegal transition
  - `tests/architecture/test_import_boundaries.py` — extend with the order-submission-client rule (or `plans/architecture/.importlinter` if the repo uses import-linter contracts)
- **Prior decisions:** Token fields and canonical serialization are defined by the Risk Kernel epic (§10.6): `{intent_id, market_ticker, outcome, action, limit_price_pips, count_centis, max_fee_micros, expires_at, idempotency_key, config_hash, kernel_sequence_number}`, TTL 60s, single-use. Do not redefine them — import the shared schema and consume Kernel-produced signing fixtures.
- **State of the world:** `hedgekit/` contains the generated scaffold plus M0–M3 output: ledger, fixed-point types, Risk Kernel (signing side), Market Connector, PaperExchange. No `order_gateway` package exists yet.

## Output Format

Deliverable is a single PR containing:

- [ ] New `hedgekit/order_gateway/` package with `tokens.py`, `state_machine.py`, `gateway.py`
- [ ] Stubbed submission: state machine reaches `SUBMISSION_REQUESTED → SUBMITTED → ACKED` against a PaperExchange stub returning typed fake acks
- [ ] Tests in `tests/order_gateway/` proving the token matrix and full transition table
- [ ] Import-boundary test asserting only `order_gateway` imports the exchange order-submission client
- [ ] Docstrings on all public APIs
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test cases that should pass after this issue lands**

```python
def test_valid_token_verifies(kernel_signed_token, matching_intent):
    assert verify_token(kernel_signed_token, matching_intent) is VerifyResult.OK

def test_single_bit_flip_fails(kernel_signed_token, matching_intent):
    for i in range(len(kernel_signed_token.signature) * 8):
        tampered = flip_bit(kernel_signed_token, i)
        assert verify_token(tampered, matching_intent) is not VerifyResult.OK

def test_token_is_single_use(kernel_signed_token, matching_intent):
    verify_and_consume(kernel_signed_token, matching_intent)
    assert verify_token(kernel_signed_token, matching_intent) is VerifyResult.REPLAYED

def test_illegal_transition_raises():
    with pytest.raises(IllegalTransition):
        transition(OrderState.INTENT_CREATED, OrderEvent.FILL)  # must pass APPROVED → … first
```

## Constraints

**Scope fence:** Do not implement real exchange submission, retry logic, crash recovery, the sweeper, or reduce-only checks — those belong to issues #38 through #41. Submission is a stub. Do not touch `hedgekit/riskkernel/` (the signing side) except to import its published token schema. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. `hedgekit run` and all existing surfaces keep working; the Gateway skeleton adds a new process without breaking any other.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines ≥90%; token verification code at 100% branch coverage (SPEC §17.6).
- [ ] `mypy --strict` clean.
- [ ] Public API changes are reflected in docstrings.
- [ ] PR body includes `Refs #6` and `Closes #37`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer action is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `order-gateway`
