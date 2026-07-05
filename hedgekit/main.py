"""Command-line entry point and heartbeat loop for hedgekit.

The ``hedgekit run`` command starts the always-on RESEARCH-mode heartbeat
loop that later issues will grow into the full four-process pipeline. For now
it emits a periodic heartbeat line and shuts down cleanly on SIGINT, SIGTERM,
or an optional beat budget.
"""

from __future__ import annotations

import argparse
import logging
import math
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from hedgekit.alerts import AlertDispatcher, AlertType, LoggingLedgerWriter, cli_token
from hedgekit.config import (
    ConfigError,
    InMemoryConfigEventRecorder,
    config_hash,
    load_config,
    load_default_config,
)
from hedgekit.ledger import rebuild_command
from hedgekit.logging_setup import configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType

    from hedgekit.config import HedgekitConfig

#: Operating mode reported in every heartbeat line. Matches the RESEARCH state
#: of the SPEC mode machine; hedgekit ships research-only for now.
MODE_RESEARCH = "RESEARCH"

#: The four SPEC processes ``hedgekit run --process`` can represent, in SPEC
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

#: Maps each alert's CLI token back to its :class:`AlertType` member.
_TOKEN_TO_ALERT_TYPE = {cli_token(alert_type): alert_type for alert_type in AlertType}

#: Shutdown reason logged when the loop exhausts its ``--max-beats`` budget.
_REASON_MAX_BEATS = "max_beats"

#: Shutdown reason logged when the loop is stopped via its stop event.
_REASON_SIGNAL = "signal"

#: Log-friendly source label for a configuration built from built-in defaults.
_DEFAULTS_SOURCE_LABEL = "<defaults>"

_LOGGER = logging.getLogger("hedgekit")


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


def build_parser() -> argparse.ArgumentParser:
    """Build the ``hedgekit`` command-line argument parser.

    Returns:
        A parser with a required ``run`` subcommand exposing
        ``--heartbeat-interval``, ``--max-beats``, ``--process``,
        ``--snapshot-fixture-dir``, and ``--config``; a ``rebuild`` subcommand
        exposing ``--ledger-path`` and
        ``--output-dir``; and a developer-only ``alert-test`` subcommand hidden
        from ``--help``.
    """
    parser = argparse.ArgumentParser(
        prog="hedgekit",
        description="hedgekit always-on forecast trader CLI.",
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
) -> HedgekitConfig:
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


def _run_alert_test(args: argparse.Namespace) -> int:
    """Dispatch a single test alert through the log-only fallback.

    With no real sinks configured, the dispatcher's fallback fires and the
    ledger writer logs the resulting :class:`~hedgekit.alerts.AlertEmitted`
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


def _build_snapshot_on_beat(fixture_dir: str) -> Callable[[int], None]:
    """Build a per-beat hook that snapshots a fixture-backed exchange.

    The connector imports are local so the heartbeat path stays free of the
    connector package unless snapshotting is actually requested.

    Args:
        fixture_dir: Directory of exchange JSON fixtures to snapshot.

    Returns:
        A callable that, given the beat sequence, runs one snapshot pass.
    """
    from hedgekit.connector import (
        FakeExchange,
        LoggingEventLedgerWriter,
        MarketSnapshotTask,
    )
    from hedgekit.screener import StubScreener

    task = MarketSnapshotTask(
        FakeExchange.from_fixture_dir(fixture_dir),
        StubScreener(),
        LoggingEventLedgerWriter(),
    )

    def _on_beat(_seq: int) -> None:
        """Run one snapshot pass, ignoring the beat sequence number."""
        task.run_once()

    return _on_beat


def _run_heartbeat(args: argparse.Namespace) -> int:
    """Load the requested config, log it, then drive the heartbeat loop.

    Structured logging is already installed by :func:`main`, so the config
    diagnostics emitted here (the ``config loaded`` line, or a ``FATAL``
    critical on a bad ``--config``) are JSON records like every other log
    line -- the ``--config`` loader (issue #11) and the JSON logging pipeline
    (issue #14) composed into one flow.

    Args:
        args: Parsed ``run`` arguments carrying ``config``, ``process``,
            ``heartbeat_interval``, ``max_beats``, and ``snapshot_fixture_dir``.
            The loaded config and the ``--process`` component compose here: the
            config is loaded and logged, then the heartbeat loop runs stamped
            with that component. When ``--snapshot-fixture-dir`` is given, a
            per-beat snapshot hook is wired in alongside.

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
    state = ShutdownState()
    _install_signal_handlers(state)
    on_beat = (
        _build_snapshot_on_beat(args.snapshot_fixture_dir)
        if args.snapshot_fixture_dir is not None
        else None
    )
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
    """Parse arguments and run the requested hedgekit command.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code (0 on success, 1 on a fatal config error).
    """
    args = build_parser().parse_args(argv)
    configure_logging(level=logging.INFO)
    if args.command == "rebuild":
        return rebuild_command(args)
    if args.command == "alert-test":
        return _run_alert_test(args)
    return _run_heartbeat(args)


if __name__ == "__main__":
    raise SystemExit(main())
