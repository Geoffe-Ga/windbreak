"""The ``ratchet-sweep`` drill (issue #59).

Composes three already-shipped mechanisms into one "ratchet sweep" scenario:

    * :meth:`~windbreak.riskkernel.governance.FloorGovernance.observe_equity`'s
      exact integer-ppm floor ratchet and its ``PROFIT_SWEEP_ADVISORY`` alert.
    * :meth:`~windbreak.net.allowlist.OutboundAllowlist.require`'s fail-closed
      rejection of a withdrawal-shaped URL.
    * :class:`~windbreak.drills.exchanges.HeldPositionsExchange`'s structural
      absence of any fund-movement method.

"Cannot move funds" is audited from two independent angles -- network egress and
exchange surface -- never just one. Every quantity is integer money-micros; the
ratchet delta is verified against the same
:func:`~windbreak.numeric.divide` floor semantics the governance uses, never a
float. The drill adds no new ratchet, egress, or exchange logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from windbreak.alerts import AlertDispatcher, AlertType, LoggingLedgerWriter
from windbreak.drills.exchanges import HeldPositionsExchange
from windbreak.drills.framework import Drill, DrillFailedError, DrillPreconditionError
from windbreak.net.allowlist import EgressDeniedError, OutboundAllowlist
from windbreak.numeric import RoundingDirection, divide
from windbreak.numeric.types import MoneyMicros
from windbreak.riskkernel.governance import FloorGovernance
from windbreak.riskkernel.modes import Mode, ModeStateMachine
from windbreak.riskkernel.process import InMemoryKernelLedgerWriter

if TYPE_CHECKING:
    from collections.abc import Callable

    from windbreak.alerts import AlertSeverity
    from windbreak.drills.context import DrillContext

#: The fresh equity gain observed to drive the ratchet and advisory, in micros.
_EQUITY_GAIN_MICROS = 2_000_000

#: The floor's ratchet share of each fresh gain, in parts per million.
_RATCHET_PPM = 500_000

#: The profit-sweep advisory threshold, in micros; the gain above clears it.
_PROFIT_SWEEP_THRESHOLD_MICROS = 1_000_000

#: The denominator taking a parts-per-million share back to a whole fraction.
_PPM_DENOMINATOR = 1_000_000

#: The lone allowlisted host; the withdrawal URL below targets a different one.
_ALLOWED_HOST = "api.example.com"

#: A withdrawal-shaped URL against a host never on the allowlist.
_WITHDRAWAL_URL = "https://withdraw.example.com/v1/withdraw"

#: The fund-movement method names a held-positions exchange must never expose.
_FUND_MOVEMENT_ATTRS: tuple[str, ...] = ("withdraw", "transfer", "move_funds")


class _RecordingSink:
    """A spy alert sink recording every dispatched alert type."""

    def __init__(self) -> None:
        """Initialize with a channel name and an empty dispatch log."""
        self.name = "drill-ratchet-spy"
        self.calls: list[AlertType] = []

    def send(
        self, alert_type: AlertType, severity: AlertSeverity, message: str
    ) -> None:
        """Record the dispatched alert type, discarding severity/message.

        Args:
            alert_type: The dispatched alert type.
            severity: The alert severity (ignored).
            message: The alert body (ignored).
        """
        del severity, message
        self.calls.append(alert_type)


class RatchetSweepDrill(Drill):
    """Prove the exact ppm ratchet and the two-angle no-withdrawal audit."""

    name: ClassVar[str] = "ratchet-sweep"

    def check_preconditions(self, ctx: object) -> None:
        """Verify the context carries a held-positions exchange to audit.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to inspect.

        Raises:
            DrillPreconditionError: If ``ctx.exchange`` is not a
                :class:`HeldPositionsExchange`.
        """
        context = cast("DrillContext", ctx)
        if not isinstance(context.exchange, HeldPositionsExchange):
            raise DrillPreconditionError(
                "ratchet-sweep requires a HeldPositionsExchange on the context"
            )

    def execute(self, ctx: object) -> dict[str, object]:
        """Ratchet the floor, then audit "cannot move funds" from two angles.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` to run
                against.

        Returns:
            Evidence recording the exact ratchet delta, the advisory, and both
            no-withdrawal audits.

        Raises:
            DrillFailedError: If the ratchet delta is wrong, the advisory did not
                fire, or either fund-movement audit fails.
        """
        context = cast("DrillContext", ctx)
        exchange = cast("HeldPositionsExchange", context.exchange)
        floor_after, advisory_fired = self._run_ratchet(context.clock)
        expected = divide(
            _EQUITY_GAIN_MICROS * _RATCHET_PPM,
            _PPM_DENOMINATOR,
            rounding=RoundingDirection.UNDERSTATE_EQUITY,
        )
        withdrawal_denied = self._withdrawal_denied()
        no_fund_movement = self._exchange_has_no_fund_movement(exchange)
        self._grade(
            floor_after, expected, advisory_fired, withdrawal_denied, no_fund_movement
        )
        return {
            "floor_increment_micros": floor_after,
            "expected_increment_micros": expected,
            "advisory_fired": advisory_fired,
            "withdrawal_denied": withdrawal_denied,
            "exchange_has_no_fund_movement": no_fund_movement,
        }

    def teardown(self, ctx: object) -> None:
        """No teardown: the audit holds no external resources.

        Args:
            ctx: The :class:`~windbreak.drills.context.DrillContext` (unused).
        """
        del ctx

    def _run_ratchet(self, clock: Callable[[], int]) -> tuple[int, bool]:
        """Observe one fresh gain and read the resulting floor and advisory.

        Args:
            clock: The injected epoch-second clock.

        Returns:
            A ``(floor_after_micros, advisory_fired)`` pair.
        """
        sink = _RecordingSink()
        governance = FloorGovernance(
            initial_floor=MoneyMicros(0),
            ratchet_ppm=_RATCHET_PPM,
            profit_sweep_threshold=MoneyMicros(_PROFIT_SWEEP_THRESHOLD_MICROS),
            mode_machine=ModeStateMachine(mode_ceiling=Mode.LIVE, mode=Mode.LIVE),
            dispatcher=AlertDispatcher([sink], ledger_writer=LoggingLedgerWriter()),
            writer=InMemoryKernelLedgerWriter(),
            clock=clock,
        )
        governance.observe_equity(MoneyMicros(_EQUITY_GAIN_MICROS))
        advisory_fired = sink.calls.count(AlertType.PROFIT_SWEEP_ADVISORY) == 1
        return governance.current_floor_micros.value, advisory_fired

    def _withdrawal_denied(self) -> bool:
        """Return whether the allowlist fails closed on a withdrawal URL.

        Returns:
            ``True`` iff :meth:`OutboundAllowlist.require` raised
            :class:`EgressDeniedError` for the off-allowlist withdrawal URL.
        """
        allowlist = OutboundAllowlist(frozenset({_ALLOWED_HOST}))
        try:
            allowlist.require(_WITHDRAWAL_URL)
        except EgressDeniedError:
            return True
        return False

    def _exchange_has_no_fund_movement(self, exchange: HeldPositionsExchange) -> bool:
        """Return whether the exchange exposes no fund-movement method at all.

        Args:
            exchange: The held-positions exchange to audit structurally.

        Returns:
            ``True`` iff none of ``withdraw``/``transfer``/``move_funds`` exists.
        """
        return not any(hasattr(exchange, attr) for attr in _FUND_MOVEMENT_ATTRS)

    def _grade(
        self,
        floor_after: int,
        expected: int,
        advisory_fired: bool,
        withdrawal_denied: bool,
        no_fund_movement: bool,
    ) -> None:
        """Fail the drill unless every audited invariant held.

        Args:
            floor_after: The floor after the ratchet, in micros.
            expected: The independently computed integer ppm increment.
            advisory_fired: Whether exactly one profit-sweep advisory fired.
            withdrawal_denied: Whether the withdrawal URL was denied.
            no_fund_movement: Whether the exchange exposes no fund movement.

        Raises:
            DrillFailedError: If any invariant did not hold.
        """
        if floor_after != expected:
            raise DrillFailedError(
                {"floor_increment_micros": floor_after, "expected": expected}
            )
        if not (advisory_fired and withdrawal_denied and no_fund_movement):
            raise DrillFailedError(
                {
                    "advisory_fired": advisory_fired,
                    "withdrawal_denied": withdrawal_denied,
                    "exchange_has_no_fund_movement": no_fund_movement,
                }
            )
