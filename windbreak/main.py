"""Command-line entry point and heartbeat loop for windbreak.

The ``windbreak run`` command starts the always-on RESEARCH-mode heartbeat
loop that later issues will grow into the full four-process pipeline. For now
it emits a periodic heartbeat line and shuts down cleanly on SIGINT, SIGTERM,
or an optional beat budget.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from windbreak.alerts import AlertDispatcher, AlertType, LoggingLedgerWriter, cli_token
from windbreak.config import (
    ConfigError,
    InMemoryConfigEventRecorder,
    LedgerConfigEventRecorder,
    config_hash,
    load_config,
    load_default_config,
)
from windbreak.drills.catalog import DRILL_NAMES
from windbreak.drills.context import bind_paper_context, bind_production_context
from windbreak.ledger import (
    SqliteLedgerStore,
    anchor_command,
    rebuild_command,
    verify_command,
)
from windbreak.logging_setup import configure_logging
from windbreak.riskkernel.ack_flow import ACKS_DIRNAME
from windbreak.riskkernel.kill import KILL_FILENAME, REARM_FILENAME

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType

    from windbreak.config import ConfigLoadEvent, WindbreakConfig

#: Operating mode reported in every heartbeat line. Matches the RESEARCH state
#: of the SPEC mode machine; windbreak ships research-only for now.
MODE_RESEARCH = "RESEARCH"

#: The four SPEC processes ``windbreak run --process`` can represent, in SPEC
#: order. Each invocation stands in for exactly one; the chosen token is
#: stamped as the ``component`` on every heartbeat and shutdown log line. The
#: gateway token is underscore-separated (``order_gateway``) to match its
#: Python package name, even though its compose/systemd unit names are
#: hyphenated (``order-gateway``).
PROCESS_CHOICES = ("pipeline", "riskkernel", "order_gateway", "dashboard")

#: Default process represented when ``--process`` is omitted.
_DEFAULT_PROCESS = "pipeline"

#: Seconds between heartbeats when ``--heartbeat-interval`` is omitted.
_DEFAULT_HEARTBEAT_INTERVAL = 5.0

#: Default alert body dispatched by the ``alert-test`` subcommand.
_DEFAULT_ALERT_MESSAGE = "test alert"

#: The environment variable a leaked trade key would surface in; the preflight
#: leak check (SPEC S5.2) fails closed if it is visible to this process.
_TRADE_KEY_ENV_VAR = "WINDBREAK_TRADE_KEY"

#: Default fixture/state directories for the ``drill`` verb when unspecified.
_DEFAULT_DRILL_FIXTURE_DIR = Path("drills/fixtures")
_DEFAULT_DRILL_STATE_DIR = Path("drills/state")

#: Maps each alert's CLI token back to its :class:`AlertType` member.
_TOKEN_TO_ALERT_TYPE = {cli_token(alert_type): alert_type for alert_type in AlertType}

#: Shutdown reason logged when the loop exhausts its ``--max-beats`` budget.
_REASON_MAX_BEATS = "max_beats"

#: Shutdown reason logged when the loop is stopped via its stop event.
_REASON_SIGNAL = "signal"

#: Log-friendly source label for a configuration built from built-in defaults.
_DEFAULTS_SOURCE_LABEL = "<defaults>"

#: An approval id is exactly 32 lowercase hex characters -- the shape
#: ``HumanAckQueue`` mints via ``secrets.token_hex(16)`` -- so the ``ack`` verb
#: rejects any other token as a usage error before writing a bogus drop-box file.
_APPROVAL_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

_LOGGER = logging.getLogger("windbreak")


def _approval_id(raw: str) -> str:
    """Parse a 32-hex-character approval id for use as an argparse ``type``.

    Args:
        raw: The raw command-line token.

    Returns:
        The validated approval id, unchanged.

    Raises:
        argparse.ArgumentTypeError: If ``raw`` is not exactly 32 lowercase hex
            characters.
    """
    if _APPROVAL_ID_PATTERN.fullmatch(raw) is None:
        raise argparse.ArgumentTypeError(
            "approval id must be exactly 32 lowercase hex characters"
        )
    return raw


@dataclass
class ShutdownState:
    """Shared mutable state coordinating a graceful shutdown.

    Attributes:
        stop_event: Set to request the heartbeat loop stop.
        reason: Name of the signal that triggered shutdown, or None while
            the loop is still running.
    """

    stop_event: threading.Event = field(default_factory=threading.Event)
    reason: str | None = None


def _non_negative_float(raw: str) -> float:
    """Parse a non-negative float for use as an argparse ``type``.

    Args:
        raw: The raw command-line token.

    Returns:
        The parsed floating-point value.

    Raises:
        argparse.ArgumentTypeError: If ``raw`` is not a float or is negative.
    """
    value = float(raw)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("heartbeat interval must be finite")
    if value < 0:
        raise argparse.ArgumentTypeError("heartbeat interval must be non-negative")
    return value


def _non_negative_int(raw: str) -> int:
    """Parse a non-negative int for use as an argparse ``type``.

    Args:
        raw: The raw command-line token.

    Returns:
        The parsed integer value.

    Raises:
        argparse.ArgumentTypeError: If ``raw`` is not an int or is negative.
    """
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("max beats must be non-negative")
    return value


def _add_run_arguments(run_parser: argparse.ArgumentParser) -> None:
    """Register the ``run`` subcommand's options on its subparser.

    Args:
        run_parser: The ``run`` subparser to populate with options.
    """
    run_parser.add_argument(
        "--heartbeat-interval",
        type=_non_negative_float,
        default=_DEFAULT_HEARTBEAT_INTERVAL,
        help="Seconds between heartbeats (default: %(default)s).",
    )
    run_parser.add_argument(
        "--max-beats",
        type=_non_negative_int,
        default=None,
        help="Stop after this many heartbeats (default: run until signalled).",
    )
    run_parser.add_argument(
        "--process",
        choices=PROCESS_CHOICES,
        default=_DEFAULT_PROCESS,
        help="Which SPEC process this invocation represents (default: %(default)s).",
    )
    run_parser.add_argument(
        "--snapshot-fixture-dir",
        default=None,
        help=(
            "Directory of exchange JSON fixtures to snapshot each beat "
            "(default: snapshotting is off)."
        ),
    )
    run_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to a SPEC §16 YAML config (default: built-in §16 defaults).",
    )
    _add_paper_loop_arguments(run_parser)


def _add_paper_loop_arguments(run_parser: argparse.ArgumentParser) -> None:
    """Register the four always-on PAPER-loop composition flags (issue #48).

    PAPER activates only when the mode ceiling permits PAPER *and* all four flags
    are supplied; each defaults to ``None`` so omitting any one leaves the loop in
    its byte-identical RESEARCH-only behavior.

    Args:
        run_parser: The ``run`` subparser to populate with the PAPER flags.
    """
    run_parser.add_argument(
        "--paper-books-dir",
        type=Path,
        default=None,
        help="Paper-exchange fixture directory (default: PAPER loop off).",
    )
    run_parser.add_argument(
        "--cassette-path",
        type=Path,
        default=None,
        help="Recorded LLM cassette for the offline forecast replay transport.",
    )
    run_parser.add_argument(
        "--ledger-path",
        type=Path,
        default=None,
        help=(
            "Path to the operational hash-chained ledger database. Every "
            "successful config load records a ConfigLoaded event here; the "
            "PAPER loop, when activated, appends its events to the same file."
        ),
    )
    run_parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Directory the weekly PAPER report stub is written into.",
    )


def _add_rebuild_arguments(rebuild_parser: argparse.ArgumentParser) -> None:
    """Register the ``rebuild`` subcommand's options on its subparser.

    Args:
        rebuild_parser: The ``rebuild`` subparser to populate with options.
    """
    rebuild_parser.add_argument(
        "--ledger-path",
        type=Path,
        required=True,
        help="Path to the SQLite ledger database.",
    )
    rebuild_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write the read-model files into.",
    )


def _add_anchor_arguments(anchor_parser: argparse.ArgumentParser) -> None:
    """Register the ``anchor`` subcommand's options on its subparser.

    Args:
        anchor_parser: The ``anchor`` subparser to populate with options.
    """
    anchor_parser.add_argument(
        "--ledger-path",
        type=Path,
        required=True,
        help="Path to the SQLite ledger database.",
    )
    anchor_parser.add_argument(
        "--anchor-path",
        type=Path,
        required=True,
        help="Path to the append-only JSON-lines anchor file.",
    )


def _add_verify_arguments(verify_parser: argparse.ArgumentParser) -> None:
    """Register the ``verify`` subcommand's options on its subparser.

    Args:
        verify_parser: The ``verify`` subparser to populate with options.
    """
    verify_parser.add_argument(
        "--ledger-path",
        type=Path,
        required=True,
        help="Path to the SQLite ledger database.",
    )
    verify_parser.add_argument(
        "--anchor-path",
        type=Path,
        required=True,
        help="Path to the append-only JSON-lines anchor file.",
    )


def _add_kill_arguments(kill_parser: argparse.ArgumentParser) -> None:
    """Register the ``kill`` subcommand's options on its subparser.

    Args:
        kill_parser: The ``kill`` subparser to populate with options.
    """
    kill_parser.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="Directory to write the KILL file into.",
    )


def _add_ack_arguments(ack_parser: argparse.ArgumentParser) -> None:
    """Register the ``ack`` subcommand's options on its subparser.

    Args:
        ack_parser: The ``ack`` subparser to populate with options.
    """
    ack_parser.add_argument(
        "--approval-id",
        type=_approval_id,
        required=True,
        help="The 32-hex-character approval id to acknowledge.",
    )
    ack_parser.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="Directory whose acks/ drop-box the ack file is written into.",
    )


def _add_rearm_arguments(rearm_parser: argparse.ArgumentParser) -> None:
    """Register the ``rearm`` subcommand's options on its subparser.

    Args:
        rearm_parser: The ``rearm`` subparser to populate with options.
    """
    rearm_parser.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="Directory to write the REARM file into.",
    )


def _add_preflight_arguments(preflight_parser: argparse.ArgumentParser) -> None:
    """Register the ``preflight`` subcommand's options on its subparser.

    Args:
        preflight_parser: The ``preflight`` subparser to populate with options.
    """
    preflight_parser.add_argument(
        "--fixture-dir",
        type=Path,
        required=True,
        help="Directory of exchange JSON fixtures to run the checks against.",
    )
    preflight_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as a JSON document instead of a table.",
    )
    preflight_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to a SPEC §16 YAML config (default: built-in §16 defaults).",
    )
    preflight_parser.add_argument(
        "--secrets-file",
        type=Path,
        action="append",
        default=None,
        help="A secrets file whose permissions to check (repeatable).",
    )


def _add_drill_arguments(drill_parser: argparse.ArgumentParser) -> None:
    """Register the ``drill`` subcommand's options on its subparser.

    Args:
        drill_parser: The ``drill`` subparser to populate with options.
    """
    drill_parser.add_argument(
        "name",
        choices=sorted(DRILL_NAMES),
        help="Which operational drill to run.",
    )
    drill_parser.add_argument(
        "--production",
        action="store_true",
        help=(
            "Rebind only the exchange adapter for a manual production run "
            "(requires a non-empty exchange credential in the environment; "
            "rebinds a fresh stub exchange until a live adapter lands). "
            "Default: paper."
        ),
    )
    drill_parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=_DEFAULT_DRILL_FIXTURE_DIR,
        help="Directory of drill fixtures (default: %(default)s).",
    )
    drill_parser.add_argument(
        "--state-dir",
        type=Path,
        default=_DEFAULT_DRILL_STATE_DIR,
        help="Directory for drill protocol/scratch files (default: %(default)s).",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the ``windbreak`` command-line argument parser.

    Returns:
        A parser with a required ``run`` subcommand exposing
        ``--heartbeat-interval``, ``--max-beats``, ``--process``,
        ``--snapshot-fixture-dir``, and ``--config``; a ``rebuild`` subcommand
        exposing ``--ledger-path`` and ``--output-dir``; ``anchor`` and
        ``verify`` subcommands exposing ``--ledger-path`` and ``--anchor-path``
        (append the ledger head to, and check the live chain against, the
        anchor file); ``kill`` and ``rearm`` subcommands exposing
        ``--state-dir``; an ``ack`` subcommand exposing ``--approval-id`` and
        ``--state-dir``; and a developer-only ``alert-test`` subcommand hidden
        from ``--help``.
    """
    parser = argparse.ArgumentParser(
        prog="windbreak",
        description="windbreak always-on forecast trader CLI.",
    )
    # ``metavar`` keeps the auto-generated ``{run,alert-test}`` choice list --
    # which would otherwise leak the hidden ``alert-test`` command -- out of the
    # top-level usage line. The ``alert-test`` parser below is registered without
    # a ``help`` argument, so argparse creates no pseudo-action for it and it is
    # omitted from the detailed subcommand listing (a developer-only command).
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")
    _add_run_arguments(subparsers.add_parser("run", help="Start the heartbeat loop."))
    _add_rebuild_arguments(
        subparsers.add_parser(
            "rebuild", help="Rebuild derived read models from the ledger."
        )
    )
    _add_anchor_arguments(
        subparsers.add_parser(
            "anchor", help="Append the ledger's head hash to the anchor file."
        )
    )
    _add_verify_arguments(
        subparsers.add_parser(
            "verify", help="Verify the ledger's live chain against its anchors."
        )
    )
    _add_kill_arguments(
        subparsers.add_parser(
            "kill", help="Engage the kill switch (write a KILL file)."
        )
    )
    _add_ack_arguments(
        subparsers.add_parser(
            "ack",
            help="Grant a human acknowledgement (write an acks/<id> file).",
        )
    )
    _add_rearm_arguments(
        subparsers.add_parser(
            "rearm",
            help="Re-arm after a kill (write the typed phrase to a REARM file).",
        )
    )
    _add_preflight_arguments(
        subparsers.add_parser(
            "preflight",
            help="Run the production-readiness preflight checklist.",
        )
    )
    _add_drill_arguments(
        subparsers.add_parser(
            "drill",
            help="Run an operational drill (rehearse a safety mechanism).",
        )
    )
    alert_parser = subparsers.add_parser("alert-test")
    alert_parser.add_argument(
        "type",
        choices=[cli_token(alert_type) for alert_type in AlertType],
        help="Alert type (as a CLI token) to emit a test alert for.",
    )
    alert_parser.add_argument(
        "--message",
        default=_DEFAULT_ALERT_MESSAGE,
        help="Alert body to dispatch (default: %(default)s).",
    )
    return parser


def run_loop(
    interval_seconds: float,
    *,
    max_beats: int | None = None,
    stop_event: threading.Event | None = None,
    state: ShutdownState | None = None,
    component: str = _DEFAULT_PROCESS,
    on_beat: Callable[[int], None] | None = None,
) -> None:
    """Emit heartbeats until stopped by the stop event or a beat budget.

    Args:
        interval_seconds: Seconds to wait between heartbeats. Passed to
            ``stop_event.wait`` unmodified.
        max_beats: Optional maximum number of heartbeats before shutting down
            with reason ``max_beats``. None runs until the stop event is set.
        stop_event: Optional event used to request shutdown. Defaults to
            ``state.stop_event`` when ``state`` is given, else a fresh event.
        state: Optional shared shutdown state. When a signal handler has
            recorded a signal name on it, that name becomes the shutdown
            reason; otherwise the generic ``signal`` reason is used.
        component: Which SPEC process (one of :data:`PROCESS_CHOICES`) this
            loop represents. Stamped as the ``component`` extra on every
            heartbeat and shutdown log record; the rendered message text is
            unchanged.
        on_beat: Optional hook invoked once per beat with the 1-based sequence
            number, after that beat's heartbeat is logged. None (the default)
            leaves the heartbeat behavior unchanged.
    """
    if stop_event is None:
        stop_event = state.stop_event if state is not None else threading.Event()

    seq = 0
    reason = _REASON_SIGNAL
    while True:
        if stop_event.is_set():
            if state is not None and state.reason is not None:
                reason = state.reason
            break
        if max_beats is not None and seq >= max_beats:
            reason = _REASON_MAX_BEATS
            break
        seq += 1
        _LOGGER.info(
            "mode=%s heartbeat seq=%d",
            MODE_RESEARCH,
            seq,
            extra={"component": component},
        )
        if on_beat is not None:
            on_beat(seq)
        stop_event.wait(interval_seconds)

    _LOGGER.info("shutdown reason=%s", reason, extra={"component": component})


def _install_signal_handlers(state: ShutdownState) -> None:
    """Install SIGINT/SIGTERM handlers that request a graceful shutdown.

    The installed handler is directly invokable as ``handler(signum, frame)``.
    On delivery it records the signal name on ``state.reason`` and sets
    ``state.stop_event`` so an in-flight :func:`run_loop` unwinds cleanly.

    Args:
        state: Shared shutdown state mutated when a signal arrives.
    """

    def _handle(signum: int, _frame: FrameType | None) -> None:
        """Record the signal name and request shutdown."""
        state.reason = signal.Signals(signum).name
        state.stop_event.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def _load_configured(
    args: argparse.Namespace, recorder: InMemoryConfigEventRecorder
) -> WindbreakConfig:
    """Load the config named by ``--config``, or the built-in defaults.

    Args:
        args: The parsed CLI arguments; ``args.config`` is a path or None.
        recorder: The recorder notified of the resulting hash and diff.

    Returns:
        The loaded configuration.

    Raises:
        ConfigError: If a ``--config`` path cannot be read or validated.
    """
    if args.config is not None:
        return load_config(args.config, recorder=recorder)
    return load_default_config(recorder=recorder)


def _run_preflight(args: argparse.Namespace) -> int:
    """Run the production-readiness checklist and print its report (SPEC S3.3).

    Builds the injected seams -- a fixture-backed read-only connector, an honest
    no-self-test scope prober (the real self-test client is issue #57), a
    trade-key environment-leak prober over :data:`os.environ`, a log-only alert
    dispatcher, and the configured secrets paths -- runs the seven checks, and
    prints the report as JSON or a table to stdout. The connector and preflight
    imports are local so the RESEARCH heartbeat path never imports them.

    Args:
        args: Parsed ``preflight`` arguments carrying ``fixture_dir``, ``json``,
            ``config``, and ``secrets_file``.

    Returns:
        The report's fail-closed exit code (0 on all-pass, 1 on any failure), or
        1 on a fatal ``--config`` error.
    """
    from windbreak.connector import FakeExchange
    from windbreak.preflight import (
        EnvTradeKeyLeakProber,
        KeyScopeProbe,
        render_table,
        report_to_json,
        run_preflight,
    )

    class _NullScopeProber:
        """A scope prober reporting no self-test support (real one: issue #57)."""

        def probe(self) -> KeyScopeProbe:
            """Return an all-unsupported probe so scope checks honestly SKIP."""
            return KeyScopeProbe(
                self_test_supported=False,
                scope_verified=False,
                withdrawal_capable=False,
            )

    recorder = InMemoryConfigEventRecorder()
    try:
        config = _load_configured(args, recorder)
    except ConfigError as exc:
        _LOGGER.critical("FATAL: %s", exc)
        return 1
    connector = FakeExchange.from_fixture_dir(args.fixture_dir)
    dispatcher = AlertDispatcher(sinks=[], ledger_writer=LoggingLedgerWriter())
    report = run_preflight(
        connector=connector,
        scope_prober=_NullScopeProber(),
        leak_prober=EnvTradeKeyLeakProber(environ=os.environ, var=_TRADE_KEY_ENV_VAR),
        eligible_markets=connector.list_markets(),
        alert_dispatcher=dispatcher,
        secrets_paths=tuple(args.secrets_file or ()),
        config=config,
    )
    print(report_to_json(report) if args.json else render_table(report))
    return report.exit_code


def _epoch_now() -> int:
    """Return the current wall clock as whole epoch seconds (SPEC S6.1).

    Casts :func:`time.time` to an ``int`` so the drill clock is float-free; this
    is the CLI's one reading of the wall clock, injected into the drill context
    so the drills themselves never call :func:`time.time`.

    Returns:
        The current time, in whole epoch seconds.
    """
    return int(time.time())


def _run_drill(args: argparse.Namespace) -> int:
    """Run one operational drill and map its verdict to an exit code (issue #59).

    Builds the deterministic paper context from the injected wall clock and the
    real process environment (the CLI's one reading of each), rebinding only the
    exchange adapter when ``--production`` is set, then runs the named drill and
    ledgers exactly one ``DrillCompleted``. The heavy registry/framework imports
    are local so the RESEARCH heartbeat path never imports them.

    Args:
        args: Parsed ``drill`` arguments carrying ``name``, ``production``,
            ``fixture_dir``, and ``state_dir``.

    Returns:
        ``0`` iff the drill passed, else ``1``.
    """
    from windbreak.drills.framework import run_drill
    from windbreak.drills.registry import DRILLS
    from windbreak.riskkernel.process import LoggingKernelLedgerWriter

    writer = LoggingKernelLedgerWriter()
    paper_ctx = bind_paper_context(
        fixture_dir=args.fixture_dir,
        state_dir=args.state_dir,
        ledger_writer=writer,
        clock=_epoch_now,
        env=os.environ,
    )
    ctx = (
        bind_production_context(paper_ctx, env=os.environ)
        if args.production
        else paper_ctx
    )
    result = run_drill(DRILLS[args.name](), ctx, writer)
    return 0 if result.passed else 1


def _run_alert_test(args: argparse.Namespace) -> int:
    """Dispatch a single test alert through the log-only fallback.

    With no real sinks configured, the dispatcher's fallback fires and the
    ledger writer logs the resulting :class:`~windbreak.alerts.AlertEmitted`
    event, both observable as JSON on stderr.

    Args:
        args: Parsed ``alert-test`` arguments carrying ``type`` and
            ``message``.

    Returns:
        The process exit code (always 0).
    """
    alert_type = _TOKEN_TO_ALERT_TYPE[args.type]
    dispatcher = AlertDispatcher(sinks=[], ledger_writer=LoggingLedgerWriter())
    dispatcher.dispatch(alert_type, args.message)
    return 0


def _run_kill(args: argparse.Namespace) -> int:
    """Engage the kill switch by dropping a ``KILL`` file into ``--state-dir``.

    The file's mere presence is the durable kill signal a running kernel's file
    watcher acts on; its content is never read, so an empty file suffices.

    Args:
        args: Parsed ``kill`` arguments carrying ``state_dir``.

    Returns:
        The process exit code (always 0).
    """
    args.state_dir.mkdir(parents=True, exist_ok=True)
    (args.state_dir / KILL_FILENAME).write_text("", encoding="utf-8")
    return 0


def _run_ack(args: argparse.Namespace) -> int:
    """Grant a human acknowledgement by dropping a file into ``acks/``.

    Writes an empty file at ``<state-dir>/acks/<approval-id>`` -- the
    presence-driven signal :class:`windbreak.riskkernel.ack_flow.AckFileWatcher`
    polls for, mirroring ``windbreak kill``'s ``KILL``-file convention. The
    approval id is already validated (32 hex chars) by the argparse ``type``, so
    a malformed id is rejected before this runs and no file is ever written. No
    network, no credentials.

    Args:
        args: Parsed ``ack`` arguments carrying ``approval_id`` and ``state_dir``.

    Returns:
        The process exit code (always 0).
    """
    acks_dir = args.state_dir / ACKS_DIRNAME
    acks_dir.mkdir(parents=True, exist_ok=True)
    (acks_dir / args.approval_id).write_text("", encoding="utf-8")
    return 0


def _run_rearm(args: argparse.Namespace) -> int:
    """Write the typed re-arm phrase *verbatim* to a ``REARM`` file.

    The phrase is written exactly as typed -- no stripping, no case change --
    because the kernel's re-arm compares it byte-for-byte against the expected
    confirmation, so any normalization here would silently break re-arm.

    Args:
        args: Parsed ``rearm`` arguments carrying ``state_dir``.

    Returns:
        The process exit code (always 0).
    """
    phrase = input()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    (args.state_dir / REARM_FILENAME).write_text(phrase, encoding="utf-8")
    return 0


def _build_snapshot_on_beat(fixture_dir: str) -> Callable[[int], None]:
    """Build a per-beat hook that snapshots a fixture-backed exchange.

    The connector imports are local so the heartbeat path stays free of the
    connector package unless snapshotting is actually requested.

    Args:
        fixture_dir: Directory of exchange JSON fixtures to snapshot.

    Returns:
        A callable that, given the beat sequence, runs one snapshot pass.
    """
    from windbreak.connector import (
        FakeExchange,
        LoggingEventLedgerWriter,
        MarketSnapshotTask,
    )
    from windbreak.screener import StubScreener

    task = MarketSnapshotTask(
        FakeExchange.from_fixture_dir(fixture_dir),
        StubScreener(),
        LoggingEventLedgerWriter(),
    )

    def _on_beat(_seq: int) -> None:
        """Run one snapshot pass, ignoring the beat sequence number."""
        task.run_once()

    return _on_beat


def _paper_activated(config: WindbreakConfig, args: argparse.Namespace) -> bool:
    """Return whether the always-on PAPER loop should be wired this run (#48).

    PAPER activates only when the configured mode ceiling permits PAPER *and*
    every one of the four PAPER flags is supplied. A ``research`` ceiling -- even
    with all four flags -- never activates it (the tracer invariant), and neither
    do partial flags. The ceiling is parsed from the SPEC S16 token, whose four
    ladder values are the only valid ceilings; a non-``RESEARCH`` ceiling permits
    PAPER.

    Args:
        config: The loaded configuration whose ``mode_ceiling`` gates activation.
        args: The parsed ``run`` arguments carrying the four PAPER flags.

    Returns:
        ``True`` only when PAPER is permitted and all four flags are supplied.
    """
    from windbreak.riskkernel.modes import Mode

    flags = (
        args.paper_books_dir,
        args.cassette_path,
        args.ledger_path,
        args.report_dir,
    )
    if any(flag is None for flag in flags):
        return False
    return Mode.from_config(config.mode_ceiling) is not Mode.RESEARCH


def _build_paper_on_beat(
    args: argparse.Namespace, config: WindbreakConfig
) -> Callable[[int], None]:
    """Build a per-beat hook that runs one always-on PAPER tick (issue #48).

    The scheduler imports are local so the RESEARCH heartbeat path never imports
    ``windbreak.scheduler`` (nor, transitively, the paper order-submission client)
    unless PAPER is actually activated. The dependency bundle -- which opens the
    ledger database -- is built once here, so no ledger is ever created on a run
    that does not activate PAPER.

    Args:
        args: The parsed ``run`` arguments carrying the four PAPER flags.
        config: The loaded PAPER-ceilinged configuration.

    Returns:
        A callable that, given the beat sequence, runs one PAPER tick.
    """
    from windbreak.scheduler.loop import build_paper_deps, run_single_tick

    deps = build_paper_deps(
        books_dir=args.paper_books_dir,
        cassette_path=args.cassette_path,
        ledger_path=args.ledger_path,
        report_dir=args.report_dir,
        config=config,
    )

    def _on_beat(seq: int) -> None:
        """Run one PAPER tick for the given beat sequence."""
        run_single_tick(deps, beat=seq)

    return _on_beat


def _resolve_on_beat(
    args: argparse.Namespace, config: WindbreakConfig
) -> Callable[[int], None] | None:
    """Resolve the per-beat hook: the PAPER tick, a snapshot pass, or none.

    PAPER activation (issue #48) takes precedence when permitted and fully
    flagged; otherwise the pre-existing snapshot hook is wired when a fixture
    directory is given; otherwise there is no hook and the loop is a bare
    RESEARCH heartbeat.

    Args:
        args: The parsed ``run`` arguments.
        config: The loaded configuration.

    Returns:
        The resolved per-beat hook, or ``None`` for a bare heartbeat.
    """
    if _paper_activated(config, args):
        return _build_paper_on_beat(args, config)
    if args.snapshot_fixture_dir is not None:
        return _build_snapshot_on_beat(args.snapshot_fixture_dir)
    return None


def _ledger_config_loads(
    ledger_path: Path, events: list[ConfigLoadEvent], component: str
) -> None:
    """Append each captured config-load event to the hash-chained ledger.

    Opens the ledger at ``ledger_path`` only after the config has already
    loaded cleanly through the in-memory recorder, so a fatal ``--config``
    error stays fail-closed and never creates a database file. The store is
    closed before returning, so a later PAPER loop can reopen the same file.

    Args:
        ledger_path: Filesystem path to the hash-chained ledger database.
        events: The config-load events captured during this run, in order.
        component: The process label stamped on each ``ConfigLoaded`` event.
    """
    store = SqliteLedgerStore(ledger_path)
    try:
        recorder = LedgerConfigEventRecorder(store, component=component)
        for event in events:
            recorder.record_config_loaded(
                config_hash=event.config_hash, diff=event.diff, source=event.source
            )
    finally:
        store.close()


def _run_heartbeat(args: argparse.Namespace) -> int:
    """Load the requested config, log it, then drive the heartbeat loop.

    Structured logging is already installed by :func:`main`, so the config
    diagnostics emitted here (the ``config loaded`` line, or a ``FATAL``
    critical on a bad ``--config``) are JSON records like every other log
    line -- the ``--config`` loader (issue #11) and the JSON logging pipeline
    (issue #14) composed into one flow. The config is first loaded through an
    in-memory recorder so a bad ``--config`` fails closed *before* any ledger
    file is opened; only after the successful load does a supplied
    ``--ledger-path`` get the captured ``ConfigLoaded`` event(s) persisted to
    the real hash-chained ledger (issue #74) -- as the first records, before
    any PAPER-loop events.

    Args:
        args: Parsed ``run`` arguments carrying ``config``, ``process``,
            ``heartbeat_interval``, ``max_beats``, ``ledger_path``, and
            ``snapshot_fixture_dir``. The loaded config and the ``--process``
            component compose here: the config is loaded and logged, its load
            is ledgered when ``--ledger-path`` is given, then the heartbeat
            loop runs stamped with that component. When
            ``--snapshot-fixture-dir`` is given, a per-beat snapshot hook is
            wired in alongside.

    Returns:
        The process exit code (0 on success, 1 on a fatal config error).
    """
    recorder = InMemoryConfigEventRecorder()
    try:
        config = _load_configured(args, recorder)
    except ConfigError as exc:
        _LOGGER.critical("FATAL: %s", exc)
        return 1
    source = str(args.config) if args.config is not None else _DEFAULTS_SOURCE_LABEL
    _LOGGER.info(
        "config loaded source=%s mode_ceiling=%s hash=%s",
        source,
        config.mode_ceiling,
        config_hash(config),
    )
    if args.ledger_path is not None:
        _ledger_config_loads(args.ledger_path, recorder.events, args.process)
    state = ShutdownState()
    _install_signal_handlers(state)
    on_beat = _resolve_on_beat(args, config)
    run_loop(
        args.heartbeat_interval,
        max_beats=args.max_beats,
        stop_event=state.stop_event,
        state=state,
        component=args.process,
        on_beat=on_beat,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the requested windbreak command.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code (0 on success, 1 on a fatal config error).
    """
    args = build_parser().parse_args(argv)
    configure_logging(level=logging.INFO)
    if args.command == "rebuild":
        return rebuild_command(args)
    if args.command == "anchor":
        return anchor_command(args)
    if args.command == "verify":
        return verify_command(args)
    if args.command == "kill":
        return _run_kill(args)
    if args.command == "ack":
        return _run_ack(args)
    if args.command == "rearm":
        return _run_rearm(args)
    if args.command == "alert-test":
        return _run_alert_test(args)
    if args.command == "preflight":
        return _run_preflight(args)
    if args.command == "drill":
        return _run_drill(args)
    return _run_heartbeat(args)


if __name__ == "__main__":
    raise SystemExit(main())
