## Role

You are a senior Python/DevOps engineer working in the hedgekit repo, expert in docker-compose service topology, systemd unit files, and minimal localhost web services.

## Goal

docker-compose and systemd skeletons run processes A–D as separate services sharing only the ledger volume and localhost sockets, and a stub dashboard serves a status page on `127.0.0.1` showing mode and last heartbeat.

## Context

- **Parent epic:** #2
- **Predecessor issue(s):** #10 (four-process packages + run loop). Issues 02–05 are not hard blockers but merge order should follow sequence numbers to keep diffs small.
- **SPEC section:** plans/SPEC_v3.md §5.1 ("Process isolation is mandatory: killing Process A must not kill B or C… docker-compose/systemd deployment runs A, B, C, D as separate services sharing only the ledger volume and localhost sockets"); §14 (dashboard binds `127.0.0.1`, authenticated, no public inbound); §18 M0 (compose + systemd skeletons; stub dashboard).
- **Files involved:**
  - `deploy/docker-compose.yml` — services `pipeline`, `riskkernel`, `order-gateway`, `dashboard`; shared ledger volume; no inter-service network beyond localhost-published ports (new).
  - `deploy/systemd/hedgekit-{pipeline,riskkernel,order-gateway,dashboard}.service` — one unit per process, `Restart=on-failure` (new).
  - `Dockerfile` — minimal image running any process via `hedgekit run --process <name>` (new).
  - `hedgekit/main.py` — `--process {pipeline,riskkernel,order_gateway,dashboard}` flag; non-pipeline processes idle with their own heartbeat component names.
  - `hedgekit/dashboard/app.py` — stdlib/`http.server`-level stub: `GET /` renders mode + last heartbeat read from the ledger; binds `127.0.0.1` only; token auth from config (new).
  - `tests/dashboard/`, `tests/test_process_flag.py` (new).
- **Prior decisions:** dashboard holds no exchange credentials (§5.2) and its allowed mutations are none at M0 — read-only page. Bind address is hardcoded `127.0.0.1`, not configurable (§14 "no public inbound exposure supported"). Compose file must not mount secrets into the pipeline/dashboard containers.
- **State of the world:** no deploy/ directory, no Dockerfile, no dashboard app; `hedgekit run` runs a single process from issue 01 (plus config/ledger/logging from 02–05 as merged).

## Output Format

Deliverable is a single PR containing:

- [ ] Compose file + four unit files matching §5.1 topology; `docker compose config` validates in CI
- [ ] `--process` flag with per-process heartbeat `component` values; killing one local process leaves others running (test via subprocess spawn)
- [ ] Dashboard stub with `127.0.0.1` bind, static token auth (from `alerts`/dashboard config section), mode + heartbeat display, and a 401 path test
- [ ] Smoke tests: each process flag starts and heartbeats; dashboard serves 200 with token and 401 without
- [ ] No drive-by changes unrelated to the goal

## Examples

**Example: process isolation demo**
```
$ docker compose up -d
$ docker compose kill pipeline
$ docker compose ps --format '{{.Name}} {{.State}}'
hedgekit-pipeline exited
hedgekit-riskkernel running
hedgekit-order-gateway running
hedgekit-dashboard running
```

**Example: test case that should pass after this issue lands**
```python
def test_dashboard_requires_token(dashboard_server):
    assert http_get(dashboard_server.url, token=None).status == 401
    assert http_get(dashboard_server.url, token=TEST_TOKEN).status == 200
```

## Constraints

**Scope fence:** Do not implement real dashboard views (positions, equity, calibration — EPIC_06), any mutation endpoints (pause/kill/ack arrive with EPIC_04), or credential wiring for exchange/LLM keys (§15, later epics). The dashboard reads the ledger file read-only. If you find yourself touching files outside the list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges — single-process `hedgekit run` still works with no flags. If your change breaks an unrelated endpoint or CLI surface, you have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh`).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no bypassed checks.
- [ ] Coverage on changed lines meets the repo threshold (90%).
- [ ] Public API changes are reflected in docstrings and any user-facing docs (README deployment note).
- [ ] PR body includes `Refs #2` and `Closes #15`.
- [ ] Latest `Verdict:` from the Claude reviewer Action on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `foundations`
