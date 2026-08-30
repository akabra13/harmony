"""An HTTP surface for the approval step.

The brief says a CLI or an HTTP endpoint is fine. Both exist, and the reason to have
both is not completeness — it is that a second front end is what proves the core is
a library rather than a script. Every route below is a thin wrapper over the same
:class:`Orchestrator` the CLI calls, and there is no behaviour here the CLI lacks.

**Authentication is deliberately absent, and faked in the crudest possible way.**
The caller declares who they are with an ``X-Harmony-User`` header. In a real
deployment that header is replaced by a validated OIDC token and the principal comes
from its claims — see DESIGN.md. What matters is that nothing *downstream* changes
when it is: the route resolves a user id, and every authority decision after that
already derives from the ``Session`` minted for that principal. Shipping this with a
fake header and saying so is more honest than shipping a homegrown auth scheme that
would have to be thrown away.

The one authorisation rule that would be a real hole without it — a person may
decide only approvals addressed to them — lives in :class:`ApprovalService` rather
than in this module, so the CLI enforces it too.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from harmony.audit.explain import RunExplainer
from harmony.kernel.errors import HarmonyError
from harmony.runtime.harness import Harness
from harmony.runtime.orchestrator import Orchestrator


class Decision(BaseModel):
    """A human's answer to an approval request."""

    note: str = Field(default="", max_length=2000)


def create_app(harness: Harness) -> FastAPI:
    """Build the API around an already-constructed harness.

    Taking the harness as an argument, rather than building one at import time,
    is what makes this testable and what keeps the composition root in one place.
    """
    app = FastAPI(
        title="Harmony",
        summary="Approval surface for the agent harness.",
        version="0.1.0",
    )
    orchestrator = Orchestrator(harness)

    def acting_user(x_harmony_user: str | None = Header(default=None)) -> str:
        """Resolve the caller. Stands in for token validation; see the module docstring."""
        if not x_harmony_user:
            raise HTTPException(401, "X-Harmony-User header is required")
        if harness.directory.try_get(x_harmony_user) is None:
            raise HTTPException(401, "no such user")
        return x_harmony_user

    # --- reading ---------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        chain_ok, broken_at = harness.audit_log.verify_chain()
        return {
            "deployment": harness.deployment.name,
            "clock": harness.clock.now().isoformat(),
            "audit_chain_intact": chain_ok,
            "audit_chain_broken_at": broken_at,
        }

    @app.get("/approvals")
    def list_approvals(user: str = Depends(acting_user)) -> list[dict[str, Any]]:
        """Approvals waiting on this person.

        Scoped to the caller rather than listing everything: an approval addressed
        to somebody else is not the caller's business, and an inbox that showed it
        would invite them to ask a colleague to click it.
        """
        return [
            {
                "approval_id": a.approval_id,
                "run_id": a.run_id,
                "reason": a.reason,
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
                "escalated": a.escalation_count > 0,
                "originally_for": a.originally_for,
            }
            for a in harness.approvals.open_for(user)
        ]

    @app.get("/approvals/{approval_id}")
    def show_approval(approval_id: str, user: str = Depends(acting_user)) -> dict[str, Any]:
        """What a decision would authorise."""
        approval = harness.approvals.get(approval_id)
        if approval is None:
            raise HTTPException(404, "no such approval")
        if approval.requested_of != user:
            raise HTTPException(403, "this approval was not addressed to you")

        proposal = harness.proposals.get(approval.proposal_id)
        if proposal is None:
            raise HTTPException(500, "the approval refers to a proposal that is missing")

        return {
            "approval_id": approval.approval_id,
            "state": approval.state.value,
            "reason": approval.reason,
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            "summary": proposal.summary,
            "reasoning": proposal.reasoning,
            "action": proposal.action.describe(),
            "alternatives_considered": proposal.alternatives_considered,
            "evidence": [e.model_dump() for e in proposal.evidence],
            # The digest binds a decision to this exact plan. A client that renders a
            # proposal and later posts a decision can compare digests and refuse if
            # the plan moved underneath the reviewer.
            "proposal_digest": approval.proposal_digest,
        }

    @app.get("/runs/{run_id}")
    def show_run(run_id: str, user: str = Depends(acting_user)) -> dict[str, Any]:
        run = harness.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        return {
            "run_id": run.run_id,
            "state": run.state.value,
            "profile": run.profile_id,
            "principal": run.principal_id,
            "trigger": run.trigger.value,
            "parent_run_id": run.parent_run_id,
            "error": run.error,
        }

    @app.get("/runs/{run_id}/audit")
    def run_audit(run_id: str, user: str = Depends(acting_user)) -> dict[str, Any]:
        """The run reconstructed from the ledger alone."""
        narrative = RunExplainer(harness.audit_log).explain_markdown(run_id)
        if narrative.startswith("No audit events"):
            raise HTTPException(404, "no audit history for that run")
        return {"run_id": run_id, "narrative": narrative}

    # --- deciding --------------------------------------------------------------

    @app.post("/approvals/{approval_id}/approve")
    def approve(
        approval_id: str, decision: Decision, user: str = Depends(acting_user)
    ) -> dict[str, Any]:
        """Approve, and execute what it authorises.

        Execution happens inline. That is right at this scale and wrong at any
        other: a workflow that takes ninety seconds should not be holding an HTTP
        connection open. A real deployment enqueues the execution and returns
        immediately, and the durable task queue is already the seam for it.
        """
        return _decide(approval_id, approve=True, user=user, note=decision.note)

    @app.post("/approvals/{approval_id}/reject")
    def reject(
        approval_id: str, decision: Decision, user: str = Depends(acting_user)
    ) -> dict[str, Any]:
        """Reject. Nothing is executed."""
        return _decide(approval_id, approve=False, user=user, note=decision.note)

    def _decide(approval_id: str, *, approve: bool, user: str, note: str) -> dict[str, Any]:
        if harness.approvals.get(approval_id) is None:
            raise HTTPException(404, "no such approval")

        try:
            run = (
                orchestrator.approve(approval_id, decided_by=user, note=note)
                if approve
                else orchestrator.reject(approval_id, decided_by=user, note=note)
            )
        except HarmonyError as exc:
            # The harness's own refusals — wrong approver, already decided, plan
            # changed since it was approved — are client errors, not server faults.
            # The structured payload travels out so a UI can say which.
            raise HTTPException(409, exc.to_payload()) from exc

        return {
            "run_id": run.run_id,
            "state": run.state.value,
            "decided_by": user,
            "error": run.error,
        }

    return app
