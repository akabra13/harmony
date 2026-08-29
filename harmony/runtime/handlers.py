"""Task handlers: how deferred work re-enters the loop.

Two handlers, both domain-free, and between them they are the whole of "the agent
does something later".

``detector.run``
    Runs one detector with a payload naming what to re-check, and opens a run if it
    finds something. This is what the Tuesday arrival check *is*. There is no
    separate follow-up machinery — a follow-up is a targeted detection, and it goes
    through dedupe, planning, gating and approval exactly like a scheduled sweep
    would. That uniformity is why the second pass of Scenario A needs no new code.

``approval.escalate``
    The end-of-day rule. Delegates to :class:`ApprovalService`, which owns the
    decision; this handler exists only to connect the queue to it.
"""

from __future__ import annotations

from harmony.audit.models import EventType
from harmony.runtime.run import TriggerKind
from harmony.schedule.worker import TaskContext, task_handler
from harmony.schedule.tools import FOLLOW_UP_TASK_KIND
from harmony.gate.approvals import ESCALATION_TASK_KIND


@task_handler(FOLLOW_UP_TASK_KIND)
def run_detector_task(ctx: TaskContext) -> None:
    """Re-check a specific condition, and re-enter the loop if it still holds."""
    from harmony.runtime.orchestrator import Orchestrator

    payload = dict(ctx.payload)
    detector_id = payload.pop("detector")
    principal_id = payload.pop("principal_id")
    parent_run_id = payload.pop("parent_run_id", None)
    reason = payload.pop("reason", "")

    audit = ctx.harness.system_audit(run_id=ctx.task.run_id)
    audit.emit(
        EventType.DETECTOR_RAN,
        f"scheduled follow-up: re-running '{detector_id}' — {reason}",
        detector=detector_id,
        principal_id=principal_id,
        parent_run_id=parent_run_id,
        payload=payload,
    )

    runs = Orchestrator(ctx.harness).detect_and_run(
        principal_id,
        detector_ids=[detector_id],
        payload=payload,
        trigger=TriggerKind.FOLLOW_UP,
        parent_run_id=parent_run_id,
    )
    audit.emit(
        EventType.SCHEDULE_TASK_FIRED,
        f"follow-up opened {len(runs)} run(s)"
        if runs
        else "follow-up found nothing outstanding",
        detector=detector_id,
        opened_runs=[r.run_id for r in runs],
    )


@task_handler(ESCALATION_TASK_KIND)
def escalate_approval_task(ctx: TaskContext) -> None:
    """Re-examine an unanswered approval at end of day."""
    approval_id = ctx.payload["approval_id"]
    approval = ctx.harness.approvals.get(approval_id)
    if approval is None:
        return

    # The availability check runs as a narrow system principal, not as the
    # approver: nobody is present to act, and reading a colleague's free/busy to
    # route an approval is not something the requester should need rights for.
    session = ctx.harness.system_session(
        run_id=approval.run_id,
        purpose=f"end-of-day escalation check for {approval_id}",
    )
    ctx.harness.approvals.escalate_if_unanswered(session, approval_id)
