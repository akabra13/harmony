"""Approval routing, escalation, and binding an approval to a plan."""

from __future__ import annotations

import datetime as _dt

import pytest

from harmony.gate.approvals import ApprovalState
from harmony.kernel.errors import ApprovalMismatch, HarmonyError
from harmony.plan.models import Proposal, WorkflowInvocation
from harmony.runtime.run import RunState
from harmony.schedule.worker import Worker


def _proposal(summary: str = "Reroute PO-77812", qty: int = 400) -> Proposal:
    return Proposal(
        summary=summary,
        reasoning="test",
        action=WorkflowInvocation(
            workflow="po_reroute",
            version=3,
            params={
                "at_risk_po_id": "PO-77812",
                "part_id": "P-4471",
                "production_order_id": "4812",
                "required_on_site_by": "2026-09-07",
                "qty": qty,
                "supervisor_id": "u-301",
            },
        ),
    )


# --- binding an approval to a plan ---------------------------------------------


def test_a_grant_covers_only_the_plan_that_was_approved(harness, dana_session):
    proposal = _proposal()
    approval = harness.approvals.request(
        dana_session, proposal=proposal, approver_id="u-101", reason="test"
    )
    harness.approvals.decide(
        dana_session, approval_id=approval.approval_id, approve=True, decided_by="u-101"
    )

    grant = harness.approvals.grant_for(
        harness.approvals.get(approval.approval_id),
        proposal=proposal,
        allowed_tools=frozenset({"erp.create_purchase_order"}),
    )
    assert grant.matches(proposal.digest())
    assert grant.permits("erp.create_purchase_order")
    assert not grant.permits("erp.cancel_purchase_order")


def test_changing_the_plan_after_approval_invalidates_it(harness, dana_session):
    """Approval is consent to a specific act, not a blanket. Silently substituting a
    different quantity would be the single worst failure this system could have."""
    approved = _proposal(qty=400)
    approval = harness.approvals.request(
        dana_session, proposal=approved, approver_id="u-101", reason="test"
    )
    harness.approvals.decide(
        dana_session, approval_id=approval.approval_id, approve=True, decided_by="u-101"
    )

    substituted = _proposal(qty=4000)
    with pytest.raises(ApprovalMismatch):
        harness.approvals.grant_for(
            harness.approvals.get(approval.approval_id),
            proposal=substituted,
            allowed_tools=frozenset({"erp.create_purchase_order"}),
        )


def test_rewording_the_reasoning_does_not_invalidate_an_approval(harness, dana_session):
    """The digest covers the summary and the action, not the prose explaining it."""
    original = _proposal()
    reworded = _proposal()
    reworded.reasoning = "Completely different wording, identical plan."

    assert original.digest() == reworded.digest()


def test_only_the_person_asked_can_decide(harness, dana_session):
    approval = harness.approvals.request(
        dana_session, proposal=_proposal(), approver_id="u-101", reason="test"
    )
    with pytest.raises(HarmonyError, match="was asked of"):
        harness.approvals.decide(
            dana_session,
            approval_id=approval.approval_id,
            approve=True,
            decided_by="u-303",
        )


def test_an_approval_cannot_be_decided_twice(harness, dana_session):
    approval = harness.approvals.request(
        dana_session, proposal=_proposal(), approver_id="u-101", reason="test"
    )
    harness.approvals.decide(
        dana_session, approval_id=approval.approval_id, approve=True, decided_by="u-101"
    )
    with pytest.raises(HarmonyError, match="already"):
        harness.approvals.decide(
            dana_session, approval_id=approval.approval_id, approve=False, decided_by="u-101"
        )


# --- escalation ----------------------------------------------------------------


def test_unanswered_and_approver_away_routes_to_the_backup(harness, orchestrator, shortfall_item):
    """The brief's rule, end to end."""
    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    original = harness.approvals.for_run(run.run_id)[0]
    assert original.requested_of == "u-101"

    harness.advance_clock(harness.clock.end_of_day() + _dt.timedelta(minutes=1))
    Worker(harness).drain()

    assert harness.approvals.get(original.approval_id).state is ApprovalState.SUPERSEDED
    successor = next(
        a for a in harness.approvals.for_run(run.run_id) if a.state is ApprovalState.PENDING
    )
    assert successor.requested_of == "u-102"  # Marcus, Dana's designated backup
    assert successor.proposal_digest == original.proposal_digest


def test_escalation_checks_the_day_after_the_deadline_not_after_now(
    harness, orchestrator, shortfall_item
):
    """Regression.

    The deadline is end of day, so the escalation task necessarily fires after
    midnight. Asking "is the approver out tomorrow?" relative to *now* skips the very
    day the rule is about — Dana is out on the 3rd, and by the time the task runs it
    is already the 3rd. The check is anchored to the deadline instead.
    """
    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    approval = harness.approvals.for_run(run.run_id)[0]

    harness.advance_clock(harness.clock.end_of_day() + _dt.timedelta(minutes=1))
    assert harness.clock.today() == _dt.date(2026, 9, 3)

    Worker(harness).drain()

    escalation = next(
        e
        for e in harness.audit_log.for_run(run.run_id)
        if e.event_type.value == "approval.escalated"
    )
    assert escalation.payload["checked_day"] == "2026-09-03"
    assert escalation.payload["to_approver"] == "u-102"


def test_an_available_approver_keeps_the_request(harness, dana_session):
    """Being busy is not being absent. Marcus is in on the 3rd, so an approval that
    sits with him stays with him."""
    approval = harness.approvals.request(
        dana_session, proposal=_proposal(), approver_id="u-102", reason="test"
    )
    harness.advance_clock(harness.clock.end_of_day() + _dt.timedelta(minutes=1))
    Worker(harness).drain()

    reloaded = harness.approvals.get(approval.approval_id)
    assert reloaded.state is ApprovalState.PENDING
    assert reloaded.requested_of == "u-102"


def test_an_answered_approval_is_not_escalated(harness, orchestrator, shortfall_item):
    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    approval = harness.approvals.for_run(run.run_id)[0]
    orchestrator.approve(approval.approval_id, decided_by="u-101")

    harness.advance_clock(harness.clock.end_of_day() + _dt.timedelta(minutes=1))
    Worker(harness).drain()

    assert not any(
        e.event_type.value == "approval.escalated"
        for e in harness.audit_log.for_run(run.run_id)
    )


def test_the_availability_check_runs_as_a_narrow_system_identity(
    harness, orchestrator, shortfall_item
):
    """Reading a colleague's diary is not something the requester needs rights for."""
    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    harness.advance_clock(harness.clock.end_of_day() + _dt.timedelta(minutes=1))
    Worker(harness).drain()

    escalation = next(
        e
        for e in harness.audit_log.for_run(run.run_id)
        if e.event_type.value == "approval.escalated"
    )
    assert escalation.actor_kind == "system"
    assert escalation.actor_id == harness.deployment.system_principal_id


# --- rejection -----------------------------------------------------------------


def test_rejecting_executes_nothing(harness, orchestrator, shortfall_item):
    from northfield.systems import erp

    before = erp.list_purchase_orders(harness.store, part_id="P-4471")
    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    approval = harness.approvals.for_run(run.run_id)[0]

    run = orchestrator.reject(approval.approval_id, decided_by="u-101", note="Not now")

    assert run.state is RunState.REJECTED
    assert erp.list_purchase_orders(harness.store, part_id="P-4471") == before
