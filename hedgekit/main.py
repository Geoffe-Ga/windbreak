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
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from hedgekit.ledger import rebuild_command

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import FrameType

#: Operating mode reported in every heartbeat line. Matches the RESEARCH state
#: of the SPEC mode machine; hedgekit ships research-only for now.
MODE_RESEARCH = "RESEARCH"

#: Seconds between heartbeats when ``--heartbeat-interval`` is omitted.
_DEFAULT_HEARTBEAT_INTERVAL = 5.0

#: Log-record format for the console handler installed by :func:`main`.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: Shutdown reason logged when the loop exhausts its ``--max-beats`` budget.
_REASON_MAX_BEATS = "max_beats"

#: Shutdown reason logged when the loop is stopped via its stop event.
_REASON_SIGNAL = "signal"

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


def build_parser() -> argparse.ArgumentParser:
    """Build the ``hedgekit`` command-line argument parser.

    Returns:
        A parser with a required ``run`` subcommand exposing
        ``--heartbeat-interval`` and ``--max-beats``, plus a ``rebuild``
        subcommand exposing ``--ledger-path`` and ``--output-dir``.
    """
    parser = argparse.ArgumentParser(
        prog="hedgekit",
        description="hedgekit always-on forecast trader CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Start the heartbeat loop.")
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
    rebuild_parser = subparsers.add_parser(
        "rebuild", help="Rebuild derived read models from the ledger."
    )
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
    return parser


def run_loop(
    interval_seconds: float,
    *,
    max_beats: int | None = None,
    stop_event: threading.Event | None = None,
    state: ShutdownState | None = None,
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
        _LOGGER.info("mode=%s heartbeat seq=%d", MODE_RESEARCH, seq)
        stop_event.wait(interval_seconds)

    _LOGGER.info("shutdown reason=%s", reason)


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


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the requested hedgekit command.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code (0 on success).
    """
    args = build_parser().parse_args(argv)
    if args.command == "rebuild":
        return rebuild_command(args)
    logging.basicConfig(
        stream=sys.stderr,
        force=True,
        level=logging.INFO,
        format=_LOG_FORMAT,
    )
    state = ShutdownState()
    _install_signal_handlers(state)
    run_loop(
        args.heartbeat_interval,
        max_beats=args.max_beats,
        stop_event=state.stop_event,
        state=state,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
