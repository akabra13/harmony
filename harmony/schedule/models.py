"""Scheduled work: the shape of something the harness will do later.

Deferred work is a row, not a timer. That single choice is what makes it survive a
restart: a killed worker leaves the task exactly where a running one would, and the
next process to look finds it due. There is no in-memory schedule to reconstruct
and nothing to lose.

``dedupe_key`` is a UNIQUE column rather than an application-level check, so
scheduling the same follow-up twice is resolved by the database instead of by a
race the application has to win.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from harmony.kernel.ids import new_id


class TaskState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    """Claimed by a worker. A lease expires so a crashed worker's task returns to
    the pool rather than being stranded — the same reasoning as a visibility
    timeout on a real queue, which is what this becomes at scale."""

    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScheduledTask(BaseModel):
    """One unit of deferred work."""

    task_id: str = Field(default_factory=lambda: new_id("TSK"))
    kind: str
    """Which handler runs this. Handlers are registered by kind, so adding a new
    sort of deferred work does not touch the worker."""

    payload: dict[str, Any] = Field(default_factory=dict)
    fire_at: _dt.datetime
    state: TaskState = TaskState.PENDING
    dedupe_key: str | None = None
    run_id: str | None = None
    """The run that scheduled this, so the follow-up's audit can be linked back to
    the decision that asked for it."""

    attempts: int = 0
    last_error: str | None = None
    created_at: _dt.datetime | None = None
    fired_at: _dt.datetime | None = None
    lease_owner: str | None = None
    lease_until: _dt.datetime | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "fire_at": self.fire_at.isoformat(),
            "state": self.state.value,
            "payload": self.payload,
        }
