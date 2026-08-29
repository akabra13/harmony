"""Persistence for proposals.

Proposals are stored rather than held in memory because an approval outlives the
process that produced it. A manager who approves on Thursday something proposed on
Tuesday must be shown the same plan, and the executor must run that plan and not a
re-derived one — re-planning at approval time would mean the human agreed to a
proposal nobody ever executed.
"""

from __future__ import annotations

import datetime as _dt

from harmony.kernel.store import Store, dump_json, load_json
from harmony.plan.models import EvidenceRef, Proposal


class ProposalRepository:
    """Saves and loads proposals by id."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def save(self, proposal: Proposal, *, now: _dt.datetime) -> Proposal:
        proposal.created_at = proposal.created_at or now
        self._store.execute(
            """
            INSERT INTO proposals (
                proposal_id, run_id, digest, summary, reasoning, evidence, action,
                alternatives, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal.proposal_id,
                proposal.run_id,
                proposal.digest(),
                proposal.summary,
                proposal.reasoning,
                dump_json([e.model_dump() for e in proposal.evidence]),
                dump_json(proposal.action.model_dump(mode="json")),
                dump_json(proposal.alternatives_considered),
                proposal.created_at.isoformat(),
            ),
        )
        return proposal

    def get(self, proposal_id: str) -> Proposal | None:
        row = self._store.query_one(
            "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
        )
        return self._row_to_proposal(row) if row else None

    def for_run(self, run_id: str) -> list[Proposal]:
        rows = self._store.query(
            "SELECT * FROM proposals WHERE run_id = ? ORDER BY created_at", (run_id,)
        )
        return [self._row_to_proposal(r) for r in rows]

    @staticmethod
    def _row_to_proposal(row) -> Proposal:
        return Proposal(
            proposal_id=row["proposal_id"],
            run_id=row["run_id"],
            summary=row["summary"],
            reasoning=row["reasoning"],
            action=load_json(row["action"], {}),
            evidence=[EvidenceRef(**e) for e in load_json(row["evidence"], [])],
            alternatives_considered=load_json(row["alternatives"], []),
            created_at=_dt.datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else None,
        )
