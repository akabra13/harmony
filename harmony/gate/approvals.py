"""Approval requests: asking a human, and knowing what to do when they do not answer.

An approval is not a flag on a run. It is a durable record with a lifecycle, bound
to a specific plan, with a deadline and a rule for what happens when the deadline
passes. That is what the brief's rule requires:

    *"If an approval request is unanswered at end of day and the approver's calendar
    shows them out the next day, it routes to their designated backup."*

Three things follow from taking that seriously:

**The deadline is scheduled work, not a loop.** Creating a request enqueues an
escalation task for end of day. If the process dies, the task is still there.

**Availability is asked of an oracle, not read from a table.** The kernel does not
know what a calendar record looks like, so it asks
:class:`AvailabilityOracle` — "is this person available tomorrow?" — and the company
answers from whatever system holds that. Microsoft Graph would answer the same
question differently and nothing here would change.

**The approver can change; the plan cannot.** Escalation reassigns who is asked.
The ``proposal_digest`` stays fixed, so whoever ends up answering is answering the
same question the first person was asked.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from harmony.audit.models import EventType
from harmony.identity.grant import ExecutionGrant
from harmony.identity.session import Session
from harmony.kernel.errors import ApprovalMismatch, HarmonyError
from harmony.kernel.ids import short_digest
from harmony.kernel.store import Store
from harmony.plan.models import Proposal
from harmony.schedule.models import ScheduledTask
from harmony.schedule.queue import TaskQueue

ESCALATION_TASK_KIND = "approval.escalate"


class ApprovalState(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    """Reassigned to a different approver. The original request is closed rather
    than left open, so "who is being asked right now?" has one answer."""


class ApprovalRequest(BaseModel):
    """One request for a human decision."""

    approval_id: str = ""
    """Derived in :meth:`ApprovalService.request` from the run, the plan and the
    escalation round, so a reproducible run produces reproducible approval ids.
    See ``Orchestrator._run_id_for`` for why that matters."""

    run_id: str
    proposal_id: str
    proposal_digest: str
    requested_of: str
    originally_for: str
    reason: str
    state: ApprovalState = ApprovalState.PENDING
    created_at: _dt.datetime | None = None
    expires_at: _dt.datetime | None = None
    decided_at: _dt.datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    escalation_count: int = 0

    @property
    def is_open(self) -> bool:
        return self.state is ApprovalState.PENDING


@runtime_checkable
class AvailabilityOracle(Protocol):
    """Answers whether someone can be expected to respond on a given day.

    The kernel's only window into a calendar. Keeping it this narrow is what stops
    the approval logic from acquiring a dependency on one company's event schema.
    """

    def is_available(self, session: Session, user_id: str, day: _dt.date) -> bool: ...


class AlwaysAvailable:
    """Fallback oracle for deployments with no calendar. Never escalates for
    absence — a defensible default, since escalating because we could not check
    would route approvals away from the right person on the strength of a missing
    integration."""

    def is_available(self, session: Session, user_id: str, day: _dt.date) -> bool:
        return True


class ApprovalService:
    """Creates, routes, escalates and resolves approval requests."""

    def __init__(
        self,
        *,
        store: Store,
        tasks: TaskQueue,
        directory,
        availability: AvailabilityOracle | None = None,
    ) -> None:
        self._store = store
        self._tasks = tasks
        self._directory = directory
        self._availability = availability or AlwaysAvailable()

    # --- creating --------------------------------------------------------------

    def request(
        self,
        session: Session,
        *,
        proposal: Proposal,
        approver_id: str,
        reason: str,
    ) -> ApprovalRequest:
        """Ask a human, and schedule the check for what happens if they do not answer."""
        now = session.clock.now()
        approval = ApprovalRequest(
            approval_id=f"APR-{short_digest(session.run_id, proposal.digest(), 0, length=4)}",
            run_id=session.run_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest(),
            requested_of=approver_id,
            originally_for=approver_id,
            reason=reason,
            created_at=now,
            expires_at=session.clock.end_of_day(),
        )
        self._insert(approval)

        approver = self._directory.try_get(approver_id)
        session.audit.emit(
            EventType.APPROVAL_REQUESTED,
            f"asked {approver.label if approver else approver_id} to approve: {proposal.summary}",
            approval_id=approval.approval_id,
            approver_id=approver_id,
            reason=reason,
            proposal_id=proposal.proposal_id,
            proposal_digest=approval.proposal_digest[:12],
            expires_at=approval.expires_at.isoformat() if approval.expires_at else None,
        )
        self._schedule_escalation(session, approval)
        return approval

    def _schedule_escalation(self, session: Session, approval: ApprovalRequest) -> None:
        """Enqueue the end-of-day check.

        The dedupe key includes the escalation count so each successive check gets
        its own task, while re-running the same round is a no-op.
        """
        task = ScheduledTask(
            kind=ESCALATION_TASK_KIND,
            payload={"approval_id": approval.approval_id},
            fire_at=approval.expires_at or session.clock.end_of_day(),
            dedupe_key=f"escalate:{approval.approval_id}:{approval.escalation_count}",
            run_id=approval.run_id,
        )
        created = self._tasks.enqueue(task, now=session.clock.now())
        if created:
            session.audit.emit(
                EventType.SCHEDULE_TASK_CREATED,
                f"will re-check approval {approval.approval_id} at end of day",
                task_id=created.task_id,
                kind=created.kind,
                fire_at=created.fire_at.isoformat(),
                approval_id=approval.approval_id,
            )

    # --- deciding --------------------------------------------------------------

    def decide(
        self,
        session: Session,
        *,
        approval_id: str,
        approve: bool,
        decided_by: str,
        note: str = "",
    ) -> ApprovalRequest:
        """Record a human's decision."""
        approval = self.get(approval_id)
        if approval is None:
            raise HarmonyError(f"no approval request '{approval_id}'")
        if not approval.is_open:
            raise HarmonyError(
                f"approval {approval_id} is already {approval.state.value}",
                state=approval.state.value,
            )
        if decided_by != approval.requested_of:
            raise HarmonyError(
                f"approval {approval_id} was asked of {approval.requested_of}, "
                f"not {decided_by}",
                requested_of=approval.requested_of,
                attempted_by=decided_by,
            )

        now = session.clock.now()
        state = ApprovalState.GRANTED if approve else ApprovalState.REJECTED
        self._set_decision(approval_id, state, decided_by, note, now)
        approval.state = state
        approval.decided_at = now
        approval.decided_by = decided_by
        approval.decision_note = note

        decider = self._directory.try_get(decided_by)
        session.audit.emit(
            EventType.APPROVAL_GRANTED if approve else EventType.APPROVAL_REJECTED,
            f"{decider.label if decider else decided_by} "
            f"{'approved' if approve else 'rejected'} {approval_id}"
            + (f": {note}" if note else ""),
            approval_id=approval_id,
            decided_by=decided_by,
            note=note,
            proposal_digest=approval.proposal_digest[:12],
        )
        return approval

    def grant_for(
        self, approval: ApprovalRequest, *, proposal: Proposal, allowed_tools: frozenset[str]
    ) -> ExecutionGrant:
        """Mint the execution grant a granted approval authorises.

        The digest is re-checked here. If the plan changed between being approved
        and being executed, the human agreed to something else, and this is where
        that is caught.
        """
        if approval.state is not ApprovalState.GRANTED:
            raise HarmonyError(
                f"approval {approval.approval_id} is {approval.state.value}, not granted"
            )
        if approval.proposal_digest != proposal.digest():
            raise ApprovalMismatch(
                "the plan changed after it was approved; the approval does not cover it",
                approval_id=approval.approval_id,
                approved_digest=approval.proposal_digest[:12],
                current_digest=proposal.digest()[:12],
            )
        return ExecutionGrant(
            proposal_digest=approval.proposal_digest,
            granted_by=approval.decided_by or approval.requested_of,
            granted_at=approval.decided_at or _dt.datetime.min,
            allowed_tools=allowed_tools,
            approval_id=approval.approval_id,
            reason=approval.reason,
        )

    # --- escalation ------------------------------------------------------------

    def escalate_if_unanswered(self, session: Session, approval_id: str) -> ApprovalRequest | None:
        """The end-of-day rule.

        Unanswered, and the approver is out tomorrow → route to their designated
        backup. Unanswered but the approver is in tomorrow → leave it with them and
        check again at the next end of day. Answered → nothing to do.
        """
        approval = self.get(approval_id)
        if approval is None or not approval.is_open:
            return None

        # The day to check is the one *after the deadline*, not the one after now.
        #
        # The deadline is end of day, so this task necessarily fires after midnight,
        # by which point "tomorrow" is already a day too late — and the approver
        # being out on the very day the request went stale is exactly the case the
        # rule exists for. Deriving it from the deadline also makes the answer
        # stable no matter how long the worker took to get here.
        deadline = approval.expires_at or session.clock.now()
        tomorrow = deadline.date() + _dt.timedelta(days=1)
        approver = self._directory.try_get(approval.requested_of)
        available = self._availability.is_available(session, approval.requested_of, tomorrow)

        if available:
            approval.escalation_count += 1
            approval.expires_at = session.clock.end_of_day(tomorrow)
            self._set_expiry(approval)
            session.audit.emit(
                EventType.APPROVAL_EXPIRED,
                f"{approval_id} still unanswered; {approver.label if approver else approval.requested_of} "
                f"is available on {tomorrow.isoformat()}, so it stays with them",
                approval_id=approval_id,
                approver_id=approval.requested_of,
                checked_day=tomorrow.isoformat(),
                available=True,
            )
            self._schedule_escalation(session, approval)
            return approval

        backup_id = approver.backup_approver_id if approver else None
        if not backup_id:
            session.audit.emit(
                EventType.APPROVAL_EXPIRED,
                f"{approval_id} unanswered and {approval.requested_of} is out on "
                f"{tomorrow.isoformat()}, but no backup approver is designated",
                approval_id=approval_id,
                approver_id=approval.requested_of,
                available=False,
                backup_approver_id=None,
            )
            return approval

        return self._reassign(session, approval, backup_id, tomorrow)

    def _reassign(
        self,
        session: Session,
        approval: ApprovalRequest,
        backup_id: str,
        checked_day: _dt.date,
    ) -> ApprovalRequest:
        """Close the old request and open an identical one for the backup."""
        now = session.clock.now()
        self._set_decision(
            approval.approval_id,
            ApprovalState.SUPERSEDED,
            decided_by=None,
            note=f"routed to backup approver {backup_id}",
            now=now,
        )

        successor = ApprovalRequest(
            approval_id=(
                f"APR-{short_digest(approval.run_id, approval.proposal_digest, approval.escalation_count + 1, length=4)}"
            ),
            run_id=approval.run_id,
            proposal_id=approval.proposal_id,
            proposal_digest=approval.proposal_digest,
            requested_of=backup_id,
            originally_for=approval.originally_for,
            reason=approval.reason,
            created_at=now,
            expires_at=session.clock.end_of_day(checked_day),
            escalation_count=approval.escalation_count + 1,
        )
        self._insert(successor)

        original = self._directory.try_get(approval.requested_of)
        backup = self._directory.try_get(backup_id)
        session.audit.emit(
            EventType.APPROVAL_ESCALATED,
            f"{approval.approval_id} was unanswered and "
            f"{original.label if original else approval.requested_of} is out on "
            f"{checked_day.isoformat()}; routed to backup "
            f"{backup.label if backup else backup_id} as {successor.approval_id}",
            approval_id=approval.approval_id,
            successor_id=successor.approval_id,
            from_approver=approval.requested_of,
            to_approver=backup_id,
            checked_day=checked_day.isoformat(),
            available=False,
            proposal_digest=approval.proposal_digest[:12],
        )
        self._schedule_escalation(session, successor)
        return successor

    # --- queries ---------------------------------------------------------------

    def get(self, approval_id: str) -> ApprovalRequest | None:
        row = self._store.query_one(
            "SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)
        )
        return self._row_to_approval(row) if row else None

    def open_for(self, user_id: str) -> list[ApprovalRequest]:
        rows = self._store.query(
            "SELECT * FROM approval_requests WHERE requested_of = ? AND state = ? "
            "ORDER BY created_at",
            (user_id, ApprovalState.PENDING.value),
        )
        return [self._row_to_approval(r) for r in rows]

    def all_open(self) -> list[ApprovalRequest]:
        rows = self._store.query(
            "SELECT * FROM approval_requests WHERE state = ? ORDER BY created_at",
            (ApprovalState.PENDING.value,),
        )
        return [self._row_to_approval(r) for r in rows]

    def for_run(self, run_id: str) -> list[ApprovalRequest]:
        rows = self._store.query(
            "SELECT * FROM approval_requests WHERE run_id = ? ORDER BY created_at", (run_id,)
        )
        return [self._row_to_approval(r) for r in rows]

    # --- storage ---------------------------------------------------------------

    def _insert(self, approval: ApprovalRequest) -> None:
        self._store.execute(
            """
            INSERT INTO approval_requests (
                approval_id, run_id, proposal_id, proposal_digest, requested_of,
                originally_for, reason, state, created_at, expires_at, escalation_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval.approval_id,
                approval.run_id,
                approval.proposal_id,
                approval.proposal_digest,
                approval.requested_of,
                approval.originally_for,
                approval.reason,
                approval.state.value,
                approval.created_at.isoformat() if approval.created_at else None,
                approval.expires_at.isoformat() if approval.expires_at else None,
                approval.escalation_count,
            ),
        )

    def _set_decision(
        self,
        approval_id: str,
        state: ApprovalState,
        decided_by: str | None,
        note: str | None,
        now: _dt.datetime,
    ) -> None:
        self._store.execute(
            """
            UPDATE approval_requests
            SET state = ?, decided_at = ?, decided_by = ?, decision_note = ?
            WHERE approval_id = ?
            """,
            (state.value, now.isoformat(), decided_by, note, approval_id),
        )

    def _set_expiry(self, approval: ApprovalRequest) -> None:
        self._store.execute(
            "UPDATE approval_requests SET expires_at = ?, escalation_count = ? "
            "WHERE approval_id = ?",
            (
                approval.expires_at.isoformat() if approval.expires_at else None,
                approval.escalation_count,
                approval.approval_id,
            ),
        )

    @staticmethod
    def _row_to_approval(row: Any) -> ApprovalRequest:
        return ApprovalRequest(
            approval_id=row["approval_id"],
            run_id=row["run_id"],
            proposal_id=row["proposal_id"],
            proposal_digest=row["proposal_digest"],
            requested_of=row["requested_of"],
            originally_for=row["originally_for"],
            reason=row["reason"],
            state=ApprovalState(row["state"]),
            created_at=_dt.datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else None,
            expires_at=_dt.datetime.fromisoformat(row["expires_at"])
            if row["expires_at"]
            else None,
            decided_at=_dt.datetime.fromisoformat(row["decided_at"])
            if row["decided_at"]
            else None,
            decided_by=row["decided_by"],
            decision_note=row["decision_note"],
            escalation_count=row["escalation_count"],
        )
