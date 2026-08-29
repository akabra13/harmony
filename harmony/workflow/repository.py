"""Persistence for workflow instances.

Separated from the engine so the engine reads as an algorithm rather than as an
algorithm interleaved with SQL. The one method that matters for correctness is
:meth:`InstanceRepository.commit_step`, which writes a step's result and advances
the cursor **in a single transaction**.

That atomicity is the entire resumption guarantee. A process killed between the two
would either re-run a completed step (harmless — the idempotency key makes it a
replay) or skip an unrun one (not harmless at all). Committing them together means
the second case cannot happen.
"""

from __future__ import annotations

import datetime as _dt

from harmony.kernel.store import Store, dump_json, load_json
from harmony.workflow.models import InstanceStatus, WorkflowInstance


class InstanceRepository:
    """Loads and saves workflow instances."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def create(self, instance: WorkflowInstance, *, now: _dt.datetime) -> WorkflowInstance:
        instance.created_at = now
        instance.updated_at = now
        self._store.execute(
            """
            INSERT INTO workflow_instances (
                instance_id, run_id, definition_name, definition_version, params,
                status, cursor, step_results, compensation_log, error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance.instance_id,
                instance.run_id,
                instance.definition_name,
                instance.definition_version,
                dump_json(instance.params),
                instance.status.value,
                instance.cursor,
                dump_json(instance.step_results),
                dump_json(instance.compensation_log),
                instance.error,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return instance

    def get(self, instance_id: str) -> WorkflowInstance | None:
        row = self._store.query_one(
            "SELECT * FROM workflow_instances WHERE instance_id = ?", (instance_id,)
        )
        return self._row_to_instance(row) if row else None

    def for_run(self, run_id: str) -> list[WorkflowInstance]:
        rows = self._store.query(
            "SELECT * FROM workflow_instances WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [self._row_to_instance(r) for r in rows]

    def unfinished(self) -> list[WorkflowInstance]:
        """Instances a restarted process should pick back up."""
        rows = self._store.query(
            "SELECT * FROM workflow_instances WHERE status IN (?, ?) ORDER BY created_at",
            (InstanceStatus.RUNNING.value, InstanceStatus.COMPENSATING.value),
        )
        return [self._row_to_instance(r) for r in rows]

    def commit_step(
        self,
        instance: WorkflowInstance,
        *,
        step_id: str,
        output: dict,
        now: _dt.datetime,
    ) -> None:
        """Record a step's result and advance the cursor, atomically.

        See the module docstring: these two writes must not be separable.
        """
        instance.step_results[step_id] = {"output": output}
        instance.cursor += 1
        instance.updated_at = now
        with self._store.tx() as conn:
            conn.execute(
                """
                UPDATE workflow_instances
                SET step_results = ?, cursor = ?, updated_at = ?
                WHERE instance_id = ?
                """,
                (
                    dump_json(instance.step_results),
                    instance.cursor,
                    now.isoformat(),
                    instance.instance_id,
                ),
            )

    def set_status(
        self,
        instance: WorkflowInstance,
        status: InstanceStatus,
        *,
        now: _dt.datetime,
        error: str | None = None,
    ) -> None:
        instance.status = status
        instance.error = error if error is not None else instance.error
        instance.updated_at = now
        self._store.execute(
            "UPDATE workflow_instances SET status = ?, error = ?, updated_at = ? "
            "WHERE instance_id = ?",
            (status.value, instance.error, now.isoformat(), instance.instance_id),
        )

    def record_compensation(
        self, instance: WorkflowInstance, entry: dict, *, now: _dt.datetime
    ) -> None:
        instance.compensation_log.append(entry)
        instance.updated_at = now
        self._store.execute(
            "UPDATE workflow_instances SET compensation_log = ?, updated_at = ? "
            "WHERE instance_id = ?",
            (dump_json(instance.compensation_log), now.isoformat(), instance.instance_id),
        )

    @staticmethod
    def _row_to_instance(row) -> WorkflowInstance:
        return WorkflowInstance(
            instance_id=row["instance_id"],
            run_id=row["run_id"],
            definition_name=row["definition_name"],
            definition_version=row["definition_version"],
            params=load_json(row["params"], {}),
            status=InstanceStatus(row["status"]),
            cursor=row["cursor"],
            step_results=load_json(row["step_results"], {}),
            compensation_log=load_json(row["compensation_log"], []),
            error=row["error"],
            created_at=_dt.datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else None,
            updated_at=_dt.datetime.fromisoformat(row["updated_at"])
            if row["updated_at"]
            else None,
        )
