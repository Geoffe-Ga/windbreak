# Order Gateway chaos suite (issue #42)

Proves SPEC S11.5: with the Order Gateway killed at every state edge, network
cut mid-submit, duplicate ACKs, out-of-order fills, missed fills, and
cancel/fill races injected over `PaperExchange`, the system always converges
to a state where four invariants hold (`tests/chaos/invariants.py`):

1. zero duplicate live orders,
2. zero orders without valid tokens,
3. zero net-short positions,
4. correct reservation release (every reservation released or consumed
   exactly once).

A fail-closed **HALT counts as convergence** -- several scenarios below
deliberately assert `run.halted is True` and that the invariants *still*
hold in that halted state (e.g. an `"ambiguous_match"`/`"vanished_order_no_fill"`
reconciliation halt legitimately leaves an order unresolved, but flagged).

## Running the suite

```bash
pytest tests/chaos -m chaos -v
```

CI runs the chaos suite as its own required job: `pytest -m chaos --no-cov`
(coverage is enforced separately by the full-suite quality job, so the chaos
job itself never needs `--cov`). The chaos suite currently also collects
under the default `./scripts/test.sh --unit` selection (it carries no
`integration`/`e2e` marker), so it contributes to the standard coverage run
too.

## Reproducing a failed seed

Every source of randomness in this suite is seeded and the seed is surfaced
on failure (guiding principle SPEC S3.5), so any chaos failure reproduces
deterministically:

### The fixed-seed storm

`test_deterministic_fault_storm_preserves_invariants` is parametrized over
the committed `CHAOS_SEEDS` tuple; the seed is baked into the pytest test id.
Reproduce one failing seed directly:

```bash
pytest tests/chaos -m chaos -k "test_deterministic_fault_storm_preserves_invariants and 7"
```

### The Hypothesis storm

`test_hypothesis_fault_storm_preserves_invariants` draws an integer seed via
`@given(seed=...)` and derives a fully deterministic intent stream and fault
combination from it (`random_intent_stream`/`random_faults`). On failure,
Hypothesis prints the shrunk failing `seed` and (because `print_blob=True`)
a `@reproduce_failure(...)` blob you can paste directly above the test to
pin that exact example. Two equivalent ways to reproduce:

```bash
# Re-run with the exact same example sequence Hypothesis explored:
pytest tests/chaos -m chaos -k test_hypothesis_fault_storm --hypothesis-seed=<n>

# Or paste the printed blob directly above the test function:
# @reproduce_failure('<hypothesis version>', b'...')
```

### A single named scenario

Every individual-family test (kill-at-every-edge, network-cut-mid-submit,
duplicate-ACK, out-of-order-fills, missed-fill, cancel/fill-race) is fully
deterministic on its own -- no seed needed, just run it directly:

```bash
pytest tests/chaos -m chaos -k test_kill_at_every_state_edge_converges -v
```

The kill-at-every-edge family's test ids name the exact durable-write edge
(`after-wal-intent`, `after-exchange-place-pre-wal-ack`, ...), reusing
`tests/order_gateway/test_recovery.py`'s own `_KILL_MATRIX` taxonomy.

## Budgets are floors, not knobs

The whole suite targets **~60 seconds** wall-time (in-memory/`tmp_path`
only), the Hypothesis storm is pinned at `max_examples=25` with
`deadline=None`, and the fixed storm at 10 seeds. **Never lower these to
hide a failure** -- fix the root cause (a genuine Gateway bug, per the
"Do not modify Gateway production code" scope fence, gets a separately
reviewable fix commit citing the failing seed) or, if a budget is genuinely
insufficient for a new scenario, raise it explicitly and say why in the PR,
never silently.

## Fault taxonomy

`tests/chaos/conftest.py`'s `FaultSpec` names a small, composable set of
faults, each installed at exactly one `OrderGateway` (or Reconciler/Sweeper)
constructor seam -- `submitter`, `wal`, `ledger_writer`, `status_source`,
`reconciliation_source`, `clock`. Two are process-local (cleared on the
harness's simulated restart: `kill_after`, `network_cut`, `duplicate_ack`,
`clock_skew`) and two are environmental (persist across the restart into the
Reconciler/Sweeper too: `reorder_fills`, `drop_fills`, `exchange_paused`).
See `ChaosHarness.run`'s docstring for the full contract.
