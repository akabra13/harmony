"""Durable memory: beliefs that outlive a run.

The rule that shapes this whole module, and the one worth arguing for in DESIGN.md:

    **Memory holds derived judgments, never system-of-record state.**

"Supplier Y has slipped three of its last five commitments" is a judgment — it took
work to derive, it stays roughly true, and it is useful to a run that has not looked
at the shipping history. "PO-77812 is open" is state, and memory must never hold it,
because the ERP is where that question is answered and a cached copy is a lie
waiting to happen.

The second rule follows from the first:

    **Memory is strictly advisory.**

A recalled fact can influence how the planner ranks its options or phrases its
recommendation. It can never satisfy a gate condition or supply a tool parameter.
So a stale belief can make the agent's *suggestion* worse — which a human then
rejects — but it can never make the agent's *actions* unsafe. That asymmetry is the
entire staleness defence, and it is cheaper and more reliable than trying to keep
every belief fresh.

The implementation here is deliberately small; the interface is the part that has
to be right. See the README under "what I cut".
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from harmony.audit.models import EventType
from harmony.identity.session import Session
from harmony.kernel.ids import new_id
from harmony.kernel.store import Store, dump_json, load_json


class FactState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CONTRADICTED = "contradicted"
    """Demoted because a run observed data that disagreed with it. Kept rather than
    deleted: "we used to believe this and stopped" is itself auditable."""


class MemoryFact(BaseModel):
    """One durable belief, with the provenance needed to check it."""

    fact_id: str = Field(default_factory=lambda: new_id("MEM"))
    scope_kind: str = "user"
    """``user`` for a personal preference, ``org`` for something everyone shares."""

    scope_id: str
    subject: str
    """What the belief is about: ``supplier:S-Y``, ``part:P-4471``."""

    predicate: str
    object: Any = None
    statement: str
    """The belief in one line, as the planner will read it."""

    confidence: float = 0.5
    provenance: dict[str, Any] = Field(default_factory=dict)
    """Where it came from: run ids, source systems, how many observations."""

    observed_at: _dt.datetime | None = None
    expires_at: _dt.datetime | None = None
    state: FactState = FactState.ACTIVE

    def is_fresh(self, now: _dt.datetime) -> bool:
        return self.state is FactState.ACTIVE and (
            self.expires_at is None or self.expires_at > now
        )

    def for_prompt(self) -> dict[str, Any]:
        return {
            "belief": self.statement,
            "about": self.subject,
            "confidence": self.confidence,
            "observed_at": self.observed_at.date().isoformat() if self.observed_at else None,
            "advisory_only": True,
        }


class MemoryStore:
    """Promotion, recall, and demotion of durable beliefs."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # --- promotion -------------------------------------------------------------

    def promote(
        self,
        session: Session,
        *,
        scope_id: str,
        subject: str,
        predicate: str,
        statement: str,
        object: Any = None,
        confidence: float = 0.6,
        ttl_days: int | None = 90,
        scope_kind: str = "user",
        provenance: dict[str, Any] | None = None,
    ) -> MemoryFact:
        """Record a belief, replacing any active belief with the same subject and
        predicate. Supersession rather than accumulation: two live answers to
        "how reliable is Supplier Y?" would leave the planner to pick one."""
        now = session.clock.now()
        self._supersede_matching(scope_kind, scope_id, subject, predicate)

        fact = MemoryFact(
            scope_kind=scope_kind,
            scope_id=scope_id,
            subject=subject,
            predicate=predicate,
            object=object,
            statement=statement,
            confidence=confidence,
            observed_at=now,
            expires_at=now + _dt.timedelta(days=ttl_days) if ttl_days else None,
            provenance={"run_id": session.run_id, **(provenance or {})},
        )
        self._insert(fact)
        session.audit.emit(
            EventType.MEMORY_PROMOTED,
            f"remembering: {statement}",
            fact_id=fact.fact_id,
            subject=subject,
            predicate=predicate,
            confidence=confidence,
            expires_at=fact.expires_at.isoformat() if fact.expires_at else None,
        )
        return fact

    # --- recall ----------------------------------------------------------------

    def recall(
        self,
        session: Session,
        *,
        scope_id: str,
        subjects: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryFact]:
        """Fetch fresh beliefs relevant to this run.

        Expired facts are demoted on read rather than by a sweep. A belief nobody
        consults does not need to be tidied; one that is consulted must be current.
        """
        now = session.clock.now()
        rows = self._store.query(
            """
            SELECT * FROM memory_facts
            WHERE scope_id IN (?, 'org') AND state = ?
            ORDER BY observed_at DESC
            """,
            (scope_id, FactState.ACTIVE.value),
        )
        facts = [self._row_to_fact(r) for r in rows]

        fresh: list[MemoryFact] = []
        for fact in facts:
            if not fact.is_fresh(now):
                self._set_state(fact.fact_id, FactState.EXPIRED)
                continue
            if subjects and fact.subject not in subjects:
                continue
            fresh.append(fact)

        fresh = fresh[:limit]
        if fresh:
            session.audit.emit(
                EventType.MEMORY_RECALLED,
                f"recalled {len(fresh)} belief(s)",
                facts=[{"id": f.fact_id, "statement": f.statement} for f in fresh],
                advisory_only=True,
            )
        return fresh

    # --- demotion --------------------------------------------------------------

    def contradict(self, session: Session, fact_id: str, *, observed: str) -> None:
        """Demote a belief a run has just disproved."""
        self._set_state(fact_id, FactState.CONTRADICTED)
        session.audit.emit(
            EventType.MEMORY_DEMOTED,
            f"belief contradicted by observation: {observed}",
            fact_id=fact_id,
            observed=observed,
        )

    # --- storage ---------------------------------------------------------------

    def _insert(self, fact: MemoryFact) -> None:
        self._store.execute(
            """
            INSERT INTO memory_facts (
                fact_id, scope_kind, scope_id, subject, predicate, object, statement,
                confidence, provenance, observed_at, expires_at, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.fact_id,
                fact.scope_kind,
                fact.scope_id,
                fact.subject,
                fact.predicate,
                dump_json(fact.object),
                fact.statement,
                fact.confidence,
                dump_json(fact.provenance),
                fact.observed_at.isoformat() if fact.observed_at else None,
                fact.expires_at.isoformat() if fact.expires_at else None,
                fact.state.value,
            ),
        )

    def _supersede_matching(
        self, scope_kind: str, scope_id: str, subject: str, predicate: str
    ) -> None:
        self._store.execute(
            """
            UPDATE memory_facts SET state = ?
            WHERE scope_kind = ? AND scope_id = ? AND subject = ? AND predicate = ?
              AND state = ?
            """,
            (
                FactState.EXPIRED.value,
                scope_kind,
                scope_id,
                subject,
                predicate,
                FactState.ACTIVE.value,
            ),
        )

    def _set_state(self, fact_id: str, state: FactState) -> None:
        self._store.execute(
            "UPDATE memory_facts SET state = ? WHERE fact_id = ?", (state.value, fact_id)
        )

    @staticmethod
    def _row_to_fact(row) -> MemoryFact:
        return MemoryFact(
            fact_id=row["fact_id"],
            scope_kind=row["scope_kind"],
            scope_id=row["scope_id"],
            subject=row["subject"],
            predicate=row["predicate"],
            object=load_json(row["object"]),
            statement=row["statement"],
            confidence=row["confidence"],
            provenance=load_json(row["provenance"], {}),
            observed_at=_dt.datetime.fromisoformat(row["observed_at"])
            if row["observed_at"]
            else None,
            expires_at=_dt.datetime.fromisoformat(row["expires_at"])
            if row["expires_at"]
            else None,
            state=FactState(row["state"]),
        )
