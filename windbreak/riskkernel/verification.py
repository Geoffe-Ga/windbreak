"""Read-only exchange verification for the Risk Kernel (SPEC S5.2 / S10.3).

Each cycle the kernel cross-checks a read-only :class:`MarketConnector`'s
exchange-verified balances, positions, and open orders against
ledger-derived :class:`LedgerExpectations`, classifies the result as
:class:`VerificationOutcome` ``CLEAN`` / ``DRIFT_WITHIN_TOLERANCE`` / ``BREACH``,
fires an operator alert for any held market whose jurisdiction is unknown, fires
a reconciliation-mismatch alert on a breach, and records exactly one bare
:class:`~windbreak.ledger.events.Event` per cycle. The resulting
:class:`VerificationSnapshot` feeds both the kernel's HALT-on-breach path and
the floor computation (its observed cash and drift rewrite the account's
verified-cash and reconciliation-buffer terms).

Balance and position drift are tolerance-graded: a nonzero diff within tolerance
is drift (still ``ok``), a diff beyond tolerance is a breach. Open orders are
discrete, so any set difference at all is a breach. Every value on this path is
a :mod:`windbreak.numeric` scaled integer -- never a float (SPEC S6.1, enforced
by ``scripts/lint_no_floats.py``) -- and every recorded payload leaf is an
``int``, ``str``, or ``bool``.

:class:`KernelLedgerWriter` and :class:`MarketConnector` are imported under
``TYPE_CHECKING`` only: both are structural protocols, so importing them for
typing alone keeps this module free of a runtime ``verification`` <-> ``process``
import cycle.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from windbreak.alerts.registry import AlertType
from windbreak.ledger.events import Event
from windbreak.numeric.types import MoneyMicros

if TYPE_CHECKING:
    from collections.abc import Mapping

    from windbreak.alerts.dispatch import AlertDispatcher
    from windbreak.connector.interface import MarketConnector
    from windbreak.connector.models import OpenOrder, Position
    from windbreak.numeric.types import ContractCentis
    from windbreak.riskkernel.process import KernelLedgerWriter

#: Component label stamped on every verification event this module records.
_COMPONENT = "riskkernel"

#: Payload schema version stamped on every verification event.
_PAYLOAD_SCHEMA_VERSION = 1

#: The :class:`~windbreak.connector.models.NormalizedMarket.jurisdiction_status`
#: value that must fire a warning alert for any held market carrying it.
_UNKNOWN_JURISDICTION = "unknown"

#: The reconciliation-mismatch alert body dispatched on a breach.
_MISMATCH_MESSAGE = "exchange verification mismatch beyond tolerance"


@dataclass(frozen=True, slots=True)
class LedgerExpectations:
    """The ledger-derived state one verification cycle checks the venue against.

    Attributes:
        expected_available_cash: The available cash the ledger expects the
            venue to report, in micros.
        expected_positions: The per-ticker net position the ledger expects, in
            contract-centis; a ticker absent from the mapping is expected flat.
        expected_open_order_ids: The exact set of resting-order ids the ledger
            expects the venue to report.
    """

    expected_available_cash: MoneyMicros
    expected_positions: Mapping[str, ContractCentis]
    expected_open_order_ids: frozenset[str]


class ExpectationSource(Protocol):
    """The seam a verifier reads its :class:`LedgerExpectations` from."""

    def get_expectations(self) -> LedgerExpectations:
        """Return the current ledger-derived expectations.

        Returns:
            The :class:`LedgerExpectations` to verify the venue against.
        """
        ...


@dataclass(frozen=True, slots=True)
class VerificationTolerances:
    """The admissible drift before a dimension is treated as a breach.

    Open orders are discrete and carry no tolerance: any set difference is a
    breach.

    Attributes:
        balance_tolerance: The maximum admissible available-cash drift, in
            micros (inclusive: a diff equal to it is still within tolerance).
        position_tolerance: The maximum admissible per-ticker position drift,
            in contract-centis (inclusive).
    """

    balance_tolerance: MoneyMicros
    position_tolerance: ContractCentis


class VerificationOutcome(enum.Enum):
    """The graded outcome of one verification cycle.

    Attributes:
        CLEAN: Every dimension matched its expectation exactly.
        DRIFT_WITHIN_TOLERANCE: A balance or position dimension drifted, but
            every drift stayed within its tolerance; not a breach.
        BREACH: At least one dimension drifted beyond tolerance, or the
            open-order sets differed at all.
    """

    CLEAN = enum.auto()
    DRIFT_WITHIN_TOLERANCE = enum.auto()
    BREACH = enum.auto()


@dataclass(frozen=True, slots=True)
class VerificationSnapshot:
    """The immutable result of one verification cycle.

    Attributes:
        outcome: The graded :class:`VerificationOutcome` of the cycle.
        balance_ok: Whether the available-cash drift was within tolerance.
        position_ok: Whether every per-ticker position drift was within
            tolerance.
        open_order_ok: Whether the observed and expected open-order id sets
            matched exactly.
        verified_at_epoch_s: The epoch second the cycle ran at.
        exchange_verified_available_cash: The venue-reported available cash the
            cycle observed, in micros.
        cash_drift: The absolute available-cash drift the cycle observed, in
            micros.
        semantics_fully_known: Whether every balance-semantics field the venue
            reported is a known (non-``UNKNOWN``) value.
    """

    outcome: VerificationOutcome
    balance_ok: bool
    position_ok: bool
    open_order_ok: bool
    verified_at_epoch_s: int
    exchange_verified_available_cash: MoneyMicros
    cash_drift: MoneyMicros
    semantics_fully_known: bool


#: Maps each outcome to the bare-``Event`` ``event_type`` recorded for it.
_EVENT_TYPE_BY_OUTCOME: dict[VerificationOutcome, str] = {
    VerificationOutcome.CLEAN: "VerificationPassed",
    VerificationOutcome.DRIFT_WITHIN_TOLERANCE: "VerificationDrift",
    VerificationOutcome.BREACH: "VerificationMismatch",
}


def _classify(
    *,
    balance_ok: bool,
    position_ok: bool,
    open_order_ok: bool,
    drift_present: bool,
) -> VerificationOutcome:
    """Grade a cycle from its per-dimension ok flags and drift presence.

    Args:
        balance_ok: Whether the balance drift was within tolerance.
        position_ok: Whether every position drift was within tolerance.
        open_order_ok: Whether the open-order sets matched exactly.
        drift_present: Whether any nonzero (but tolerable) drift was observed.

    Returns:
        ``BREACH`` if any dimension failed, else ``DRIFT_WITHIN_TOLERANCE`` if a
        tolerable drift was present, else ``CLEAN``.
    """
    if not (balance_ok and position_ok and open_order_ok):
        return VerificationOutcome.BREACH
    if drift_present:
        return VerificationOutcome.DRIFT_WITHIN_TOLERANCE
    return VerificationOutcome.CLEAN


class ReadOnlyVerifier:
    """Cross-checks a read-only venue against ledger expectations each cycle."""

    def __init__(
        self,
        connector: MarketConnector,
        expectation_source: ExpectationSource,
        tolerances: VerificationTolerances,
        dispatcher: AlertDispatcher,
        ledger_writer: KernelLedgerWriter,
    ) -> None:
        """Initialize the verifier.

        Args:
            connector: The read-only market connector to observe the venue
                through.
            expectation_source: The seam supplying ledger-derived expectations.
            tolerances: The per-dimension drift tolerances.
            dispatcher: The alert dispatcher jurisdiction and mismatch alerts
                fan out through.
            ledger_writer: The seam each cycle's single event is recorded
                through.
        """
        self._connector = connector
        self._expectation_source = expectation_source
        self._tolerances = tolerances
        self._dispatcher = dispatcher
        self._ledger_writer = ledger_writer

    def run_cycle(self, now_epoch_s: int) -> VerificationSnapshot:
        """Run one verification cycle and return its snapshot.

        Fetches the venue's balances, positions, open orders, and balance
        semantics; diffs each against the ledger expectations; grades the
        outcome; fires a jurisdiction-unknown alert for each held market whose
        jurisdiction is unknown and a reconciliation-mismatch alert on a breach;
        and records exactly one bare event.

        Args:
            now_epoch_s: The epoch second to stamp the snapshot at.

        Returns:
            The :class:`VerificationSnapshot` describing the cycle.
        """
        expectations = self._expectation_source.get_expectations()
        positions = self._connector.get_positions()
        open_orders = self._connector.get_open_orders()
        observed_cash = self._connector.get_balances().available
        semantics_known = self._connector.get_balance_semantics().is_fully_known()

        cash_drift = self._cash_drift(observed_cash, expectations)
        position_drift = self._position_drift(positions, expectations)
        open_order_ok = self._open_orders_match(open_orders, expectations)
        balance_ok = cash_drift.value <= self._tolerances.balance_tolerance.value
        position_ok = position_drift <= self._tolerances.position_tolerance.value

        outcome = _classify(
            balance_ok=balance_ok,
            position_ok=position_ok,
            open_order_ok=open_order_ok,
            drift_present=cash_drift.value > 0 or position_drift > 0,
        )
        snapshot = VerificationSnapshot(
            outcome=outcome,
            balance_ok=balance_ok,
            position_ok=position_ok,
            open_order_ok=open_order_ok,
            verified_at_epoch_s=now_epoch_s,
            exchange_verified_available_cash=observed_cash,
            cash_drift=cash_drift,
            semantics_fully_known=semantics_known,
        )
        # Record the audit event before any alerting: alert dispatch performs
        # per-ticker ``get_market`` lookups, and recording the cycle's outcome
        # first ensures a raise there can never lose the ledgered verification
        # event (nor, at the process level, the HALT this snapshot drives).
        self._record(snapshot)
        self._dispatch_alerts(positions, open_orders, outcome)
        return snapshot

    def _cash_drift(
        self, observed_cash: MoneyMicros, expectations: LedgerExpectations
    ) -> MoneyMicros:
        """Return the absolute available-cash drift for this cycle.

        Args:
            observed_cash: The venue-reported available cash, in micros.
            expectations: The ledger expectations supplying the expected cash.

        Returns:
            The absolute cash drift, in micros.
        """
        drift = abs(observed_cash.value - expectations.expected_available_cash.value)
        return MoneyMicros(drift)

    def _position_drift(
        self,
        positions: tuple[Position, ...],
        expectations: LedgerExpectations,
    ) -> int:
        """Return the largest absolute per-ticker position drift, in centis.

        A ticker present on only one side is diffed against an implicit zero on
        the other, so an unexpected held position (or a missing expected one) is
        surfaced as its full size.

        Args:
            positions: The venue-reported open positions.
            expectations: The ledger expectations supplying expected positions.

        Returns:
            The maximum absolute position drift across every observed or
            expected ticker, in contract-centis; ``0`` when both sides are flat.
        """
        observed = {position.ticker: position.quantity.value for position in positions}
        expected = {
            ticker: quantity.value
            for ticker, quantity in expectations.expected_positions.items()
        }
        diffs = [
            abs(observed.get(ticker, 0) - expected.get(ticker, 0))
            for ticker in set(observed) | set(expected)
        ]
        return max(diffs, default=0)

    def _open_orders_match(
        self,
        open_orders: tuple[OpenOrder, ...],
        expectations: LedgerExpectations,
    ) -> bool:
        """Return whether the observed open-order ids match the expected set.

        Args:
            open_orders: The venue-reported resting orders.
            expectations: The ledger expectations supplying the expected id set.

        Returns:
            ``True`` iff the observed and expected id sets are equal.
        """
        observed_ids = frozenset(order.id for order in open_orders)
        return observed_ids == expectations.expected_open_order_ids

    def _dispatch_alerts(
        self,
        positions: tuple[Position, ...],
        open_orders: tuple[OpenOrder, ...],
        outcome: VerificationOutcome,
    ) -> None:
        """Fire jurisdiction-unknown and (on a breach) mismatch alerts.

        Args:
            positions: The venue-reported open positions.
            open_orders: The venue-reported resting orders.
            outcome: The graded outcome of the cycle.
        """
        self._alert_unknown_jurisdictions(positions, open_orders)
        if outcome is VerificationOutcome.BREACH:
            self._dispatcher.dispatch(
                AlertType.RECONCILIATION_MISMATCH, _MISMATCH_MESSAGE
            )

    def _alert_unknown_jurisdictions(
        self,
        positions: tuple[Position, ...],
        open_orders: tuple[OpenOrder, ...],
    ) -> None:
        """Fire one warning alert per held market of unknown jurisdiction.

        Args:
            positions: The venue-reported open positions.
            open_orders: The venue-reported resting orders.
        """
        held = {position.ticker for position in positions}
        held |= {order.ticker for order in open_orders}
        for ticker in sorted(held):
            market = self._connector.get_market(ticker)
            if market.jurisdiction_status == _UNKNOWN_JURISDICTION:
                self._dispatcher.dispatch(
                    AlertType.JURISDICTION_UNKNOWN,
                    f"held market {ticker} has unknown jurisdiction status",
                )

    def _record(self, snapshot: VerificationSnapshot) -> None:
        """Record exactly one bare event describing the cycle's snapshot.

        Args:
            snapshot: The snapshot to record.
        """
        self._ledger_writer.record(
            Event(
                event_type=_EVENT_TYPE_BY_OUTCOME[snapshot.outcome],
                component=_COMPONENT,
                payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
                payload=_payload(snapshot),
            )
        )


def _payload(snapshot: VerificationSnapshot) -> dict[str, object]:
    """Project a snapshot into a JSON-safe, float-free event payload.

    Every leaf is an ``int``, ``str``, or ``bool`` (SPEC S6.1): the money terms
    are emitted as their scaled-integer ``.value`` and the outcome as its enum
    member name.

    Args:
        snapshot: The snapshot to project.

    Returns:
        The event payload mapping.
    """
    return {
        "outcome": snapshot.outcome.name,
        "balance_ok": snapshot.balance_ok,
        "position_ok": snapshot.position_ok,
        "open_order_ok": snapshot.open_order_ok,
        "verified_at_epoch_s": snapshot.verified_at_epoch_s,
        "exchange_verified_available_cash": (
            snapshot.exchange_verified_available_cash.value
        ),
        "cash_drift": snapshot.cash_drift.value,
        "semantics_fully_known": snapshot.semantics_fully_known,
    }
