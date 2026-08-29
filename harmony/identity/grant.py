"""Execution grants: the artifact a human approval produces.

Scopes answer "is this person allowed to create purchase orders?". A grant answers
"did a human agree to *this* purchase order?". They are different questions and the
harness keeps them apart, because conflating them is how agents end up with
standing permission to do anything they were ever permitted to do once.

A grant is produced by the gate, carried by the executor, and demanded by the tool
invoker before any write. It is bound to a proposal digest, so if the plan changes
between approval and execution the grant stops matching and the write refuses. It
also carries an explicit allow-list of tool names: an approved reroute authorises
the reroute's tools and nothing else, even though the principal's scopes would
permit more.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExecutionGrant:
    """Authority to perform a specific, already-approved set of writes."""

    proposal_digest: str
    granted_by: str
    """The principal who approved. Not necessarily the principal who acts — an
    escalated approval is granted by a manager and executed as the requester."""

    granted_at: _dt.datetime
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    approval_id: str = ""
    reason: str = ""

    def permits(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools

    def matches(self, proposal_digest: str) -> bool:
        return self.proposal_digest == proposal_digest

    def describe(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at.isoformat(),
            "proposal_digest": self.proposal_digest[:12],
            "allowed_tools": sorted(self.allowed_tools),
        }


# A grant used for reads and for tools that touch no system of record. Writes never
# accept it; the invoker checks `writes` before it checks the grant.
NO_WRITES = ExecutionGrant(
    proposal_digest="",
    granted_by="",
    granted_at=_dt.datetime.min,
    allowed_tools=frozenset(),
    reason="read-only",
)
