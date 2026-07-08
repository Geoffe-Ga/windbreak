# hedgekit Runbook

Operational instructions for running and observing hedgekit. This runbook
grows with the project; today it covers the always-on PAPER loop shipped in
issue #48.

## Running the PAPER loop

### Prerequisites / config

The PAPER loop is one per-beat hook inside `hedgekit run`'s existing RESEARCH
heartbeat loop (`hedgekit/main.py`). It activates only when **both** of these
hold, checked by `_paper_activated`:

1. The active configuration's `mode_ceiling` (SPEC S16) permits `PAPER` --
   i.e. `Mode.from_config(config.mode_ceiling) is not Mode.RESEARCH`. The
   built-in default configuration (`hedgekit.config.load_default_config`,
   used whenever `--config` is omitted) already ships `mode_ceiling: "paper"`,
   so no custom config is required to satisfy this condition.
2. **All four** of the following `run` flags are supplied together:

   | Flag | Meaning |
   |------|---------|
   | `--paper-books-dir` | Paper-exchange fixture directory (books/markets/fees) loaded via `PaperExchange.from_fixture_dir`. |
   | `--cassette-path` | Recorded LLM cassette file for the offline forecast-replay transport (`ReplayCassette.from_path`). |
   | `--ledger-path` | Path to the PAPER loop's hash-chained SQLite ledger database (a sibling `<name>.wal` file backs its write-ahead log). |
   | `--report-dir` | Directory the weekly report stub is written into. |

If the ceiling forbids PAPER, or even one of the four flags is missing, none
of this is wired: `hedgekit run` falls back to its plain RESEARCH heartbeat
(optionally with `--snapshot-fixture-dir` snapshotting, if given) -- **byte
identical to today's behavior**. This "all four flags or nothing" gate is the
tracer invariant: partially-flagged or ceiling-mismatched invocations can
never half-activate PAPER.

**RESEARCH is the safe default.** Omitting the four PAPER flags (or setting
`mode_ceiling: research`) is always a safe, side-effect-free way to run
`hedgekit run` -- no ledger is created, no paper exchange is touched.

### Starting the loop

```bash
hedgekit run \
  --paper-books-dir tests/fixtures/books/deep_walk \
  --cassette-path /path/to/cassette.json \
  --ledger-path /path/to/state/ledger.db \
  --report-dir /path/to/state/reports \
  --heartbeat-interval 5
```

- `--cassette-path` must point at an existing recorded-cassette JSON file. An
  empty cassette (`{}`) is a valid, offline-safe placeholder as long as the
  forecast pipeline's research stage never actually reaches the LLM transport
  (it abstains first on zero verified citations when no research tools are
  wired) -- see `tests/integration/conftest.py` for the pattern this mirrors.
- `--ledger-path`'s parent directory must exist; the ledger file and its
  `.wal` sibling are created on first use.
- Stop the loop with `Ctrl-C` (SIGINT) or SIGTERM; it shuts down cleanly and
  logs the shutdown reason. `--max-beats N` stops it automatically after `N`
  heartbeats (useful for a bounded smoke run).

### What one PAPER tick actually does

Each beat runs one `hedgekit.scheduler.loop.run_single_tick` pass over the
*real* (unmodified) components, per SPEC S5.3's SINGLE order path:

```
snapshot -> forecast -> select -> approve (seam) -> [only if a token mints]
route -> PaperExchange fill -> reconcile
```

Every stage appends an audit event to the shared hash-chained ledger, plus a
per-tick `ModeHeartbeat`, `EquitySampled`, and `PositionsSnapshotRecorded`.
The weekly report stub (below) is also (re-)written each tick.

**Known limitation -- today's tick never actually fills.** The `approve`
stage composes the real `RiskKernel.evaluate_intent` with the real
`ApprovalPipeline.approve` (`KernelApproval` in `hedgekit/scheduler/loop.py`).
Right now that seam can never mint an approval token, for two independent
reasons:

- Three SPEC S10.3 pre-trade checks are still unconditional-veto stubs,
  blocked on issue #110.
- The reconciliation checks fail closed on `verification=None`, which is
  exactly what the loop honestly supplies today -- no live exchange
  verification cycle runs in PAPER yet.

So a real PAPER tick ledgers a full decision trail (snapshot, forecast,
selector decision, and an `IntentVetoed`) but routes nothing and fills
nothing; `filled_centis` on every tick's outcome is `0`. Don't be surprised
to see nothing but vetoes in `/decisions` or `selector_decisions.json` --
that is the expected, honestly-ledgered state of the loop today. The first
real, kernel-approved paper fill activates once issue #110 lands and a live
verification cycle is wired into the loop.

**Known limitation -- the kill switch does not stop the PAPER loop yet.**
`hedgekit kill --state-dir <dir>` and `hedgekit rearm --state-dir <dir>` write
and clear a `KILL`/`REARM` file, but the PAPER loop's `RiskKernel` is
constructed with `kill_integration=None` (`hedgekit/scheduler/loop.py`), so no
kill-file watcher is polled. To stop the loop today, stop the process itself
(`Ctrl-C`/SIGINT or SIGTERM).

### Observing via the dashboard

`hedgekit.dashboard.app` serves a read-only, loopback-only HTTP surface:

- Binds `127.0.0.1` only (never a public interface -- not configurable, per
  SPEC S14).
- Every route requires `Authorization: Bearer <token>` (timing-safe compared
  against the token `create_server(token=...)` was built with); a
  missing/wrong token gets a `401` with a `WWW-Authenticate: Bearer`
  challenge.

Routes:

| Path | Renders |
|------|---------|
| `/` | Current mode and last-heartbeat status. |
| `/positions` | The latest open-positions snapshot. |
| `/equity` | The equity curve vs. the configured capital floor. |
| `/decisions` | The interleaved selector decisions, including skip/veto reasons. |

There is no `hedgekit run` CLI wiring for the dashboard process yet --
`create_server` is a library entry point an operator boots directly. To serve
it against a live PAPER ledger:

```python
from hedgekit.dashboard.app import create_server
from hedgekit.dashboard.views import build_ledger_read_models_source

server = create_server(
    token="replace-with-a-real-secret",
    status_source=lambda: ...,  # wire to a real status source
    read_models_source=build_ledger_read_models_source(Path("/path/to/state/ledger.db")),
    port=8765,
)
server.serve_forever()
```

Until the loop has ledgered data, `/positions`, `/equity`, and `/decisions`
each render a plain "No data yet." placeholder rather than an empty table or
an error -- this is the documented behavior, not a bug. Passing no
`read_models_source` at all (the default) renders that same placeholder on
every view unconditionally.

### Observing via ledger read models

`hedgekit rebuild` folds a verified ledger into a set of byte-stable JSON
read-model files -- the same projection functions the dashboard reads live:

```bash
hedgekit rebuild --ledger-path /path/to/state/ledger.db --output-dir /path/to/state/read-models
```

This writes (or overwrites) six files into `--output-dir`:

- `config_versions.json` -- every `ConfigLoaded` event.
- `mode_history.json` -- every `ModeHeartbeat` event.
- `gateway_events.json` -- the chronological Order Gateway / crash-recovery
  event trail.
- `positions.json` -- the latest `PositionsSnapshotRecorded` snapshot (at
  most one entry).
- `equity_curve.json` -- every `EquitySampled` sample, in ledger order.
- `selector_decisions.json` -- the interleaved `SelectorDecisionRecorded` /
  `IntentApproved` / `IntentVetoed` trail, in ledger order.

`rebuild` verifies the ledger's hash chain before projecting; a corrupted
chain fails closed with a nonzero exit code and the offending sequence number
on stderr, rather than silently emitting a plausible-but-wrong projection.

### Weekly reports

Each PAPER tick calls `hedgekit.reports.weekly.maybe_write_weekly`, which
writes at most one `weekly-YYYY-MM-DD.md` file per ISO calendar week into
`--report-dir` (idempotent: repeated calls within the same ISO week return the
already-written file untouched). The stub carries markdown section headers
(`Equity vs floor`, `Positions`, `Decisions`) each with a `No data yet.` body
-- populating the real bodies from ledgered data is a later documentation
pass.

### Known limitations (summary)

- The real Risk Kernel currently vetoes every intent (three SPEC S10.3 checks
  are unconditional-veto stubs blocked on #110; reconciliation also fails
  closed on the `verification=None` the loop supplies), so no PAPER tick
  fills yet -- expect vetoes, not fills, in the ledger and dashboard.
- `hedgekit kill`/`hedgekit rearm` do not stop or gate the PAPER loop today
  (`kill_integration=None`); use process signals to stop the loop.
- There is no `hedgekit run --process dashboard` wiring yet that actually
  boots the HTTP dashboard server; operators start it directly via
  `hedgekit.dashboard.app.create_server`.
- Weekly reports are structural stubs (`No data yet.` bodies); the real
  report content is a later pass.
