"""Shared builders for `tests/scheduler/*` (issue #48, RED).

`hedgekit.scheduler.loop` does not exist yet, so importing `PaperTickDeps`,
`build_paper_deps`, `run_single_tick`, `ApprovalSeam`, or `KernelApproval`
below fails collection with `ModuleNotFoundError: No module named
'hedgekit.scheduler'` -- the expected Gate 1 RED state for issue #48.

This module itself imports only already-shipped machinery
(`hedgekit.riskkernel.*`, `hedgekit.ledger.*`, `hedgekit.numeric`,
`tests.riskkernel.conftest`), so it collects cleanly on its own; the
`ModuleNotFoundError` surfaces only from `test_loop.py`'s own
`from hedgekit.scheduler.loop import ...` line.

Builder-placement choice mirrors `tests/riskkernel/conftest.py` and
`tests/order_gateway/conftest.py`: plain, explicitly-imported functions
rather than pytest fixtures, so they compose cleanly inside
`@pytest.mark.parametrize`-driven tables and inside any Hypothesis-decorated
test that might be added later.
"""

from __future__ import annotations

from hedgekit.riskkernel.modes import Mode, ModeStateMachine
from hedgekit.riskkernel.process import InMemoryKernelLedgerWriter, RiskKernel
from hedgekit.riskkernel.reservations import ApprovalPipeline, ReservationLedger
from hedgekit.riskkernel.signing import SigningKeyHandle
from hedgekit.riskkernel.tokens import TokenIssuer

#: A fixed 32-byte HMAC key shared by every mint/verify pair in this package's
#: tests -- mirrors `tests/order_gateway/conftest.py::KEY_MATERIAL`. SPEC S10.6
#: approval tokens are symmetric, so the exact same bytes both mint (via
#: `TokenIssuer`) and would verify (Gateway side) for the fill-leg scenario.
KEY_MATERIAL = b"s" * 32

#: The single ticker every scheduler-unit-test intent targets: the sole ticker
#: in the shared `tests/fixtures/books/deep_walk` fixture, matching
#: `tests/order_gateway/conftest.py::DEFAULT_MARKET_TICKER`.
DEFAULT_MARKET_TICKER = "MKT-DEEP"

#: The fixed "current instant" (epoch seconds) every builder below agrees on.
DEFAULT_NOW_EPOCH_S = 1_700_000_000

#: A fixed, content-stable config-revision hash stamped on every issued token.
TEST_CONFIG_HASH = "scheduler-test-config-hash"


def build_kernel_approval_components(
    *, key_material: bytes = KEY_MATERIAL, config_hash: str = TEST_CONFIG_HASH
) -> tuple[RiskKernel, ApprovalPipeline, InMemoryKernelLedgerWriter]:
    """Build a real `RiskKernel` + `ApprovalPipeline` pair over one shared ledger.

    Mirrors the exact composition `hedgekit.scheduler.loop.KernelApproval` is
    specified to wire: a `RiskKernel` (for the ledgered `IntentVetoed`/
    `IntentApproved` audit event) and an `ApprovalPipeline` (for the
    reserve-and-issue path), both writing through the same in-memory ledger so
    a test can inspect every event either component recorded, in order.

    Args:
        key_material: The signing key material the issued tokens are minted
            under.
        config_hash: The configuration-revision hash stamped into every
            issued token's claims.

    Returns:
        A `(kernel, pipeline, writer)` triple sharing one ledger writer.
    """
    writer = InMemoryKernelLedgerWriter()
    # The kernel's *own* tracked mode -- not the caller's context -- is what
    # `RiskKernel.evaluate_intent` stamps onto the effective context, so the
    # mode machine is started already in PAPER (matching the always-on PAPER
    # tick this package composes) rather than the default RESEARCH, which
    # would add a spurious `mode_permission_ceiling` veto reason on top of the
    # ones this suite is pinning.
    mode_machine = ModeStateMachine(mode_ceiling=Mode.PAPER, mode=Mode.PAPER)
    kernel = RiskKernel(writer, mode_machine=mode_machine)
    ledger = ReservationLedger(writer)
    issuer = TokenIssuer(SigningKeyHandle(key_material))
    pipeline = ApprovalPipeline(ledger, issuer, config_hash=config_hash)
    return kernel, pipeline, writer
