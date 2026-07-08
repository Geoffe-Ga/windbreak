## Role

You are a senior Python engineer working in `windbreak/config/`, expert in typed configuration modeling (pydantic or dataclasses under mypy --strict) and fail-fast validation.

## Goal

`windbreak run --config <path>` loads the complete SPEC §16 YAML schema into typed objects, rejects any unknown key with a fatal error naming the key path, and produces a canonical hash + human-readable diff for every loaded version.

## Context

- **Parent epic:** #2
- **Predecessor issue(s):** #10 (must be merged first — package layout and CLI exist).
- **SPEC section:** plans/SPEC_v3.md §16 (full schema: `mode_ceiling`, `exchange`, `capital`, `risk`, `screener`, `forecast`, `evaluation`, `ops`, `alerts`); §1.1-4 (`mode_ceiling` can never be exceeded); §10.7 (floor raise/lower asymmetry — loader only models the fields; governance logic is EPIC_04).
- **Files involved:**
  - `windbreak/config/schema.py` — typed models for every §16 section (new).
  - `windbreak/config/loader.py` — YAML load, unknown-key rejection, canonical serialization, `config_hash` (new).
  - `windbreak/main.py` — accept `--config`, load before entering the run loop.
  - `tests/config/` — schema, unknown-key, hash/diff tests (new).
- **Prior decisions:** integer units only — ppm/micros/pips fields are `int`; no float fields anywhere in config (§6.1). Unknown keys are fatal, not warnings (§16 heading). Ledgering the version is a hook: emit a `ConfigLoaded(config_hash, diff)` event via a narrow interface that issue #13 will back with the real ledger; land it here as an in-memory recorder protocol.
- **State of the world:** `windbreak/config/__init__.py` is an empty stub from the skeleton issue; CLI has no `--config` flag; heartbeat interval is a CLI flag.

## Output Format

Deliverable is a single PR containing:

- [ ] Typed models covering every key in the §16 example verbatim, with the §16 defaults as model defaults
- [ ] Loader raising `ConfigError` (exit code ≠ 0 from CLI) on unknown keys, wrong types, or missing file
- [ ] Canonical serialization → SHA-256 `config_hash`; `diff_configs(old, new)` returning key-path-level changes
- [ ] `ConfigEventRecorder` protocol + in-memory implementation, called with hash + diff on every load
- [ ] Tests: full example loads; each section rejects an injected unknown key; hash is stable across key order; diff reports exactly the changed paths
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: unknown key is fatal**
```
$ windbreak run --config bad.yaml
FATAL: unknown configuration key: risk.max_leverage (unknown keys are fatal per SPEC §16)
$ echo $?
1
```

**Example: test case that should pass after this issue lands**
```python
def test_unknown_key_is_fatal():
    with pytest.raises(ConfigError, match=r"risk\.max_leverage"):
        load_config(yaml_fixture_with({"risk": {"max_leverage": 2}}))
```

## Constraints

**Scope fence:** Do not implement floor-lowering governance, cool-offs, or mode transitions (EPIC_04 / Risk Kernel), and do not write to a real ledger (issue #13). Do not add config keys absent from §16. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — `windbreak run` without `--config` must still idle with heartbeats using defaults. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines meets the repo threshold (90%).
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #2` and `Closes #11`.
- [ ] Latest `Verdict:` from the Claude reviewer Action on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `foundations`
