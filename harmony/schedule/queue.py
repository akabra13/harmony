"""The durable task queue.

Small, deliberately. It does the four things a queue must do — enqueue with
deduplication, find what is due, lease it so two workers cannot both run it, and
record the outcome — and nothing else. Retries, backoff policies and priority are
absent; the README says so under "what I cut", and DESIGN.md says what replaces
this at scale.

The lease is the part worth keeping even at this size. Without it, "the worker
crashed mid-task" and "the worker is still working" are indistinguishable, and the
system must choose between stranding work and running it twice.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

from harmony.schedule.models import ScheduledTask, TaskState
from harmony.kernel.store import Store, dump_json, load_json

DEFAULT_LEASE_SECONDS = 300


class TaskQueue:
    """Durable, deduplicated, leased task storage."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # --- writing ---------------------------------------------------------------

    def enqueue(self, task: ScheduledTask, *, now: _dt.datetime) -> ScheduledTask | None:
        """Insert a task. Returns ``None`` when its dedupe key already exists.

        A ``None`` return is a normal outcome, not an error: it means this exact
        follow-up was already scheduled. Callers audit it as a no-op.
        """
        task.created_at = task.created_at or now
        try:
            self._store.execute(
                """
                INSERT INTO scheduled_tasks (
                    task_id, kind, payload, fire_at, state, dedupe_key, run_id,
                    attempts, last_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (
                    task.task_id,
                    task.kind,
                    dump_json(task.payload),
                    task.fire_at.isoformat(),
                    TaskState.PENDING.value,
                    task.dedupe_key,
                    task.run_id,
                    task.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return task

    # --- reading ---------------------------------------------------------------

    def due(self, now: _dt.datetime, *, limit: int = 100) -> list[ScheduledTask]:
        """Pending tasks whose time has come, oldest first."""
        rows = self._store.query(
            """
            SELECT * FROM scheduled_tasks
            WHERE state = ? AND fire_at <= ?
            ORDER BY fire_at, created_at
            LIMIT ?
            """,
            (TaskState.PENDING.value, now.isoformat(), limit),
        )
        return [self._row_to_task(r) for r in rows]

    def pending(self) -> list[ScheduledTask]:
        rows = self._store.query(
            "SELECT * FROM scheduled_tasks WHERE state IN (?, ?) ORDER BY fire_at",
            (TaskState.PENDING.value, TaskState.LEASED.value),
        )
        return [self._row_to_task(r) for r in rows]

    def get(self, task_id: str) -> ScheduledTask | None:
        row = self._store.query_one(
            "SELECT * FROM scheduled_tasks WHERE task_id = ?", (task_id,)
        )
        return self._row_to_task(row) if row else None

    def by_dedupe_key(self, key: str) -> ScheduledTask | None:
        row = self._store.query_one(
            "SELECT * FROM scheduled_tasks WHERE dedupe_key = ?", (key,)
        )
        return self._row_to_task(row) if row else None

    # --- leasing ---------------------------------------------------------------

    def claim(
        self,
        task: ScheduledTask,
        *,
        owner: str,
        now: _dt.datetime,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Take ownership of a task. False when another worker got there first."""
        until = now + _dt.timedelta(seconds=lease_seconds)
        with self._store.tx() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_tasks
                SET state = ?, lease_owner = ?, lease_until = ?, attempts = attempts + 1
                WHERE task_id = ? AND state = ?
                """,
                (
                    TaskState.LEASED.value,
                    owner,
                    until.isoformat(),
                    task.task_id,
                    TaskState.PENDING.value,
                ),
            )
            return cursor.rowcount == 1

    def release_expired_leases(self, now: _dt.datetime) -> int:
        """Return abandoned tasks to the pending pool. Called at the top of a tick,
        which is how a killed worker's in-flight task gets picked up again."""
        with self._store.tx() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_tasks
                SET state = ?, lease_owner = NULL, lease_until = NULL
                WHERE state = ? AND lease_until < ?
                """,
                (TaskState.PENDING.value, TaskState.LEASED.value, now.isoformat()),
            )
            return cursor.rowcount

    # --- completion ------------------------------------------------------------

    def complete(self, task_id: str, *, now: _dt.datetime) -> None:
        self._store.execute(
            """
            UPDATE scheduled_tasks
            SET state = ?, fired_at = ?, lease_owner = NULL, lease_until = NULL
            WHERE task_id = ?
            """,
            (TaskState.DONE.value, now.isoformat(), task_id),
        )

    def fail(self, task_id: str, *, error: str, now: _dt.datetime) -> None:
        self._store.execute(
            """
            UPDATE scheduled_tasks
            SET state = ?, last_error = ?, fired_at = ?, lease_owner = NULL, lease_until = NULL
            WHERE task_id = ?
            """,
            (TaskState.FAILED.value, error[:2000], now.isoformat(), task_id),
        )

    def cancel(self, task_id: str) -> None:
        self._store.execute(
            "UPDATE scheduled_tasks SET state = ? WHERE task_id = ? AND state = ?",
            (TaskState.CANCELLED.value, task_id, TaskState.PENDING.value),
        )

    # --- mapping ---------------------------------------------------------------

    @staticmethod
    def _row_to_task(row) -> ScheduledTask:
        return ScheduledTask(
            task_id=row["task_id"],
            kind=row["kind"],
            payload=load_json(row["payload"], {}),
            fire_at=_dt.datetime.fromisoformat(row["fire_at"]),
            state=TaskState(row["state"]),
            dedupe_key=row["dedupe_key"],
            run_id=row["run_id"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=_dt.datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else None,
            fired_at=_dt.datetime.fromisoformat(row["fired_at"]) if row["fired_at"] else None,
            lease_owner=row["lease_owner"],
            lease_until=_dt.datetime.fromisoformat(row["lease_until"])
            if row["lease_until"]
            else None,
        )
