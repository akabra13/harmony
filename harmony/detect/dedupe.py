"""Dedupe: deciding whether a detection is news.

Detectors are pure — they look at the world and say what they see, every time they
run. Running one three times finds the same shortfall three times, and an agent that
alerted three times would be turned off by lunchtime. This module is where a
detection becomes, or fails to become, an event.

Three outcomes, keyed on the two hashes an item carries:

``RAISED``
    No open item with this fingerprint. Genuinely new; open a run.

``SUPPRESSED``
    An open item with the same fingerprint *and* the same content. Bump the
    counter and last-seen; do nothing else. The situation is unchanged and the
    human has already been told.

``SUPERSEDED``
    An open item with the same fingerprint but *different* content. The facts moved
    — the delay got worse, the shortfall grew. Close the old item, open a new one,
    and run. This is the case a naive "have I seen this fingerprint?" check gets
    wrong, and it is the case that matters most.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from enum import StrEnum

from harmony.audit.models import EventType
from harmony.detect.models import AttentionItem, ItemState, Severity
from harmony.identity.session import Session
from harmony.kernel.store import Store, dump_json, load_json
from harmony.providers.base import SubjectRef


class DedupeOutcome(StrEnum):
    RAISED = "raised"
    SUPPRESSED = "suppressed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class DedupeResult:
    outcome: DedupeOutcome
    item: AttentionItem
    previous_item_id: str | None = None

    @property
    def should_run(self) -> bool:
        """Whether this detection warrants a pass of the agent loop."""
        return self.outcome in (DedupeOutcome.RAISED, DedupeOutcome.SUPERSEDED)


class AttentionItemStore:
    """Persistence and dedupe for attention items."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # --- the decision ----------------------------------------------------------

    def admit(self, session: Session, item: AttentionItem) -> DedupeResult:
        """Decide what to do with a fresh detection, and record the decision."""
        now = session.clock.now()
        item.first_seen = item.first_seen or now
        item.last_seen = now

        existing = self._open_with_fingerprint(item.fingerprint)

        if existing is None:
            self._insert(item)
            session.audit.emit(
                EventType.ATTENTION_ITEM_RAISED,
                item.title,
                item_id=item.item_id,
                detector=item.detector_id,
                severity=item.severity.value,
                subjects=[str(s) for s in item.subjects],
                findings=item.facts,
                evidence=[e.model_dump(exclude_none=True) for e in item.evidence],
            )
            return DedupeResult(DedupeOutcome.RAISED, item)

        if existing.content_hash == item.content_hash:
            self._touch(existing.item_id, now)
            session.audit.emit(
                EventType.ATTENTION_ITEM_SUPPRESSED,
                f"already open and unchanged: {existing.title}",
                item_id=existing.item_id,
                detector=item.detector_id,
                seen_count=existing.seen_count + 1,
                fingerprint=item.fingerprint[:12],
            )
            existing.seen_count += 1
            existing.last_seen = now
            return DedupeResult(DedupeOutcome.SUPPRESSED, existing)

        self._supersede(existing.item_id, item.item_id, now)
        self._insert(item)
        session.audit.emit(
            EventType.ATTENTION_ITEM_SUPERSEDED,
            f"situation changed: {item.title}",
            item_id=item.item_id,
            supersedes=existing.item_id,
            detector=item.detector_id,
            previous_findings=existing.facts,
            findings=item.facts,
        )
        return DedupeResult(DedupeOutcome.SUPERSEDED, item, previous_item_id=existing.item_id)

    # --- queries ---------------------------------------------------------------

    def get(self, item_id: str) -> AttentionItem | None:
        row = self._store.query_one(
            "SELECT * FROM attention_items WHERE item_id = ?", (item_id,)
        )
        return self._row_to_item(row) if row else None

    def open_for(self, principal_id: str) -> list[AttentionItem]:
        rows = self._store.query(
            "SELECT * FROM attention_items WHERE principal_id = ? AND state = ? "
            "ORDER BY first_seen",
            (principal_id, ItemState.OPEN.value),
        )
        return [self._row_to_item(r) for r in rows]

    def _open_with_fingerprint(self, fingerprint: str) -> AttentionItem | None:
        row = self._store.query_one(
            "SELECT * FROM attention_items WHERE fingerprint = ? AND state = ? "
            "ORDER BY first_seen DESC LIMIT 1",
            (fingerprint, ItemState.OPEN.value),
        )
        return self._row_to_item(row) if row else None

    # --- mutation --------------------------------------------------------------

    def attach_run(self, item_id: str, run_id: str) -> None:
        self._store.execute(
            "UPDATE attention_items SET run_id = ? WHERE item_id = ?", (run_id, item_id)
        )

    def set_state(self, item_id: str, state: ItemState) -> None:
        self._store.execute(
            "UPDATE attention_items SET state = ? WHERE item_id = ?", (state.value, item_id)
        )

    def _insert(self, item: AttentionItem) -> None:
        self._store.execute(
            """
            INSERT INTO attention_items (
                item_id, detector_id, principal_id, fingerprint, content_hash, state,
                severity, title, subject_refs, evidence, payload,
                first_seen, last_seen, seen_count, run_id, superseded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.item_id,
                item.detector_id,
                item.principal_id,
                item.fingerprint,
                item.content_hash,
                item.state.value,
                item.severity.value,
                item.title,
                dump_json([s.model_dump() for s in item.subjects]),
                dump_json([e.model_dump() for e in item.evidence]),
                dump_json(item.facts),
                item.first_seen.isoformat() if item.first_seen else None,
                item.last_seen.isoformat() if item.last_seen else None,
                item.seen_count,
                item.run_id,
                item.superseded_by,
            ),
        )

    def _touch(self, item_id: str, now: _dt.datetime) -> None:
        self._store.execute(
            "UPDATE attention_items SET last_seen = ?, seen_count = seen_count + 1 "
            "WHERE item_id = ?",
            (now.isoformat(), item_id),
        )

    def _supersede(self, old_id: str, new_id: str, now: _dt.datetime) -> None:
        self._store.execute(
            "UPDATE attention_items SET state = ?, superseded_by = ?, last_seen = ? "
            "WHERE item_id = ?",
            (ItemState.SUPERSEDED.value, new_id, now.isoformat(), old_id),
        )

    # --- mapping ---------------------------------------------------------------

    @staticmethod
    def _row_to_item(row) -> AttentionItem:
        return AttentionItem(
            item_id=row["item_id"],
            detector_id=row["detector_id"],
            principal_id=row["principal_id"],
            title=row["title"],
            severity=Severity(row["severity"]),
            subjects=[SubjectRef(**s) for s in load_json(row["subject_refs"], [])],
            evidence=load_json(row["evidence"], []),
            facts=load_json(row["payload"], {}),
            fingerprint=row["fingerprint"],
            content_hash=row["content_hash"],
            state=ItemState(row["state"]),
            first_seen=_dt.datetime.fromisoformat(row["first_seen"])
            if row["first_seen"]
            else None,
            last_seen=_dt.datetime.fromisoformat(row["last_seen"])
            if row["last_seen"]
            else None,
            seen_count=row["seen_count"],
            run_id=row["run_id"],
            superseded_by=row["superseded_by"],
        )
