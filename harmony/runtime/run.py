"""The run: one pass of the agent loop, as a persisted state machine.

Every state transition is written to the database and to the audit log. A run is
therefore always resumable and always explicable: at any instant there is a row
saying where it is, and a ledger saying how it got there.

The states are the loop:

    DETECTED → GATHERING_CONTEXT → PLANNING → GATING
                                                ├→ DENIED         (terminal)
                                                ├→ NO_ACTION      (terminal)
                                                ├→ AWAITING_APPROVAL → APPROVED
                                                │                    → REJECTED
                                                └→ EXECUTING
    EXECUTING → COMPLETED | FAILED | COMPENSATED

``AWAITING_APPROVAL`` is the interesting one: a run can sit there across a restart,
across a change of approver, and across days of simulated time. It is the only
state whose exit is caused by a human rather than by the process, which is what
makes it the state a durable-execution engine would model as a suspended node —
see DESIGN.md.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum

from pydantic import BaseModel, Field

from harmony.audit.models import EventType
from harmony.kernel.ids import new_id
from harmony.kernel.store import Store


class RunState(StrEnum):
    DETECTED = "detected"
    GATHERING_CONTEXT = "gathering_context"
    PLANNING = "planning"
    GATING = "gating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    DENIED = "denied"
    NO_ACTION = "no_action"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATED = "compensated"

    @property
    def is_terminal(self) -> bool:
        return self in {
            RunState.REJECTED,
            RunState.DENIED,
            RunState.NO_ACTION,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.COMPENSATED,
        }


class TriggerKind(StrEnum):
    SCHEDULE = "schedule"
    """A detector sweep."""

    FOLLOW_UP = "follow_up"
    """A scheduled re-check from an earlier run. Carries ``parent_run_id``, which is
    what links Tuesday's arrival check back to the reroute that asked for it."""

    MANUAL = "manual"


class AgentRun(BaseModel):
    """One pass of the loop."""

    run_id: str = Field(default_factory=lambda: new_id("RUN"))
    profile_id: str
    principal_id: str
    attention_item_id: str | None = None
    state: RunState = RunState.DETECTED
    proposal_id: str | None = None
    trigger: TriggerKind = TriggerKind.SCHEDULE
    parent_run_id: str | None = None
    error: str | None = None
    created_at: _dt.datetime | None = None
    updated_at: _dt.datetime | None = None


class RunRepository:
    """Persistence and state transitions for runs."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def create(self, run: AgentRun, *, now: _dt.datetime) -> AgentRun:
        run.created_at = now
        run.updated_at = now
        self._store.execute(
            """
            INSERT INTO runs (
                run_id, profile_id, principal_id, attention_item_id, state,
                proposal_id, trigger, parent_run_id, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.profile_id,
                run.principal_id,
                run.attention_item_id,
                run.state.value,
                run.proposal_id,
                run.trigger.value,
                run.parent_run_id,
                run.error,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return run

    def transition(
        self,
        run: AgentRun,
        state: RunState,
        *,
        audit,
        now: _dt.datetime,
        reason: str = "",
        error: str | None = None,
    ) -> AgentRun:
        """Move to a new state, persisting and auditing together."""
        previous = run.state
        run.state = state
        run.error = error if error is not None else run.error
        run.updated_at = now
        self._store.execute(
            "UPDATE runs SET state = ?, error = ?, updated_at = ? WHERE run_id = ?",
            (state.value, run.error, now.isoformat(), run.run_id),
        )
        audit.emit(
            EventType.RUN_STATE_CHANGED,
            reason or f"{previous.value} → {state.value}",
            from_state=previous.value,
            to_state=state.value,
            error=error,
        )
        return run

    def set_proposal(self, run: AgentRun, proposal_id: str) -> None:
        run.proposal_id = proposal_id
        self._store.execute(
            "UPDATE runs SET proposal_id = ? WHERE run_id = ?", (proposal_id, run.run_id)
        )

    def set_attention_item(self, run: AgentRun, item_id: str) -> None:
        run.attention_item_id = item_id
        self._store.execute(
            "UPDATE runs SET attention_item_id = ? WHERE run_id = ?", (item_id, run.run_id)
        )

    def get(self, run_id: str) -> AgentRun | None:
        row = self._store.query_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return self._row_to_run(row) if row else None

    def awaiting_approval(self) -> list[AgentRun]:
        rows = self._store.query(
            "SELECT * FROM runs WHERE state = ? ORDER BY created_at",
            (RunState.AWAITING_APPROVAL.value,),
        )
        return [self._row_to_run(r) for r in rows]

    def recent(self, limit: int = 20) -> list[AgentRun]:
        rows = self._store.query(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _row_to_run(row) -> AgentRun:
        return AgentRun(
            run_id=row["run_id"],
            profile_id=row["profile_id"],
            principal_id=row["principal_id"],
            attention_item_id=row["attention_item_id"],
            state=RunState(row["state"]),
            proposal_id=row["proposal_id"],
            trigger=TriggerKind(row["trigger"]),
            parent_run_id=row["parent_run_id"],
            error=row["error"],
            created_at=_dt.datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else None,
            updated_at=_dt.datetime.fromisoformat(row["updated_at"])
            if row["updated_at"]
            else None,
        )
