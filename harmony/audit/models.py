"""The audit vocabulary.

Requirement 7 of the brief: *"from the audit log alone, someone should be able to
reconstruct what the agent saw, what it concluded, what it was allowed to do, who
approved what, and what actually happened in each system."*

That sentence is a schema requirement, and :class:`EventType` is the schema. The
five clauses map onto five families of event:

===========================  ====================================================
"what it saw"                CONTEXT_* — including what was *withheld*
"what it concluded"          LLM_*, PLAN_*
"what it was allowed to do"  GATE_*, SCOPE_DENIED
"who approved what"          APPROVAL_*
"what actually happened"     TOOL_*, WORKFLOW_*, SCHEDULE_*
===========================  ====================================================

Adding an event type is cheap; removing one breaks a reader. Treat these names as
published API.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    # --- run lifecycle ---------------------------------------------------------
    RUN_STARTED = "run.started"
    RUN_STATE_CHANGED = "run.state_changed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"

    # --- what the agent saw ----------------------------------------------------
    DETECTOR_RAN = "detect.detector_ran"
    ATTENTION_ITEM_RAISED = "detect.item_raised"
    ATTENTION_ITEM_SUPPRESSED = "detect.item_suppressed"
    ATTENTION_ITEM_SUPERSEDED = "detect.item_superseded"
    CONTEXT_REQUESTED = "context.requested"
    CONTEXT_SLICE_FETCHED = "context.slice_fetched"
    CONTEXT_PROVIDER_SKIPPED = "context.provider_skipped"
    CONTEXT_REDACTED = "context.redacted"

    # --- what the agent concluded ----------------------------------------------
    LLM_CALLED = "llm.called"
    LLM_OUTPUT_REJECTED = "llm.output_rejected"
    PLAN_PROPOSED = "plan.proposed"
    PLAN_REJECTED = "plan.rejected"

    # --- what the agent was allowed to do --------------------------------------
    GATE_EVALUATED = "gate.evaluated"
    GATE_RULE_EVALUATED = "gate.rule_evaluated"
    SCOPE_DENIED = "gate.scope_denied"

    # --- who approved what -----------------------------------------------------
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_ESCALATED = "approval.escalated"
    APPROVAL_EXPIRED = "approval.expired"

    # --- what actually happened ------------------------------------------------
    TOOL_INVOKED = "tool.invoked"
    TOOL_SUCCEEDED = "tool.succeeded"
    TOOL_FAILED = "tool.failed"
    TOOL_REPLAYED = "tool.replayed"

    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_STEP_STARTED = "workflow.step_started"
    WORKFLOW_STEP_COMPLETED = "workflow.step_completed"
    WORKFLOW_STEP_FAILED = "workflow.step_failed"
    WORKFLOW_RESUMED = "workflow.resumed"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_COMPENSATING = "workflow.compensating"
    WORKFLOW_COMPENSATION_STEP = "workflow.compensation_step"
    WORKFLOW_COMPENSATED = "workflow.compensated"
    WORKFLOW_COMPENSATION_FAILED = "workflow.compensation_failed"

    SCHEDULE_TASK_CREATED = "schedule.task_created"
    SCHEDULE_TASK_FIRED = "schedule.task_fired"
    SCHEDULE_TASK_FAILED = "schedule.task_failed"
    CLOCK_ADVANCED = "schedule.clock_advanced"

    # --- memory ----------------------------------------------------------------
    MEMORY_RECALLED = "memory.recalled"
    MEMORY_PROMOTED = "memory.promoted"
    MEMORY_DEMOTED = "memory.demoted"


class AuditEvent(BaseModel):
    """One immutable line in the ledger.

    ``ts_clock`` is simulated time — the time the business would recognise.
    ``ts_wall`` is real time — when the process actually did it. Keeping both is
    what lets a reader distinguish "the agent waited five days" from "the demo ran
    in four seconds".
    """

    seq: int | None = None
    event_id: str
    run_id: str | None
    ts_clock: _dt.datetime
    ts_wall: _dt.datetime
    actor_kind: str
    actor_id: str
    event_type: EventType
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = ""
    entry_hash: str = ""
