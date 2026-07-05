## Role

You are a senior Python engineer working in `hedgekit/ledger/`, expert in SQLite (WAL mode), append-only event stores, and hash-chain integrity schemes.

## Goal

An append-only, hash-chained event ledger on SQLite/WAL accepts typed events behind a repository interface, and `hedgekit rebuild` reconstructs read models from events byte-identically, verified by a CI-run equivalence test.

## Context

- **Parent epic:** #2
- **Predecessor issue(s):** #10 (package layout), #11 (config supplies `ops.state_dir`).
- **SPEC section:** plans/SPEC_v3.md §12 (event row: `sequence_number, event_type, created_at, component, payload_json, payload_schema_version, prev_hash, event_hash` with `event_hash = hash(sequence_number || event_type || created_at || payload_json || prev_hash)`; read models rebuildable; Postgres behind the same repository interface later); §1.1-7 (append-only auditability).
- **Files involved:**
  - `hedgekit/ledger/events.py` — base event type + the M0 event set (`ConfigLoaded`, `ModeHeartbeat`, `AlertEmitted` placeholder) with `payload_schema_version` (new).
  - `hedgekit/ledger/store.py` — `LedgerStore` interface + SQLite implementation, append + iterate, chain verification (new).
  - `hedgekit/ledger/rebuild.py` — fold events into read models; `hedgekit rebuild` CLI subcommand (new).
  - `hedgekit/config/loader.py` — swap the in-memory `ConfigEventRecorder` for the real ledger.
  - `tests/ledger/` — chain integrity, tamper detection, rebuild equivalence (new).
- **Prior decisions:** append is the only write; no UPDATE/DELETE statements anywhere in the module (assert via test that scans the SQL). Hash is SHA-256 over the exact §12 concatenation with canonical JSON (sorted keys, no whitespace). Timestamps are UTC ISO-8601 with microseconds. The genesis row's `prev_hash` is 64 zero hex chars.
- **State of the world:** `hedgekit/ledger/__init__.py` is an empty stub; config loader records to an in-memory protocol from issue #11; no `rebuild` subcommand exists.

## Output Format

Deliverable is a single PR containing:

- [ ] SQLite/WAL `LedgerStore` with `append(event) -> sequence_number`, `read_all()`, `verify_chain()`
- [ ] `hedgekit rebuild` subcommand producing read models and exiting non-zero on chain breaks
- [ ] Rebuild-equivalence test: write N mixed events, rebuild twice into temp dirs, assert byte-identical serialized read models
- [ ] Tamper tests: editing any persisted field of any row makes `verify_chain()` fail with the offending sequence number
- [ ] Config loader now ledgering `ConfigLoaded` events with hash + diff
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: chain row (conceptual)**
```
seq=2 event_type=ConfigLoaded prev_hash=ab3f… event_hash=sha256(2||ConfigLoaded||2026-07-04T21:00:00.000001Z||{"config_hash":"…"}||ab3f…)
```

**Example: test case that should pass after this issue lands**
```python
def test_tampered_payload_breaks_chain(store):
    store.append(ModeHeartbeat(seq=1))
    store.append(ModeHeartbeat(seq=2))
    raw_sqlite_update(store.path, row=2, payload_json='{"seq": 999}')
    with pytest.raises(ChainIntegrityError, match="sequence_number=2"):
        store.verify_chain()
```

## Constraints

**Scope fence:** Do not implement backups, restore drills, disk-space halts, or audit-bundle export (§12's operational tail lands with EPIC_07/08 hardening), and do not add Postgres — only the repository interface must permit it. Read models for markets/forecasts/orders arrive with their own epics; M0 read models cover only config versions and heartbeats. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — `hedgekit run` idles and now also ledgers its heartbeats. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines meets the repo threshold (90%).
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #2` and `Closes #13`.
- [ ] Latest `Verdict:` from the Claude reviewer Action on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `foundations`
