"""Fault-injection harness for the Order Gateway chaos suite (issue #42).

Generalizes the crash-simulating doubles that already exist in
`tests/order_gateway/test_recovery.py` (`_KillSwitch`, `_CrashingWal`,
`_CrashingLedgerWriter`, `_CrashingSubmitter`, `SimulatedCrashError`) into a
single, named, composable fault taxonomy (:class:`FaultSpec`) installed at
exactly one `OrderGateway` constructor seam apiece
(`submitter`/`wal`/`ledger_writer`/`status_source`/`reconciliation_source`/
`clock`), plus three new fault kinds the existing recovery suite has no
equivalent for: a network cut isolated to the submitter seam, a duplicate-ACK
WAL redelivery, and reordering/dropping fills observed through
`reconciliation_source`.

:class:`ChaosHarness` (exposed as the `chaos_harness` fixture) assembles a
`PaperExchange` + `OrderGateway` + `Reconciler` + `Sweeper` wired with a
caller-supplied fault list, drives a stream of intents through the
(possibly crash-prone) live Gateway, then "restarts" -- a fresh, un-faulted
Gateway over the *same* durable ledger/WAL/exchange, exactly mirroring
`test_recovery.py`'s own restart pattern -- and runs the Reconciler and
Sweeper to fixpoint. Every step drives the system through its public methods
only; the returned `GatewaySnapshot` (`tests/chaos/invariants.py`) is built
from durable ledger/WAL records and the venue's live truth alone.

Two fault-persistence classes exist, matching what a real process restart
would and would not carry over:

    * *Process-local* faults (`submitter`, `wal`, `ledger_writer`, `clock`
      seams) apply only to the first, possibly-crashing Gateway instance --
      a real crash does not survive its own restart.
    * *Environmental* faults (`status_source`, `reconciliation_source` seams)
      model the venue/feed's own flakiness, so they persist across the
      "restart" into the fresh Gateway, its Reconciler, and its Sweeper too.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hedgekit.connector.models import ExchangeStatus
from hedgekit.ledger.store import SqliteLedgerStore
from hedgekit.numeric.types import ContractCentis, PricePips
from hedgekit.order_gateway.gateway import (
    GatewayHaltedError,
    OrderGateway,
    PaperSubmitter,
)
from hedgekit.order_gateway.ledger_writer import SqliteGatewayLedgerWriter
from hedgekit.order_gateway.reconciler import Reconciler
from hedgekit.order_gateway.sweeper import Sweeper, SweepPolicy
from hedgekit.order_gateway.wal import WriteAheadLog
from tests.chaos.invariants import GatewaySnapshot
from tests.order_gateway.conftest import (
    DEFAULT_MARKET_TICKER,
    DEFAULT_NOW_EPOCH_S,
    KEY_MATERIAL,
    issue_matching_token,
    make_intent,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Literal

    from hedgekit.connector.models import Fill, OpenOrder
    from hedgekit.connector.paper import PaperExchange
    from hedgekit.ledger.events import Event
    from hedgekit.order_gateway.gateway import (
        GatewayPositionSource,
        GatewayResult,
        GatewayStatusSource,
        OrderSubmitter,
        SubmissionAck,
    )
    from hedgekit.order_gateway.ledger_writer import GatewayLedgerWriter
    from hedgekit.order_gateway.reconciler import ReconcileOutcome
    from hedgekit.order_gateway.recovery import ReconciliationSourceProtocol
    from hedgekit.order_gateway.sweeper import SweepOutcome
    from hedgekit.order_gateway.wal import WalRecord, WriteAheadLogProtocol
    from hedgekit.riskkernel.checks import OrderIntent
    from hedgekit.tokens.verify import SignedApprovalToken

#: A fixed observation instant every synthetic `ExchangeStatus` reading
#: stamps its `fetched_at` with -- irrelevant to the paused-status fault, but
#: a real `datetime` is required to construct the value type.
_FIXED_FETCHED_AT = datetime(2024, 1, 1, tzinfo=UTC)

#: The books-fixture directory root every exchange loader resolves under.
_BOOKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "books"

#: Every closed-set fault kind :func:`random_faults` may draw and
#: :class:`ChaosHarness` may install -- one per named SPEC S11.5 scenario
#: family (`kill_after`, `network_cut`, `duplicate_ack`, `reorder_fills`,
#: `drop_fills`), plus two demonstrating the two remaining constructor seams
#: (`exchange_paused` on `status_source`, `clock_skew` on `clock`).
ALL_FAULT_KINDS: tuple[str, ...] = (
    "kill_after",
    "network_cut",
    "duplicate_ack",
    "reorder_fills",
    "drop_fills",
    "exchange_paused",
    "clock_skew",
)


def _load_exchange(*parts: str) -> PaperExchange:
    """Load a `PaperExchange` from a `tests/fixtures/books/<parts>` directory.

    Args:
        *parts: The path segments under `tests/fixtures/books/` naming the
            fixture directory (e.g. `("deep_walk",)` or
            `("volatile_markets", "gap_move")`).

    Returns:
        A freshly loaded `PaperExchange`.
    """
    from hedgekit.connector.paper import PaperExchange

    return PaperExchange.from_fixture_dir(_BOOKS_DIR.joinpath(*parts))


def deep_walk_exchange() -> PaperExchange:
    """Load the single-ticker `deep_walk` fixture (the harness's default).

    Returns:
        A fresh `PaperExchange` whose sole ticker is `DEFAULT_MARKET_TICKER`.
    """
    return _load_exchange("deep_walk")


def gap_move_exchange() -> PaperExchange:
    """Load the two-ticker `volatile_markets/gap_move` move-breach fixture.

    Returns:
        A fresh `PaperExchange` for the sweeper move-breach family.
    """
    return _load_exchange("volatile_markets", "gap_move")


def resting_full_consume_exchange() -> PaperExchange:
    """Load the single-ticker `resting_full_consume` trade-through fixture.

    Returns:
        A fresh `PaperExchange` whose sole resting order fully consumes via a
        recorded trade-through print on one `advance()` call.
    """
    return _load_exchange("resting_full_consume")


def random_intent_stream(
    seed: int, *, n: int, market_ticker: str = DEFAULT_MARKET_TICKER
) -> tuple[OrderIntent, ...]:
    """Build `n` seeded-random, individually valid, brand-new `OrderIntent`s.

    Every intent targets `market_ticker` with its own idempotency key (so
    each is a genuinely distinct economic identity) and a price spanning both
    sides of the `deep_walk` fixture's 4600-pip ask, so both immediate taker
    fills and resting orders occur across a long-enough stream.

    Args:
        seed: The deterministic seed every drawn field derives from.
        n: How many intents to build.
        market_ticker: The ticker every intent targets.

    Returns:
        The `n` generated intents, in draw order.
    """
    rng = random.Random(seed)
    intents = []
    for i in range(n):
        price = PricePips(rng.randint(4000, 4700))
        size = ContractCentis(rng.randint(10, 150))
        intents.append(
            make_intent(
                market_ticker=market_ticker,
                price=price,
                size=size,
                idempotency_key=f"chaos-{seed}-{i}",
            )
        )
    return tuple(intents)


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """One named, composable fault-injection instruction (issue #42, SPEC S11.5).

    Attributes:
        name: A short, human-readable label surfaced in failure output (so a
            failing seed names exactly which faults it composed).
        seam: The single `OrderGateway` (or Reconciler/Sweeper) constructor
            seam this fault installs at.
        kind: The closed-set fault behavior to install.
        kill_after: For `kind="kill_after"`, the 1-based durable-write tick
            (shared across the wal/ledger_writer/submitter seams, mirroring
            `test_recovery.py`'s own kill matrix) that raises
            `SimulatedCrashError`.
        rng_seed: For `kind in ("reorder_fills", "drop_fills")`, the seed the
            installed wrapper's own `random.Random` draws from.
        skew_seconds: For `kind="clock_skew"`, the (possibly negative) offset
            applied to the wrapped clock's base reading.
        drop_ppm: For `kind="drop_fills"`, the per-fill drop probability, in
            parts-per-million (default 1,000,000 -- always drop).
    """

    name: str
    seam: Literal[
        "submitter",
        "wal",
        "ledger_writer",
        "status_source",
        "reconciliation_source",
        "clock",
    ]
    kind: Literal[
        "kill_after",
        "network_cut",
        "duplicate_ack",
        "reorder_fills",
        "drop_fills",
        "exchange_paused",
        "clock_skew",
    ]
    kill_after: int | None = None
    rng_seed: int | None = None
    skew_seconds: int | None = None
    drop_ppm: int = 1_000_000


def random_faults(
    seed: int, *, kinds: Sequence[str] = ALL_FAULT_KINDS, max_faults: int = 2
) -> tuple[FaultSpec, ...]:
    """Draw a small, seeded, composable combination of fault kinds.

    Args:
        seed: The deterministic seed the draw is derived from.
        kinds: The pool of fault kinds eligible to be drawn.
        max_faults: The maximum number of faults to compose in one draw.

    Returns:
        Zero to `max_faults` distinct `FaultSpec`s, in the order they should
        be installed (installation order matters when two faults share a
        seam -- each wraps the previous seam's current value).
    """
    rng = random.Random(seed)
    pool = list(kinds)
    count = rng.randint(0, min(max_faults, len(pool)))
    chosen = rng.sample(pool, k=count)
    return tuple(_fault_for_kind(kind, seed, rng) for kind in chosen)


def _fault_for_kind(kind: str, seed: int, rng: random.Random) -> FaultSpec:
    """Build one concrete `FaultSpec` for a drawn fault `kind`.

    Args:
        kind: The closed-set fault kind drawn by :func:`random_faults`.
        seed: The storm's own seed, folded into the fault's `name`.
        rng: The storm's shared random source, drawn from for any
            kind-specific parameter (e.g. `kill_after`'s tick point).

    Returns:
        The concrete `FaultSpec`.

    Raises:
        ValueError: If `kind` is not one of the closed set this function
            recognizes.
    """
    if kind == "kill_after":
        return FaultSpec(
            name=f"kill-after-{seed}",
            seam="wal",
            kind="kill_after",
            kill_after=rng.randint(1, 7),
        )
    if kind == "network_cut":
        return FaultSpec(
            name=f"network-cut-{seed}", seam="submitter", kind="network_cut"
        )
    if kind == "duplicate_ack":
        return FaultSpec(name=f"duplicate-ack-{seed}", seam="wal", kind="duplicate_ack")
    if kind == "reorder_fills":
        return FaultSpec(
            name=f"reorder-fills-{seed}",
            seam="reconciliation_source",
            kind="reorder_fills",
            rng_seed=seed,
        )
    if kind == "drop_fills":
        return FaultSpec(
            name=f"drop-fills-{seed}",
            seam="reconciliation_source",
            kind="drop_fills",
            rng_seed=seed,
        )
    if kind == "exchange_paused":
        return FaultSpec(
            name=f"exchange-paused-{seed}", seam="status_source", kind="exchange_paused"
        )
    if kind == "clock_skew":
        return FaultSpec(
            name=f"clock-skew-{seed}",
            seam="clock",
            kind="clock_skew",
            skew_seconds=rng.randint(-5, 5),
        )
    raise ValueError(f"unknown fault kind: {kind!r}")


class SimulatedCrashError(Exception):
    """Raised by a fault-injecting wrapper to simulate a mid-operation crash."""


class NetworkCutError(Exception):
    """Raised by `_NetworkCutSubmitter` to simulate an ack lost in transit."""


class KillSwitch:
    """Shared counter raising `SimulatedCrashError` on its Nth `tick()`.

    Generalized from `tests/order_gateway/test_recovery.py`'s `_KillSwitch`
    so the chaos suite's kill-at-every-edge family (and any fault combining
    multiple durable-write seams) shares one implementation rather than
    duplicating it. A `kill_after=None` switch never fires (a harness run
    with no `kill_after` fault installed still safely ticks a real switch).
    """

    def __init__(self, kill_after: int | None) -> None:
        """Initialize, tracking ticks against the configured kill point.

        Args:
            kill_after: The 1-based tick count that raises, or `None` to
                never raise.
        """
        self._kill_after = kill_after
        self._count = 0
        self._armed = kill_after is not None

    def tick(self, label: str) -> None:
        """Record one durable write, raising if it is the configured Nth.

        Args:
            label: A short description of the seam that just wrote durably,
                folded into the raised error for diagnosability.

        Raises:
            SimulatedCrashError: On exactly the `kill_after`-th tick (once
                armed and a `kill_after` is configured).
        """
        self._count += 1
        if (
            self._armed
            and self._kill_after is not None
            and self._count == self._kill_after
        ):
            raise SimulatedCrashError(
                f"simulated crash immediately after {label} "
                f"(durable write #{self._count})"
            )

    def disarm(self) -> None:
        """Suspend crashing (ticks still count) around a clean boot recovery."""
        self._armed = False

    def rearm(self) -> None:
        """Re-arm crashing (if a `kill_after` is configured) and reset the count."""
        self._armed = self._kill_after is not None
        self._count = 0


class _KillSwitchWal:
    """A `WriteAheadLogProtocol`-shaped wrapper ticking a shared `KillSwitch`."""

    def __init__(self, inner: WriteAheadLogProtocol, kill_switch: KillSwitch) -> None:
        """Bind the wrapper to the real WAL and the shared kill switch.

        Args:
            inner: The real write-ahead log every call delegates to first.
            kill_switch: The shared counter ticked after each durable append.
        """
        self._inner = inner
        self._kill_switch = kill_switch

    def append_intent(self, intent: OrderIntent, client_order_id: str, /) -> None:
        """Durably append the intent, then tick the shared kill switch.

        Args:
            intent: The order intent to journal.
            client_order_id: The intent's content-addressed id.
        """
        self._inner.append_intent(intent, client_order_id)
        self._kill_switch.tick("wal_intent")

    def append_ack(
        self, client_order_id: str, order_id: str | None, filled: ContractCentis, /
    ) -> None:
        """Durably append the ack, then tick the shared kill switch.

        Args:
            client_order_id: The intent's content-addressed id.
            order_id: The venue's resting-order id, or `None`.
            filled: The quantity filled immediately, in contract-centis.
        """
        self._inner.append_ack(client_order_id, order_id, filled)
        self._kill_switch.tick("wal_ack")

    def read_all(self) -> tuple[WalRecord, ...]:
        """Delegate to the real WAL's `read_all()`.

        Returns:
            Whatever the real `WriteAheadLog.read_all()` returns.
        """
        return self._inner.read_all()


class _KillSwitchLedgerWriter:
    """A `GatewayLedgerWriter`-shaped wrapper ticking a shared `KillSwitch`."""

    def __init__(self, inner: GatewayLedgerWriter, kill_switch: KillSwitch) -> None:
        """Bind the wrapper to the real ledger writer and the kill switch.

        Args:
            inner: The real ledger writer every call delegates to.
            kill_switch: The shared counter ticked after each durable write.
        """
        self._inner = inner
        self._kill_switch = kill_switch

    def record(self, event: Event) -> None:
        """Durably record `event`, then tick the shared kill switch.

        Args:
            event: The ledger event to record.
        """
        self._inner.record(event)
        self._kill_switch.tick(f"ledger_{event.event_type}")


class _KillSwitchSubmitter:
    """An `OrderSubmitter`-shaped wrapper ticking a shared `KillSwitch`."""

    def __init__(self, inner: OrderSubmitter, kill_switch: KillSwitch) -> None:
        """Bind the wrapper to the real submitter and the kill switch.

        Args:
            inner: The real submitter every call delegates to first.
            kill_switch: The shared counter ticked after each placement.
        """
        self._inner = inner
        self._kill_switch = kill_switch

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Durably place the order, then tick the shared kill switch.

        Args:
            intent: The verified order intent to submit.
            token: The approval token that authorized it.

        Returns:
            The real submitter's `SubmissionAck`.
        """
        ack = self._inner.submit(intent, token)
        self._kill_switch.tick("submit")
        return ack


class _NetworkCutSubmitter:
    """Simulates a network cut immediately after the venue accepts an order.

    The wrapped `submit()` genuinely places the order (the venue-side effect
    is real), then raises before the ack ever reaches the Gateway --
    mirroring the existing kill matrix's "after-exchange-place-pre-wal-ack"
    edge, isolated to the submitter seam alone (no wal/ledger tick coupling)
    so it composes cleanly with other faults (the network-cut-mid-submit
    family, SPEC S11.5).
    """

    def __init__(self, inner: OrderSubmitter) -> None:
        """Bind the wrapper to the real submitter.

        Args:
            inner: The real submitter the placement is genuinely made through.
        """
        self._inner = inner

    def submit(self, intent: OrderIntent, token: SignedApprovalToken) -> SubmissionAck:
        """Place the order for real, then raise before returning its ack.

        Args:
            intent: The verified order intent to submit.
            token: The approval token that authorized it.

        Raises:
            NetworkCutError: Always -- the placement already happened.
        """
        self._inner.submit(intent, token)
        raise NetworkCutError(
            "network cut: the venue accepted the order but its ack was lost in transit"
        )


class _DuplicateAckWal:
    """Wraps a WAL, durably appending every ack record *twice*.

    Models an acknowledgement redelivered at the transport/infra layer,
    landing twice in the append-only write-ahead log -- the duplicate-ACK
    family (SPEC S11.5). The plain `WriteAheadLog` carries no idempotency
    guard against an exogenous duplicate append (its own module docstring:
    one canonical-JSON line per append, nothing more), so this fault
    specifically exercises whether the Gateway's own recovery/rehydration
    stays correct under that duplication.
    """

    def __init__(self, inner: WriteAheadLogProtocol) -> None:
        """Bind the wrapper to the real WAL.

        Args:
            inner: The real write-ahead log every call delegates to.
        """
        self._inner = inner

    def append_intent(self, intent: OrderIntent, client_order_id: str, /) -> None:
        """Durably append the intent exactly once (unaffected by this fault).

        Args:
            intent: The order intent to journal.
            client_order_id: The intent's content-addressed id.
        """
        self._inner.append_intent(intent, client_order_id)

    def append_ack(
        self, client_order_id: str, order_id: str | None, filled: ContractCentis, /
    ) -> None:
        """Durably append the identical ack record twice.

        Args:
            client_order_id: The intent's content-addressed id.
            order_id: The venue's resting-order id, or `None`.
            filled: The quantity filled immediately, in contract-centis.
        """
        self._inner.append_ack(client_order_id, order_id, filled)
        self._inner.append_ack(client_order_id, order_id, filled)

    def read_all(self) -> tuple[WalRecord, ...]:
        """Delegate to the real WAL's `read_all()`.

        Returns:
            Whatever the real `WriteAheadLog.read_all()` returns.
        """
        return self._inner.read_all()


class _ReorderingReconciliationSource:
    """Wraps a reconciliation source, shuffling `get_fills()`'s returned order.

    `matched_fill_centis` sums fill quantity by ticker/side/price regardless
    of order, so a correct Reconciler/Sweeper must be indifferent to fill
    presentation order; this fault (the out-of-order-fills family) proves it.
    """

    def __init__(self, inner: ReconciliationSourceProtocol, seed: int) -> None:
        """Bind the wrapper to the real source and seed its shuffle.

        Args:
            inner: The real reconciliation source every call delegates to.
            seed: The deterministic seed the shuffle order derives from.
        """
        self._inner = inner
        self._rng = random.Random(seed)

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Delegate to the real source's `get_open_orders()` unchanged.

        Returns:
            The real source's open orders.
        """
        return self._inner.get_open_orders()

    def get_fills(self, since: datetime, /) -> tuple[Fill, ...]:
        """Return the real source's fills, seed-shuffled into a new order.

        Args:
            since: The exclusive lower bound on fill time.

        Returns:
            The same fills the real source reports, reordered.
        """
        fills = list(self._inner.get_fills(since))
        self._rng.shuffle(fills)
        return tuple(fills)


class _DroppingReconciliationSource:
    """Wraps a reconciliation source, dropping a seeded fraction of fills.

    Models a fill-notification feed that silently loses some deliveries --
    the missed-fill family (SPEC S11.5). Under the Gateway's own closed
    allowlist (SPEC S3.2 -- when in doubt, halt), a dropped fill on a
    vanished tracked order is indistinguishable from a genuine anomaly and
    correctly halts fail-closed rather than guessing; convergence is that
    halt, not a silent miscount.
    """

    def __init__(
        self, inner: ReconciliationSourceProtocol, seed: int, *, drop_ppm: int
    ) -> None:
        """Bind the wrapper to the real source and its drop probability.

        Args:
            inner: The real reconciliation source every call delegates to.
            seed: The deterministic seed the per-fill drop draw derives from.
            drop_ppm: The per-fill drop probability, in parts-per-million.
        """
        self._inner = inner
        self._rng = random.Random(seed)
        self._drop_ppm = drop_ppm

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Delegate to the real source's `get_open_orders()` unchanged.

        Returns:
            The real source's open orders.
        """
        return self._inner.get_open_orders()

    def get_fills(self, since: datetime, /) -> tuple[Fill, ...]:
        """Return the real source's fills with a seeded subset dropped.

        Args:
            since: The exclusive lower bound on fill time.

        Returns:
            The surviving (non-dropped) fills.
        """
        return tuple(
            fill
            for fill in self._inner.get_fills(since)
            if self._rng.randrange(1_000_000) >= self._drop_ppm
        )


class _FixedFillsReconciliationSource:
    """Wraps a reconciliation source, substituting a caller-fixed fill tuple.

    Gives a test full, deterministic control over exactly which fills
    `get_fills()` reports (and in what order), independent of fixture
    arithmetic -- used by the out-of-order-fills scenario to prove a
    multi-fill heal is insensitive to presentation order.
    """

    def __init__(
        self, inner: ReconciliationSourceProtocol, fills: tuple[Fill, ...]
    ) -> None:
        """Bind the wrapper to the real source and the fixed fill tuple.

        Args:
            inner: The real reconciliation source `get_open_orders()`
                delegates to.
            fills: The exact fills `get_fills()` returns, verbatim.
        """
        self._inner = inner
        self._fills = fills

    def get_open_orders(self) -> tuple[OpenOrder, ...]:
        """Delegate to the real source's `get_open_orders()` unchanged.

        Returns:
            The real source's open orders.
        """
        return self._inner.get_open_orders()

    def get_fills(self, since: datetime, /) -> tuple[Fill, ...]:
        """Return the caller-fixed fill tuple, ignoring `since`.

        Args:
            since: Ignored.

        Returns:
            The fixed fill tuple supplied at construction.
        """
        del since
        return self._fills


class _AlwaysPausedStatusSource:
    """A `GatewayStatusSource` reporting the exchange permanently paused."""

    def get_exchange_status(self) -> ExchangeStatus | None:
        """Return a fixed `"paused"` status reading.

        Returns:
            An `ExchangeStatus` with `status="paused"`.
        """
        return ExchangeStatus(status="paused", fetched_at=_FIXED_FETCHED_AT)


class _SkewedClock:
    """A clock double offset by a fixed (possibly negative) number of seconds."""

    def __init__(self, base: int, skew_seconds: int) -> None:
        """Capture the skewed reading once, at construction.

        Args:
            base: The unskewed epoch-second reading to offset.
            skew_seconds: The (possibly negative) offset to apply.
        """
        self._value = base + skew_seconds

    def __call__(self) -> int:
        """Return the fixed, skewed epoch second.

        Returns:
            `base + skew_seconds`, captured at construction.
        """
        return self._value


@dataclass
class _Seams:
    """The six `OrderGateway`/Reconciler/Sweeper constructor seams a fault
    installs at, mutated in place as each `FaultSpec` is applied in order.

    Attributes:
        submitter: The seam verified orders are submitted through.
        wal: The seam intents/acks are durably journalled through.
        ledger_writer: The seam every transition/event is recorded through.
        status_source: The seam the exchange trading status is read through.
        reconciliation_source: The seam the venue's live open orders/fills
            are read through.
        clock: The zero-argument callable returning the current epoch second.
    """

    submitter: OrderSubmitter
    wal: WriteAheadLogProtocol
    ledger_writer: GatewayLedgerWriter
    status_source: GatewayStatusSource | None
    reconciliation_source: ReconciliationSourceProtocol
    clock: Callable[[], int]


def _apply_fault(seams: _Seams, fault: FaultSpec, kill_switch: KillSwitch) -> None:
    """Install one `FaultSpec` onto `seams`, mutating it in place.

    Args:
        seams: The seam bundle to mutate.
        fault: The fault to install.
        kill_switch: The shared kill switch every `kind="kill_after"` fault
            (there may be several composed faults, but only one shared
            switch, matching `test_recovery.py`'s own single-counter design)
            ticks against.

    Raises:
        ValueError: If `fault.kind` is not one of the closed set this
            function recognizes.
    """
    if fault.kind == "kill_after":
        seams.wal = _KillSwitchWal(seams.wal, kill_switch)
        seams.ledger_writer = _KillSwitchLedgerWriter(seams.ledger_writer, kill_switch)
        seams.submitter = _KillSwitchSubmitter(seams.submitter, kill_switch)
    elif fault.kind == "network_cut":
        seams.submitter = _NetworkCutSubmitter(seams.submitter)
    elif fault.kind == "duplicate_ack":
        seams.wal = _DuplicateAckWal(seams.wal)
    elif fault.kind == "reorder_fills":
        seams.reconciliation_source = _ReorderingReconciliationSource(
            seams.reconciliation_source, fault.rng_seed or 0
        )
    elif fault.kind == "drop_fills":
        seams.reconciliation_source = _DroppingReconciliationSource(
            seams.reconciliation_source, fault.rng_seed or 0, drop_ppm=fault.drop_ppm
        )
    elif fault.kind == "exchange_paused":
        seams.status_source = _AlwaysPausedStatusSource()
    elif fault.kind == "clock_skew":
        seams.clock = _SkewedClock(seams.clock(), fault.skew_seconds or 0)
    else:
        raise ValueError(f"unknown fault kind: {fault.kind!r}")


@dataclass(slots=True)
class ChaosRun:
    """The result of one `ChaosHarness.run()` scenario, settled to quiescence.

    Attributes:
        snapshot: The final public-state snapshot, ready for
            `tests.chaos.invariants.assert_all_invariants`.
        halted: Whether the post-quiescence (restarted) Gateway ended up
            halted -- a fail-closed halt is itself convergence, not a failure.
        raised: Every fault exception `process_intent` raised while driving
            the live (pre-restart) Gateway through `intents`, in order.
    """

    snapshot: GatewaySnapshot
    halted: bool
    raised: tuple[BaseException, ...]


class ChaosHarness:
    """Drives one Order Gateway chaos scenario to quiescence (issue #42).

    Assembles a `PaperExchange` + `OrderGateway` + `Reconciler` + `Sweeper`
    wired with a caller-supplied, composable fault list, drives a stream of
    intents through the (possibly crash-prone) live Gateway, then "restarts"
    -- a fresh, un-faulted Gateway over the *same* durable ledger/WAL/exchange
    -- and runs it to quiescence, mirroring
    `tests/order_gateway/test_recovery.py`'s own restart pattern. Every step
    drives the system through its public methods only.
    """

    def __init__(self, tmp_path: Path) -> None:
        """Bind the harness to this test's scratch directory.

        Args:
            tmp_path: The pytest-provided scratch directory the ledger DB and
                WAL file are created under.
        """
        self._tmp_path = tmp_path

    def run(
        self,
        *,
        intents: Sequence[OrderIntent],
        faults: Sequence[FaultSpec] = (),
        exchange_factory: Callable[[], PaperExchange] = deep_walk_exchange,
        position_source: GatewayPositionSource | None = None,
        advance_cycles: int = 0,
        reconcile_cycles: int = 5,
        sweep_cycles: int = 5,
        sweep_policy: SweepPolicy | None = None,
        sweeper_now: int = DEFAULT_NOW_EPOCH_S,
        reconciliation_source_factory: (
            Callable[[ReconciliationSourceProtocol], ReconciliationSourceProtocol]
            | None
        ) = None,
        before_reconcile: (
            Callable[[PaperExchange, list[GatewayResult]], None] | None
        ) = None,
    ) -> ChaosRun:
        """Drive one full chaos scenario to quiescence and return its result.

        Args:
            intents: The stream of order intents to drive through the live
                (pre-restart) Gateway, each freshly token-minted.
            faults: The composable fault list to install before driving
                `intents`. Faults on `submitter`/`wal`/`ledger_writer`/
                `clock` apply only to the pre-restart Gateway; faults on
                `status_source`/`reconciliation_source` persist across the
                restart into the Reconciler and Sweeper too.
            exchange_factory: Builds the fresh `PaperExchange` the scenario
                runs against. Defaults to the single-ticker `deep_walk`
                fixture.
            position_source: The reduce-only position source to wire (both
                pre- and post-restart), or `None` to leave enforcement off.
            advance_cycles: How many times to call `PaperExchange.advance()`
                after the restart's own `.recover()` and before reconciling/
                sweeping -- the cancel/fill-race and missed-fill families'
                out-of-band consumption. Deliberately timed *after*
                `.recover()` (see `_settle`'s docstring) so the restarted
                Gateway first adopts the still-resting order as tracked.
            reconcile_cycles: The maximum number of `Reconciler.run_once()`
                cycles to run to fixpoint (bounded; never unbounded).
            sweep_cycles: The maximum number of `Sweeper.sweep_once()` cycles
                to run to fixpoint (bounded; never unbounded).
            sweep_policy: The Sweeper's TTL/move-tick policy; defaults to
                `SweepPolicy()`.
            sweeper_now: The fixed epoch second the post-restart Sweeper's
                clock reports, independent of the Gateway's own clock --
                lets a scenario force TTL staleness deterministically.
            reconciliation_source_factory: An escape hatch applied *after*
                every `faults` entry, wrapping the (possibly already
                fault-wrapped) `reconciliation_source` one more time -- lets a
                scenario substitute a fully deterministic, hand-built fill
                feed (e.g. `_FixedFillsReconciliationSource`) that no seeded
                `FaultSpec` can express.
            before_reconcile: Called once, after the restarted Gateway's own
                `.recover()` (and any `advance_cycles`) but before the
                Reconciler/Sweeper run, with the live exchange and every
                `GatewayResult` the pre-restart Gateway collected -- lets a
                scenario bypass the Gateway to mutate the venue directly
                (e.g. `exchange.cancel_order(...)`, mirroring
                `test_reconciler.py`'s/`test_sweeper.py`'s own out-of-band
                cancellation idiom) using the real order ids `process_intent`
                returned, *after* recovery has already adopted the order as
                tracked (see the `advance_cycles` timing note above -- the
                same reasoning applies here).

        Returns:
            The scenario's `ChaosRun`.
        """
        exchange = exchange_factory()
        db_path = self._tmp_path / "ledger.db"
        wal_path = self._tmp_path / "wal.jsonl"
        store = SqliteLedgerStore(db_path)
        wal = WriteAheadLog(wal_path)
        kill_after = next(
            (fault.kill_after for fault in faults if fault.kind == "kill_after"), None
        )
        kill_switch = KillSwitch(kill_after)
        seams = _Seams(
            submitter=PaperSubmitter(exchange),
            wal=wal,
            ledger_writer=SqliteGatewayLedgerWriter(store),
            status_source=None,
            reconciliation_source=exchange,
            clock=lambda: DEFAULT_NOW_EPOCH_S,
        )
        for fault in faults:
            _apply_fault(seams, fault, kill_switch)
        if reconciliation_source_factory is not None:
            seams.reconciliation_source = reconciliation_source_factory(
                seams.reconciliation_source
            )

        gateway_a = OrderGateway(
            seams.submitter,
            verification_key=KEY_MATERIAL,
            clock=seams.clock,
            ledger_writer=seams.ledger_writer,
            wal=seams.wal,
            ledger_reader=store,
            reconciliation_source=seams.reconciliation_source,
            status_source=seams.status_source,
            position_source=position_source,
        )
        # A clean boot recovery ledgers its own RecoveryCompleted checkpoint
        # through the (possibly kill-switched) writer; disarm around it so a
        # `kill_after` fault's tick count starts fresh at the first intent,
        # mirroring `test_recovery.py`'s own disarm/rearm idiom.
        kill_switch.disarm()
        gateway_a.recover()
        kill_switch.rearm()

        raised: list[BaseException] = []
        results: list[GatewayResult] = []
        for intent in intents:
            token = issue_matching_token(intent)
            try:
                results.append(gateway_a.process_intent(intent, token))
            except (SimulatedCrashError, NetworkCutError, GatewayHaltedError) as exc:
                # A real crash (or a live fail-closed halt) ends the process;
                # further intents would only ever raise again.
                raised.append(exc)
                break

        store.close()

        snapshot, halted = self._settle(
            db_path=db_path,
            wal_path=wal_path,
            exchange=exchange,
            reconciliation_source=seams.reconciliation_source,
            status_source=seams.status_source,
            position_source=position_source,
            advance_cycles=advance_cycles,
            reconcile_cycles=reconcile_cycles,
            sweep_cycles=sweep_cycles,
            sweep_policy=sweep_policy,
            sweeper_now=sweeper_now,
            before_reconcile=before_reconcile,
            results=results,
        )
        return ChaosRun(snapshot=snapshot, halted=halted, raised=tuple(raised))

    def _settle(
        self,
        *,
        db_path: Path,
        wal_path: Path,
        exchange: PaperExchange,
        reconciliation_source: ReconciliationSourceProtocol,
        status_source: GatewayStatusSource | None,
        position_source: GatewayPositionSource | None,
        advance_cycles: int,
        reconcile_cycles: int,
        sweep_cycles: int,
        sweep_policy: SweepPolicy | None,
        sweeper_now: int,
        before_reconcile: Callable[[PaperExchange, list[GatewayResult]], None] | None,
        results: list[GatewayResult],
    ) -> tuple[GatewaySnapshot, bool]:
        """Restart over the durable state and run to a bounded fixpoint.

        `advance_cycles` and `before_reconcile` deliberately run *after* the
        restarted Gateway's own `.recover()` -- not before -- so `.recover()`
        first adopts any still-resting order as tracked (exactly as a real
        restart racing a still-pending fill would), and only *then* does the
        venue consume or cancel it out-of-band. Running them before the
        restart would instead have `.recover()` see the order already gone
        and correctly decline to (re)track it at all, silently erasing the
        very race the cancel/fill-race and missed-fill families need the
        Reconciler/Sweeper to resolve.

        Args:
            db_path: The ledger database path the pre-restart Gateway wrote.
            wal_path: The write-ahead log path the pre-restart Gateway wrote.
            exchange: The (still-live) paper exchange to reconcile against.
            reconciliation_source: The (possibly fault-wrapped) venue-truth
                seam, reused unchanged across the restart.
            status_source: The (possibly fault-wrapped) status seam, reused
                unchanged across the restart.
            position_source: The reduce-only position source, or `None`.
            advance_cycles: How many times to call `PaperExchange.advance()`
                after `.recover()` and before reconciling/sweeping.
            reconcile_cycles: The bounded maximum reconcile-cycle count.
            sweep_cycles: The bounded maximum sweep-cycle count.
            sweep_policy: The Sweeper's policy, or `None` for the default.
            sweeper_now: The Sweeper's fixed clock reading.
            before_reconcile: Optional callback given the live exchange and
                the pre-restart `GatewayResult`s, run after `.recover()`/
                `advance_cycles` and before the Reconciler/Sweeper.
            results: The pre-restart Gateway's collected `GatewayResult`s,
                forwarded to `before_reconcile`.

        Returns:
            The settled `GatewaySnapshot` and the restarted Gateway's final
            `halted` flag.
        """
        fresh_store = SqliteLedgerStore(db_path)
        fresh_wal = WriteAheadLog(wal_path)
        gateway_b = OrderGateway(
            PaperSubmitter(exchange),
            verification_key=KEY_MATERIAL,
            clock=lambda: DEFAULT_NOW_EPOCH_S,
            ledger_writer=SqliteGatewayLedgerWriter(fresh_store),
            wal=fresh_wal,
            ledger_reader=fresh_store,
            reconciliation_source=reconciliation_source,
            status_source=status_source,
            position_source=position_source,
        )
        gateway_b.recover()

        for _ in range(advance_cycles):
            exchange.advance()

        if before_reconcile is not None:
            before_reconcile(exchange, results)

        self._run_reconciler_to_fixpoint(
            gateway_b, fresh_store, reconciliation_source, reconcile_cycles
        )
        self._run_sweeper_to_fixpoint(
            gateway_b,
            exchange,
            fresh_store,
            reconciliation_source,
            sweep_cycles,
            sweep_policy,
            sweeper_now,
        )

        snapshot = GatewaySnapshot(
            ledger_records=fresh_store.read_all(),
            wal_records=fresh_wal.read_all(),
            open_orders=exchange.get_open_orders(),
            positions=(
                position_source.get_positions() if position_source is not None else ()
            ),
        )
        halted = gateway_b.halted
        fresh_store.close()
        return snapshot, halted

    def _run_reconciler_to_fixpoint(
        self,
        gateway: OrderGateway,
        store: SqliteLedgerStore,
        reconciliation_source: ReconciliationSourceProtocol,
        max_cycles: int,
    ) -> None:
        """Run `Reconciler.run_once()` until a halt or a repeated outcome.

        Args:
            gateway: The restarted Gateway the Reconciler reconciles.
            store: The durable ledger, read and written through.
            reconciliation_source: The (possibly fault-wrapped) venue-truth
                seam.
            max_cycles: The bounded maximum number of cycles to run.
        """
        reconciler = Reconciler(
            gateway,
            ledger_reader=store,
            reconciliation_source=reconciliation_source,
            ledger_writer=SqliteGatewayLedgerWriter(store),
        )
        previous: ReconcileOutcome | None = None
        for _ in range(max_cycles):
            if gateway.halted:
                break
            outcome = reconciler.run_once()
            if outcome.halted or outcome == previous:
                break
            previous = outcome

    def _run_sweeper_to_fixpoint(
        self,
        gateway: OrderGateway,
        exchange: PaperExchange,
        store: SqliteLedgerStore,
        reconciliation_source: ReconciliationSourceProtocol,
        max_cycles: int,
        policy: SweepPolicy | None,
        now: int,
    ) -> None:
        """Run `Sweeper.sweep_once()` until a halt or a repeated outcome.

        Args:
            gateway: The restarted Gateway the Sweeper sweeps.
            exchange: The paper exchange serving as `canceller`/`price_source`.
            store: The durable ledger, read and written through.
            reconciliation_source: The (possibly fault-wrapped) venue-truth
                seam.
            max_cycles: The bounded maximum number of cycles to run.
            policy: The TTL/move-tick policy, or `None` for the default.
            now: The Sweeper's fixed clock reading.
        """
        sweeper = Sweeper(
            gateway,
            canceller=exchange,
            price_source=exchange,
            reconciliation_source=reconciliation_source,
            ledger_reader=store,
            ledger_writer=SqliteGatewayLedgerWriter(store),
            policy=policy if policy is not None else SweepPolicy(),
            clock=lambda: now,
        )
        previous: SweepOutcome | None = None
        for _ in range(max_cycles):
            if gateway.halted:
                break
            outcome = sweeper.sweep_once()
            if outcome == previous:
                break
            previous = outcome


@pytest.fixture
def chaos_harness(tmp_path: Path) -> ChaosHarness:
    """Provide a fresh `ChaosHarness` bound to this test's `tmp_path`.

    Args:
        tmp_path: The pytest-provided per-test scratch directory.

    Returns:
        A `ChaosHarness` ready for `.run(intents=..., faults=...)`.
    """
    return ChaosHarness(tmp_path)
