# hedgekit

An open-source, locally hosted, always-on AI forecast trader for fully collateralized binary event markets (e.g., Kalshi).

**Status:** Pre-implementation scaffold. Building against [`plans/SPEC_v3.md`](plans/SPEC_v3.md).

**License target:** Apache-2.0

## What this is

`hedgekit` is a local-first daemon that:

1. Screens prediction markets for questions where careful research can plausibly beat the crowd.
2. Uses an LLM "superforecaster" scaffold to produce calibrated probability estimates with verified citations.
3. Compares those estimates against live executable order books.
4. May create order intents — none of which can reach an exchange unless approved by an independent, veto-holding **Risk Kernel** and submitted through a token-verifying, credential-isolated **Order Gateway**.

The design descends from publicly documented AI-forecasting pipelines (screen by volume → exclude information-disadvantaged categories → deep LLM research → trade only where forecast and executable price disagree beyond fees). It deliberately does **not** assume the headline results around those pipelines are reproducible — widely cited figures come from a paper portfolio with no commissions/borrow costs/dividends, and a self-reported anecdote whose own author says the edge is already competed away.

## Important disclaimers

- **This is not investment advice.**
- Most operators should expect **no durable edge**. Discovering that and stopping at paper trading is a **success state** of this design, not a failure of the software.
- **Live trading is disabled by default.** Promotion from paper to live trading is gated by pre-registered, quantitative evidence — never by narrative or operator impatience.
- Only bounded-loss, fully collateralized binary event contracts are in scope. No margin, perps, options, leverage, or shorting-to-open.
- Legal eligibility to trade these products varies by jurisdiction and product; this software does not provide legal advice.
- The truest floor is money never deposited: fund the exchange only with risk capital, keep floor capital in an unlinked account, and grant only trade-scope API keys.

## Core invariants

1. **Floor Invariant** — worst-case equity must always be ≥ a configured floor, computed conservatively and enforced pre-trade by an independent process. Any unprovable input halts the system.
2. **Bounded-loss instruments only** — fully collateralized binary contracts with exactly known maximum loss at order time. Margin, perps, options, leverage, and equities/crypto spot/derivs are forbidden, not configurable.
3. **No trade credentials outside the Order Gateway** — research, forecasting, selection, and dashboard components never hold trade-capable credentials.
4. **Evidence-gated autonomy** — `RESEARCH → PAPER → LIVE_MICRO → LIVE`, with promotion by pre-registered quantitative gates only; demotion and halting are automatic.
5. **Research/execution firewall** — web content and model output can influence only the probability fields of a forecast; never config, credentials, routing, or control flow.
6. **Temporal integrity** — only real-time forecasts on then-unresolved questions count toward gates, guarding against LLM training-data leakage from backtests.
7. **Append-only auditability** — every snapshot, forecast, decision, veto, approval, order transition, and reconciliation is written to a hash-chained ledger.

## Architecture

Four isolated processes sharing only a ledger volume and localhost sockets:

- **Process A — Main pipeline** (no trade credentials): Market Connector → Screener → Forecast Engine → Trade Selector.
- **Process B — Risk Kernel** (read-only exchange credentials + signing key): independent veto authority over mode, floor enforcement, capital reservations, and approval tokens.
- **Process C — Order Gateway** (trade-scope credentials + verify key): the only component that can submit live orders; owns reconciliation.
- **Process D — Dashboard** (no exchange credentials, `127.0.0.1` only): visibility and a constrained set of allowed mutations (pause, kill, acknowledge, raise floor — never lower it).

Order flow has exactly one path: market snapshot → screen → (triage) → forecast → selector decision → order intent → Risk Kernel checks → capital reservation → signed approval token → Order Gateway verification → exchange submission → reconciliation → ledgered terminal state.

See [`plans/SPEC_v3.md`](plans/SPEC_v3.md) for the full specification: threat model, canonical data model, evaluation methodology, configuration reference, testing strategy, and milestone plan.

## Development

Scaffolded with [Start Green Stay Green](https://github.com/Geoffe-Ga/start_green_stay_green): quality gates, CI/CD, AI subagents, and the Ralph autonomous fleet loop are pre-configured.

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install
```

### Quality checks

```bash
pre-commit run --all-files   # all 32 hooks (recommended before commit)

./scripts/test.sh            # pytest with coverage
./scripts/lint.sh            # ruff
./scripts/format.sh --fix    # black + isort
./scripts/typecheck.sh       # mypy --strict
./scripts/security.sh        # bandit + pip-audit
./scripts/complexity.sh      # radon/xenon (≤10 cyclomatic)
./scripts/mutation.sh        # mutmut
./scripts/check-all.sh       # everything
```

### Quality standards

- **Test coverage:** ≥90% (spec requires 100% branch coverage + ≥90% mutation score on `riskkernel`, fixed-point accounting, and token verification — see SPEC §17.6)
- **Cyclomatic complexity:** ≤10 per function
- **Type hints:** 100%, `mypy --strict`
- **All linters:** zero violations

### Repository layout

```
hedgekit/            # Main package
tests/               # Test suite
scripts/             # Quality-gate scripts + Ralph fleet mechanics (scripts/ralph/)
plans/               # SPEC_v3.md and planning documents
prompts/             # Maintenance-scan prompts
.github/workflows/   # CI, AI code review, maintenance scans, metrics dashboard
.claude/             # CLAUDE.md docs, skills, and subagent profiles
docs/                # Live metrics dashboard (GitHub Pages)
```

### Deployment

SPEC §5.1 mandates process isolation: the four processes run as **separate
services** sharing only the ledger volume and localhost sockets — killing one
must never kill another. `deploy/` ships two equivalent skeletons for this at
M0.

**docker compose**

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Starts four services — `pipeline`, `riskkernel`, `order-gateway`, `dashboard`
— each built from the repo-root `Dockerfile` and running
`hedgekit run --process <name>`, with `restart: on-failure`. Only `dashboard`
publishes a port, bound to `127.0.0.1:8080` (SPEC §14: no public inbound), and
its ledger mount is read-only since it holds no trade authority. Process
isolation in practice:

```bash
$ docker compose -f deploy/docker-compose.yml kill pipeline
$ docker compose -f deploy/docker-compose.yml ps --format '{{.Name}} {{.State}}'
hedgekit-pipeline       exited
hedgekit-riskkernel     running
hedgekit-order-gateway  running
hedgekit-dashboard      running
```

**systemd**

`deploy/systemd/` ships one unit per process —
`hedgekit-{pipeline,riskkernel,order-gateway,dashboard}.service` — each with
`Restart=on-failure`. Units are install-prefix-agnostic:
`ExecStart=/usr/bin/env hedgekit run --process <name>` resolves `hedgekit`
from `PATH` rather than a hardcoded install path.

**Dashboard**

The dashboard server (`hedgekit.dashboard.app`) binds `127.0.0.1` only — never
a public interface — and every request must present a static bearer token
(`Authorization: Bearer <token>`), serving a single read-only status page
(mode + last heartbeat). At M0 this HTTP surface exists only as a library: the
`dashboard` process still just idles with heartbeats, and the `127.0.0.1:8080`
compose publish is a reserved placeholder — nothing binds it yet. The server is
wired into the process (and its token and status source connected to their real
backing: config, #11; ledger, #13) once those land. It is a **stub**: real
views (positions, equity, calibration) and mutations (pause, kill, acknowledge,
raise floor) arrive with later epics.

This is an M0 skeleton: the tracer `hedgekit run` (no flags) still just idles
in `RESEARCH` mode, emitting heartbeats.

### Ralph fleet loop

The repo includes the opt-in Ralph autonomous fleet loop (`.claude/commands/ralph-tick.md`, `scripts/ralph/`, maintenance-scan workflows). It assumes a GitHub-hosted issue/PR backlog and git worktrees, and requires manual secret/label setup — see `scripts/ralph/FLEET.md` and `scripts/ralph/PROMPT.md`.

## Attribution

Generated with [Start Green Stay Green](https://github.com/Geoffe-Ga/start_green_stay_green) — maximum-quality Python projects from day one.
