## Role

You are a senior Python engineer working in `hedgekit/alerts/` and the logging setup, expert in structured logging, secret hygiene, and pluggable notification sinks.

## Goal

All hedgekit processes log structured lines with secrets redacted by construction, and a sink-agnostic alert layer delivers every SPEC §14 mandatory alert type to configured sinks (ntfy, SMTP, webhook, desktop, log-only fallback), ledgering each emission.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_02_NUMBER (config supplies `alerts.sinks`), #EPIC_01_ISSUE_04_NUMBER (ledger records `AlertEmitted`).
- **SPEC section:** plans/SPEC_v3.md §14 (sink list + mandatory alerts: mode change, halt/kill, veto, reconciliation mismatch, schema anomaly, floor-change request, daily-loss pause, drawdown demotion, fee model unavailable, jurisdiction unknown, canary drift, profit-sweep advisory, backup failure, disk halt); §15 (secrets never in logs); §18 M0 ("structured logging with secret redaction; alert-sink abstraction").
- **Files involved:**
  - `hedgekit/logging_setup.py` — structured (JSON-lines) logging config + redaction filter (new).
  - `hedgekit/alerts/registry.py` — enum of the 14 §14 mandatory alert types; registering a new type requires a severity + description (new).
  - `hedgekit/alerts/sinks.py` — `AlertSink` protocol; ntfy, SMTP, webhook, desktop, log-only implementations; fan-out with per-sink failure isolation (new).
  - `hedgekit/main.py` — install logging at startup.
  - `tests/alerts/`, `tests/test_logging_redaction.py` (new).
- **Prior decisions:** redaction is structural, not regex-only: any log record field named in a denylist (`api_key`, `token`, `secret`, `password`, `authorization`, …) renders as `[REDACTED]`, plus a pattern pass for common key shapes (`sk-…`, `Bearer …`). A failing sink must never raise into the caller — it logs and falls back to log-only. Network sinks are behind the §15 outbound-allowlist idea: sink hosts come only from config.
- **State of the world:** scaffold logging is whatever the generated `main.py` does (plain prints); no alerts package exists; ledger and config from predecessor issues are merged.

## Output Format

Deliverable is a single PR containing:

- [ ] JSON-lines logging with ISO-8601 UTC timestamps, `component` field, and the redaction filter installed process-wide
- [ ] Alert registry covering exactly the §14 mandatory list (no extras, no omissions) with a test asserting the set matches the SPEC
- [ ] Sink implementations + fan-out; emission writes an `AlertEmitted` ledger event including alert type, severity, and sink outcomes
- [ ] Tests: secret-shaped values never appear in captured output; each sink's happy path (mocked transport) and failure isolation; log-only fallback fires when all sinks fail
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: redacted log line**
```json
{"ts":"2026-07-04T21:00:00.000001Z","level":"INFO","component":"config","msg":"loaded","llm_api_key":"[REDACTED]","config_hash":"ab3f12…"}
```

**Example: test case that should pass after this issue lands**
```python
def test_mandatory_alert_registry_matches_spec():
    assert {a.name for a in MandatoryAlert} == SPEC_SECTION_14_ALERTS  # the 14 names, no drift
```

## Constraints

**Scope fence:** Do not implement the conditions that *trigger* these alerts (vetoes, halts, drift detection belong to EPIC_04–EPIC_07) — this issue delivers the transport and registry only, exercised via tests and a hidden `hedgekit alert-test <type>` dev subcommand. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — `hedgekit run` idles, heartbeats now flow through structured logging. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines meets the repo threshold (90%).
- [ ] Public API changes are reflected in docstrings and any user-facing docs.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest `Verdict:` from the Claude reviewer Action on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `foundations`
