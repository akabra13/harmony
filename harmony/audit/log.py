"""Append-only, hash-chained audit log.

Append-only is enforced three ways, in increasing order of seriousness:

1. There is no update or delete method on this class.
2. SQL triggers reject ``UPDATE`` and ``DELETE`` on ``audit_events``.
3. Each entry hashes the previous entry's hash, so editing history requires
   rewriting every subsequent row. :meth:`verify_chain` detects the attempt.

The third is the one that matters. An enterprise audit trail whose integrity rests
on "we didn't write an update method" is a promise; a chain is evidence.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from typing import Any

from harmony.audit.models import AuditEvent, EventType
from harmony.kernel.clock import Clock, SystemClock
from harmony.kernel.ids import digest, new_id
from harmony.kernel.store import Store, dump_json, load_json

GENESIS_HASH = "0" * 64


class AuditLog:
    """Writes and reads the ledger. Never updates it."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self._wall = SystemClock()

    # --- writing ---------------------------------------------------------------

    def append(
        self,
        *,
        event_type: EventType,
        actor_kind: str,
        actor_id: str,
        run_id: str | None = None,
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append one event and return it, with its chain position filled in."""
        payload = payload or {}
        with self._store.tx() as conn:
            row = conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["entry_hash"] if row else GENESIS_HASH

            event = AuditEvent(
                event_id=new_id("EV", 8),
                run_id=run_id,
                ts_clock=self._clock.now(),
                ts_wall=self._wall.now(),
                actor_kind=actor_kind,
                actor_id=actor_id,
                event_type=event_type,
                summary=summary,
                payload=payload,
                prev_hash=prev_hash,
            )
            event.entry_hash = self._hash(event)

            cursor = conn.execute(
                """
                INSERT INTO audit_events (
                    event_id, run_id, ts_clock, ts_wall, actor_kind, actor_id,
                    event_type, summary, payload, prev_hash, entry_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.run_id,
                    event.ts_clock.isoformat(),
                    event.ts_wall.isoformat(),
                    event.actor_kind,
                    event.actor_id,
                    event.event_type.value,
                    event.summary,
                    dump_json(event.payload),
                    event.prev_hash,
                    event.entry_hash,
                ),
            )
            event.seq = cursor.lastrowid
        return event

    @staticmethod
    def _hash(event: AuditEvent) -> str:
        """Hash the fields that constitute the record. Excludes ``seq`` (assigned by
        the database) and ``entry_hash`` (the output)."""
        return digest(
            event.prev_hash,
            event.event_id,
            event.run_id,
            event.ts_clock.isoformat(),
            event.actor_kind,
            event.actor_id,
            event.event_type.value,
            event.summary,
            event.payload,
        )

    # --- reading ---------------------------------------------------------------

    def for_run(self, run_id: str) -> list[AuditEvent]:
        rows = self._store.query(
            "SELECT * FROM audit_events WHERE run_id = ? ORDER BY seq", (run_id,)
        )
        return [self._row_to_event(r) for r in rows]

    def recent(self, limit: int = 50) -> list[AuditEvent]:
        rows = self._store.query(
            "SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?", (limit,)
        )
        return [self._row_to_event(r) for r in reversed(rows)]

    def all_runs(self) -> list[str]:
        rows = self._store.query(
            "SELECT DISTINCT run_id FROM audit_events WHERE run_id IS NOT NULL ORDER BY seq"
        )
        return [r["run_id"] for r in rows]

    @staticmethod
    def _row_to_event(row: Any) -> AuditEvent:
        return AuditEvent(
            seq=row["seq"],
            event_id=row["event_id"],
            run_id=row["run_id"],
            ts_clock=_dt.datetime.fromisoformat(row["ts_clock"]),
            ts_wall=_dt.datetime.fromisoformat(row["ts_wall"]),
            actor_kind=row["actor_kind"],
            actor_id=row["actor_id"],
            event_type=EventType(row["event_type"]),
            summary=row["summary"] or "",
            payload=load_json(row["payload"], {}),
            prev_hash=row["prev_hash"],
            entry_hash=row["entry_hash"],
        )

    # --- integrity -------------------------------------------------------------

    def verify_chain(self) -> tuple[bool, str | None]:
        """Recompute every hash. Returns ``(ok, first_broken_event_id)``."""
        prev = GENESIS_HASH
        for row in self._store.query("SELECT * FROM audit_events ORDER BY seq"):
            event = self._row_to_event(row)
            if event.prev_hash != prev or self._hash(event) != event.entry_hash:
                return False, event.event_id
            prev = event.entry_hash
        return True, None


class AuditWriter:
    """A run-scoped, actor-bound view of the log.

    Handed to sessions, providers and tools so they cannot accidentally write an
    event attributed to the wrong actor or the wrong run. The kernel passes one of
    these around; nothing outside :mod:`harmony.audit` holds the :class:`AuditLog`.
    """

    def __init__(self, log: AuditLog, *, run_id: str | None, actor_kind: str, actor_id: str):
        self._log = log
        self.run_id = run_id
        self.actor_kind = actor_kind
        self.actor_id = actor_id

    def emit(
        self,
        event_type: EventType,
        summary: str = "",
        **payload: Any,
    ) -> AuditEvent:
        return self._log.append(
            event_type=event_type,
            actor_kind=self.actor_kind,
            actor_id=self.actor_id,
            run_id=self.run_id,
            summary=summary,
            payload=payload,
        )

    def bind(
        self,
        *,
        run_id: str | None = None,
        actor_id: str | None = None,
        actor_kind: str | None = None,
    ) -> AuditWriter:
        """A writer for the same log with a different run or actor."""
        return AuditWriter(
            self._log,
            run_id=run_id if run_id is not None else self.run_id,
            actor_kind=actor_kind or self.actor_kind,
            actor_id=actor_id or self.actor_id,
        )

    def events(self) -> Sequence[AuditEvent]:
        return self._log.for_run(self.run_id) if self.run_id else []
