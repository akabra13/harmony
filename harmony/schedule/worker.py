"""The worker: drains due tasks and dispatches them to handlers by kind.

The loop is intentionally boring — release expired leases, find what is due, claim
it, run it, record the outcome. All the interesting behaviour lives in handlers
registered by kind, so adding a new sort of deferred work never touches this file.

Two handler kinds ship with the harness and both are domain-free:

``detector.run``
    Invoke one detector with a payload. This is what a follow-up *is*: the Tuesday
    arrival check is not a special mechanism, it is this detector-with-an-argument
    firing on a date the workflow chose.

``approval.escalate``
    Re-examine an approval that nobody answered, and route it onward if the
    approver is out tomorrow.

Under a simulated clock the worker does not sleep. :meth:`Worker.drain` runs ticks
until nothing more is due, which is what makes "advance to Tuesday" fire the
follow-up immediately and deterministically instead of after a real week.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from harmony.audit.models import EventType
from harmony.kernel.registry import Registry
from harmony.schedule.models import ScheduledTask
from harmony.schedule.queue import TaskQueue

if TYPE_CHECKING:
    from harmony.runtime.harness import Harness


@dataclass
class TaskContext:
    """What a task handler is given."""

    harness: "Harness"
    task: ScheduledTask
    now: _dt.datetime

    @property
    def payload(self) -> dict[str, Any]:
        return self.task.payload


TaskHandler = Callable[[TaskContext], Any]

TASK_HANDLERS: Registry[TaskHandler] = Registry("task handler")


def task_handler(kind: str) -> Callable[[TaskHandler], TaskHandler]:
    """Register a handler for one task kind.

        @task_handler("approval.escalate")
        def escalate(ctx: TaskContext) -> None: ...
    """

    def decorator(fn: TaskHandler) -> TaskHandler:
        TASK_HANDLERS.register(kind, fn)
        return fn

    return decorator


class Worker:
    """Runs due tasks. One per process; the lease is what makes more than one safe."""

    def __init__(self, harness: "Harness", *, name: str = "worker-1") -> None:
        self._harness = harness
        self._queue: TaskQueue = harness.tasks
        self.name = name

    def tick(self) -> list[ScheduledTask]:
        """Run every task due at the current instant. Returns what was run."""
        now = self._harness.clock.now()
        self._queue.release_expired_leases(now)

        ran: list[ScheduledTask] = []
        for task in self._queue.due(now):
            if not self._queue.claim(task, owner=self.name, now=now):
                continue  # another worker took it
            self._run(task, now)
            ran.append(task)
        return ran

    def drain(self, *, max_rounds: int = 25) -> list[ScheduledTask]:
        """Tick until nothing is due.

        A task may schedule another that is already due — a follow-up whose check
        immediately raises a new item, say — so one tick is not enough. The round
        cap turns a scheduling cycle into a loud failure instead of a hang.
        """
        ran: list[ScheduledTask] = []
        for _ in range(max_rounds):
            batch = self.tick()
            if not batch:
                return ran
            ran.extend(batch)
        raise RuntimeError(
            f"scheduler did not settle after {max_rounds} rounds; "
            "a task is likely scheduling itself"
        )

    def _run(self, task: ScheduledTask, now: _dt.datetime) -> None:
        audit = self._harness.system_audit(run_id=task.run_id)
        audit.emit(
            EventType.SCHEDULE_TASK_FIRED,
            f"firing {task.kind}",
            task_id=task.task_id,
            kind=task.kind,
            scheduled_for=task.fire_at.isoformat(),
            payload=task.payload,
        )
        handler = TASK_HANDLERS.try_get(task.kind)
        if handler is None:
            self._queue.fail(task.task_id, error=f"no handler for kind '{task.kind}'", now=now)
            audit.emit(
                EventType.SCHEDULE_TASK_FAILED,
                f"no handler registered for '{task.kind}'",
                task_id=task.task_id,
                kind=task.kind,
            )
            return

        try:
            handler(TaskContext(harness=self._harness, task=task, now=now))
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self._queue.fail(task.task_id, error=str(exc), now=now)
            audit.emit(
                EventType.SCHEDULE_TASK_FAILED,
                f"{task.kind} failed: {exc}",
                task_id=task.task_id,
                kind=task.kind,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        self._queue.complete(task.task_id, now=now)
