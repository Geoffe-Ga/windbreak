"""Human-acknowledgement orchestration for the Risk Kernel (SPEC S10.3, #57).

The Risk Kernel already has the ``human_ack_satisfied`` pre-trade check
(:mod:`windbreak.riskkernel.checks`) and the pending-acknowledgement queue
(:class:`windbreak.riskkernel.human_ack.HumanAckQueue`), but nothing *composes*
them: nothing turns an over-threshold veto into a HELD state that later
resubmits once an operator grants it, and nothing watches a filesystem drop-box
for that grant. This module supplies the missing coordinator:

    * :class:`AckGatedApprovalPipeline` -- wraps an existing
      :class:`~windbreak.riskkernel.reservations.ApprovalPipeline` /
      :class:`~windbreak.riskkernel.human_ack.HumanAckQueue` pair. Its
      :meth:`~AckGatedApprovalPipeline.submit` stamps the queue's currently
      granted intent ids onto the context (mirroring
      ``ApprovalPipeline._effective_context``'s ledger-stamping precedent),
      runs the wrapped pipeline, and turns a veto whose *sole* reason is
      ``human_ack_satisfied`` into a HELD outcome that opens a pending
      acknowledgement -- while a veto carrying any *other* reason stays a plain,
      fail-closed veto (an operator is never asked to bless an intent that is
      independently broken).
    * :class:`AckFileWatcher` -- a presence-driven, always-consume drop-box
      watcher mirroring :class:`windbreak.riskkernel.kill.KillFileWatcher`: each
      file under ``<state_dir>/acks/`` names a pending approval to grant, and is
      removed whether the grant succeeded, the id was never issued, or it had
      already lapsed (fail-closed, never raising).

Every epoch second is an ``int`` (SPEC S6.1); :meth:`submit` carries no clock of
its own, so ``request_ack``'s ``now`` is ``context.now_epoch_s`` -- the same
instant the check pipeline reads, which every expiry-boundary test relies on.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from windbreak.riskkernel.checks import HUMAN_ACK_REQUIRED_REASON, _order_cost
from windbreak.riskkernel.human_ack import AckLapsedError, UnknownApprovalError

if TYPE_CHECKING:
    from pathlib import Path

    from windbreak.riskkernel.checks import Decision, OrderIntent
    from windbreak.riskkernel.context import EvaluationContext
    from windbreak.riskkernel.human_ack import HumanAckQueue, PendingHumanAck
    from windbreak.riskkernel.reservations import ApprovalPipeline
    from windbreak.tokens.verify import SignedApprovalToken

_LOGGER = logging.getLogger("windbreak.riskkernel.ack_flow")

#: The state-dir subdirectory the drop-box watcher scans and the CLI ``ack``
#: verb writes into. Public so ``windbreak ack`` reuses the exact name rather
#: than duplicating the literal, mirroring ``kill.KILL_FILENAME``.
ACKS_DIRNAME = "acks"


@dataclass(frozen=True, slots=True)
class AckGatedOutcome:
    """The result of an ack-gated submission.

    Attributes:
        token: The signed approval token when the intent was approved, else
            ``None`` (a HELD or vetoed intent carries no token).
        decision: The wrapped pipeline's check-pipeline verdict.
        held: Whether the intent is HELD awaiting a human acknowledgement --
            ``True`` only when the sole veto reason was ``human_ack_satisfied``.
        pending_ack: The opened :class:`PendingHumanAck` when ``held``, else
            ``None``.
    """

    token: SignedApprovalToken | None
    decision: Decision
    held: bool
    pending_ack: PendingHumanAck | None


class AckGatedApprovalPipeline:
    """Composes an approval pipeline with a human-acknowledgement queue (#57)."""

    def __init__(self, pipeline: ApprovalPipeline, ack_queue: HumanAckQueue) -> None:
        """Wire the coordinator to an existing pipeline and ack queue.

        Args:
            pipeline: The already-built approval pipeline to run each submission
                through (composition, not reimplementation).
            ack_queue: The pending-acknowledgement queue whose grants gate a
                resubmission and whose ``request_ack`` opens a HELD intent's
                pending acknowledgement.
        """
        self._pipeline = pipeline
        self._ack_queue = ack_queue

    def submit(
        self, intent: OrderIntent, context: EvaluationContext
    ) -> AckGatedOutcome:
        """Run ``intent`` through the pipeline, holding an ack-only veto.

        The queue's currently granted intent ids are stamped onto a copy of
        ``context`` (via :func:`dataclasses.replace`, never mutating the
        caller's context) so a previously granted intent now clears the
        ``human_ack_satisfied`` check. The wrapped pipeline then runs, and:

            * no veto -> the issued token, ``held=False``;
            * a veto whose *sole* reason is ``human_ack_satisfied`` -> a pending
              acknowledgement is opened (at ``context.now_epoch_s``) and the
              intent is HELD (``held=True``, no token); resubmitting the same
              intent while that acknowledgement is still pending is idempotent
              -- it re-returns the existing HELD outcome rather than opening a
              second request;
            * any other veto (including ``human_ack_satisfied`` *plus* another
              reason) -> a plain, fail-closed veto (``held=False``, no ack
              requested).

        Args:
            intent: The order intent to submit.
            context: The caller-supplied evaluation context; its
                ``now_epoch_s`` is the instant both the check pipeline and any
                opened acknowledgement read.

        Returns:
            The :class:`AckGatedOutcome` describing approval, HELD, or veto.
        """
        acknowledged = self._ack_queue.acknowledged_intent_ids(context.now_epoch_s)
        effective = dataclasses.replace(context, acknowledged_intent_ids=acknowledged)
        outcome = self._pipeline.approve(intent, effective)
        decision = outcome.decision
        if not decision.vetoed:
            return AckGatedOutcome(
                token=outcome.token, decision=decision, held=False, pending_ack=None
            )
        if decision.reasons == (HUMAN_ACK_REQUIRED_REASON,):
            pending = self._request_or_existing_hold(intent, effective)
            return AckGatedOutcome(
                token=None, decision=decision, held=True, pending_ack=pending
            )
        return AckGatedOutcome(
            token=None, decision=decision, held=False, pending_ack=None
        )

    def _request_or_existing_hold(
        self, intent: OrderIntent, effective: EvaluationContext
    ) -> PendingHumanAck:
        """Open a pending acknowledgement for ``intent``, or reuse its open one.

        An intent that already has a still-pending acknowledgement (a resubmit
        before anyone has granted or the ttl has lapsed) reuses that request, so
        a resubmit-while-pending is idempotent; only an intent with no open
        request opens a fresh one. Checking the queue first means
        :meth:`HumanAckQueue.request_ack`'s duplicate guard is never tripped.

        Args:
            intent: The over-threshold intent being held.
            effective: The ledger-stamped context supplying price/size/fees for
                the worst-case cost and the current epoch second.

        Returns:
            The pending :class:`PendingHumanAck` for ``intent`` -- the one
            already open for it, or a freshly opened one.
        """
        now = effective.now_epoch_s
        for pending in self._ack_queue.pending_acks(now):
            if pending.intent_id == intent.intent_id:
                return pending
        return self._ack_queue.request_ack(
            intent.intent_id, _order_cost(intent, effective), now
        )

    def expire_due(self, now: int) -> None:
        """Lapse every due, still-pending acknowledgement.

        Delegates to :meth:`HumanAckQueue.expire_due`, whose inclusive ttl
        boundary and reservation release it inherits unchanged.

        Args:
            now: The current epoch second.
        """
        self._ack_queue.expire_due(now)


class AckFileWatcher:
    """Grants pending acknowledgements named by drop-box files (issue #57).

    Mirrors :class:`windbreak.riskkernel.kill.KillFileWatcher`'s bounded,
    always-consume shape, but over a directory of files rather than two fixed
    names: one :meth:`poll_once` per beat grants every pending approval named by
    a file under ``<state_dir>/acks/`` and removes the file afterward --
    whether the grant succeeded, the id was never issued, or it had already
    lapsed -- so a malformed or stale drop never wedges the beat.
    """

    def __init__(self, queue: HumanAckQueue, state_dir: Path) -> None:
        """Wire the watcher to a queue and the directory it polls.

        Args:
            queue: The queue whose pending acknowledgements a file grants.
            state_dir: The directory whose ``acks/`` subdirectory is scanned.
        """
        self._queue = queue
        self._state_dir = state_dir

    def poll_once(self, now: int) -> None:
        """Grant and consume every drop-box file once (bounded, no loop/sleep).

        A missing ``acks/`` directory is a no-op, so the watcher runs cleanly
        before the first ``windbreak ack`` has ever written one.

        Args:
            now: The current beat's epoch second, forwarded to each grant so a
                lapsed acknowledgement is correctly rejected at its ttl.
        """
        acks_dir = self._state_dir.joinpath(ACKS_DIRNAME)
        if not acks_dir.is_dir():
            return
        for entry in sorted(acks_dir.iterdir()):
            if entry.is_file():
                self._grant_and_consume(entry, now)

    def _grant_and_consume(self, path: Path, now: int) -> None:
        """Grant the approval named by ``path`` and always remove the file.

        The file's name is the approval id. A grant for an id that was never
        issued (:class:`UnknownApprovalError`) or has already lapsed
        (:class:`AckLapsedError`) grants nothing and is logged, never raised;
        the file is deleted in the ``finally`` regardless, mirroring
        ``KillFileWatcher``'s always-consume robustness.

        Args:
            path: The drop-box file whose name is the approval id to grant.
            now: The current epoch second, forwarded to the grant.
        """
        try:
            self._queue.grant(approval_id=path.name, now=now)
        except (UnknownApprovalError, AckLapsedError):
            _LOGGER.debug("ack drop-box: no live approval for %r", path.name)
        finally:
            path.unlink(missing_ok=True)
