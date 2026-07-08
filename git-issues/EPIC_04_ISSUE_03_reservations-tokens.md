## Role

You are a senior Python engineer with applied-cryptography and concurrency experience, working in this repo's `windbreak/riskkernel/` package.

## Goal

All approvals serialize through a single-writer reservation ledger, and every approval yields an HMAC-signed, single-use, intent-bound, TTL-bound token whose forgery matrix (replay, mutation, expiry, partial-field forgery, bit-flips) fails verification completely — making floor-breach-via-race (T4) impossible rather than unlikely.

## Context

- **Parent epic:** #5
- **Predecessor issue(s):** #30 (must be merged first)
- **SPEC section:** `plans/SPEC_v3.md` §10.5 (reservations), §10.6 (approval tokens), §5.2 (Kernel holds signing key; Gateway holds verification key), threats T3, T4
- **Files involved:**
  - `windbreak/riskkernel/reservations.py` — new: single-writer reservation ledger
  - `windbreak/riskkernel/tokens.py` — new: token signing over canonical serialization (the ONLY module importing the signing-key handle; import-linter contract from the skeleton issue must cover it)
  - `windbreak/riskkernel/checks.py` — wire in approval-token-uniqueness and idempotency-key-uniqueness checks (§10.3)
  - `windbreak/tokens/verify.py` — new shared verification logic (imported later by the Gateway in EPIC_05; no Gateway code here)
  - `tests/riskkernel/test_reservations.py`, `tests/riskkernel/test_tokens.py`, `tests/riskkernel/test_token_forgery_matrix.py`
- **Prior decisions:** Reservations are created *before* the token is returned; single-use, intent-bound, amount-bound, time-bound, ledgered; released on expiry/cancel/reject/reconciliation; adjusted on partial fill (§10.5). Token = HMAC/signature over canonical serialization of `{intent_id, market_ticker, outcome, action, limit_price_pips, count_centis, max_fee_micros, expires_at, idempotency_key, config_hash, kernel_sequence_number}`; TTL 60s default; single-use (§10.6). Capital is reserved at approval (T4 row, §4).
- **State of the world:** Floor math and real checks exist (#30); approvals are still impossible because reservation/token slots in the pipeline are VETO stubs. Signing-key handle is a stub module from the skeleton issue.

## Output Format

Deliverable is a single PR containing:

- [ ] `reservations.py`: all reserve/release/adjust operations serialized through one writer (thread/process-safe); floor checks read reservations from this ledger; every state change ledgered
- [ ] `tokens.py`: canonical (byte-stable) serialization, HMAC signing, sequence numbers; keys loaded via the keyring/secrets layer from EPIC_01, never from plain config
- [ ] `verify.py`: verification checking signature, intent-hash match, expiry, and single-use
- [ ] Pipeline now issues a signed token when every check passes: reserve → sign → ledger → return token
- [ ] Bit-flip matrix test: flipping ANY single bit of a valid token or any signed field fails verification; replay of a consumed token fails; expired token fails; partial-field forgery fails (§10.12)
- [ ] Concurrency test: N concurrent intents that each individually pass can never jointly over-reserve past the floor (T4)
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: test cases that should pass after this issue lands**

```python
def test_every_bit_flip_fails_verification(valid_token_bytes):
    for i in range(len(valid_token_bytes) * 8):
        assert not verify(flip_bit(valid_token_bytes, i))

def test_concurrent_intents_cannot_jointly_breach_floor():
    # 10 intents, each individually affordable, jointly 5x the budget
    results = approve_concurrently(kernel, intents=10)
    assert reserved_total(kernel) + floor <= worst_case_equity(kernel.state)
```

## Constraints

**Scope fence:** Do not build the Order Gateway or any exchange submission path — EPIC_05 consumes `verify.py` later. Do not implement partial-fill *reconciliation* flows (EPIC_05 reconciler); reservation adjustment must expose the API and unit-test it directly. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges: an intent that passes all checks now yields a real signed token (consumed by nothing yet); vetoes still ledger cleanly.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] 100% branch coverage on `riskkernel` and token packages (§17.6); ≥90% elsewhere.
- [ ] `mypy --strict` clean; standard crypto primitives only (§3.4) — no hand-rolled MACs.
- [ ] PR body includes `Refs #5` and `Closes #31`.
- [ ] Latest `Verdict:` on HEAD from the Claude reviewer Action is `LGTM`.

## Labels

`spec-decomposition`, `core`, `risk-kernel`, `security`
