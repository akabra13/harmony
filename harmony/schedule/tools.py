"""Tools the harness provides for its own scheduling.

Almost every tool belongs to the company — it touches a system of record that
company owns. These two are different: they act on the harness's own queue, they
mean the same thing for every deployment, and so they ship with the kernel.

They are what make "schedule a check for Tuesday to confirm the shipment actually
arrived" an ordinary step in a workflow rather than a special capability the engine
needs to know about. A follow-up is a tool call like any other: scoped, audited,
idempotent, and — usefully — compensable, so a workflow that rolls back does not
leave a check scheduled for a purchase order that no longer exists.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from pydantic import BaseModel, Field

from harmony.identity.session import Session
from harmony.kernel.clock import parse_datetime
from harmony.kernel.ids import short_digest
from harmony.schedule.models import ScheduledTask
from harmony.tools.base import tool

FOLLOW_UP_TASK_KIND = "detector.run"


class CreateFollowUpInput(BaseModel):
    detector: str = Field(description="Id of the detector to run when this fires.")
    fire_at: _dt.date = Field(description="The date on which to run it.")
    reason: str = Field(default="", description="Why this check was scheduled.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for the detector, naming what specifically to re-check.",
    )


class CreateFollowUpOutput(BaseModel):
    task_id: str
    fire_at: _dt.date
    detector: str
    already_scheduled: bool = False


class CancelFollowUpInput(BaseModel):
    task_id: str


class CancelFollowUpOutput(BaseModel):
    task_id: str
    cancelled: bool


@tool(
    "schedule.create_followup",
    description=(
        "Schedule a detector to run on a future date, re-entering the agent loop "
        "if it finds the condition still holds."
    ),
    scopes={"harmony:schedule:create"},
    input=CreateFollowUpInput,
    output=CreateFollowUpOutput,
    writes=True,
    compensation="schedule.cancel_followup",
    system="harmony",
)
def create_followup(session: Session, inp: CreateFollowUpInput) -> CreateFollowUpOutput:
    """Enqueue a durable follow-up.

    The dedupe key is derived from the detector and its payload, so scheduling the
    same check twice — from a retried run, say — leaves one task rather than two
    and reports which happened.
    """
    tasks = session.services.tasks  # type: ignore[union-attr]
    dedupe_key = f"followup:{inp.detector}:{short_digest(inp.payload, inp.fire_at.isoformat())}"

    existing = tasks.by_dedupe_key(dedupe_key)
    if existing is not None:
        return CreateFollowUpOutput(
            task_id=existing.task_id,
            fire_at=inp.fire_at,
            detector=inp.detector,
            already_scheduled=True,
        )

    created = tasks.enqueue(
        ScheduledTask(
            task_id=session.derive_id("TSK", 6),
            kind=FOLLOW_UP_TASK_KIND,
            payload={
                "detector": inp.detector,
                "principal_id": session.principal.id,
                "reason": inp.reason,
                "parent_run_id": session.run_id,
                **inp.payload,
            },
            fire_at=parse_datetime(inp.fire_at).replace(hour=8),
            dedupe_key=dedupe_key,
            run_id=session.run_id,
        ),
        now=session.clock.now(),
    )
    assert created is not None  # the dedupe check above already handled the collision
    return CreateFollowUpOutput(
        task_id=created.task_id, fire_at=inp.fire_at, detector=inp.detector
    )


@tool(
    "schedule.cancel_followup",
    description="Cancel a previously scheduled follow-up.",
    scopes={"harmony:schedule:create"},
    input=CancelFollowUpInput,
    output=CancelFollowUpOutput,
    writes=True,
    system="harmony",
)
def cancel_followup(session: Session, inp: CancelFollowUpInput) -> CancelFollowUpOutput:
    """Compensation for :func:`create_followup`."""
    tasks = session.services.tasks  # type: ignore[union-attr]
    tasks.cancel(inp.task_id)
    task = tasks.get(inp.task_id)
    return CancelFollowUpOutput(
        task_id=inp.task_id, cancelled=task is not None and task.state.value == "cancelled"
    )
