"""The HTTP approval surface.

What these tests are really checking is that the second front end is *thin* — that
approving over HTTP does exactly what approving over the CLI does, and that the
authorisation rules hold because they live below both rather than in either.

The interesting cases are the refusals. A route that let one person answer another
person's approval, or that let a decision through after the plan changed, would be a
real hole and neither is visible from the happy path.
"""

from __future__ import annotations

import pytest

from harmony.runtime.run import RunState
from northfield.systems import erp

pytestmark = pytest.mark.integration


@pytest.fixture
def client(harness):
    from fastapi.testclient import TestClient

    from harmony.http.app import create_app

    return TestClient(create_app(harness))


@pytest.fixture
def pending(harness, orchestrator, shortfall_item):
    """A run parked at AWAITING_APPROVAL, exactly as Scenario A leaves it."""
    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    return run, harness.approvals.for_run(run.run_id)[0]


DANA = {"X-Harmony-User": "u-101"}
MARCUS = {"X-Harmony-User": "u-102"}


# --- authentication ------------------------------------------------------------


def test_an_unidentified_caller_is_refused(client):
    assert client.get("/approvals").status_code == 401


def test_an_unknown_user_is_refused(client):
    response = client.get("/approvals", headers={"X-Harmony-User": "u-999"})
    assert response.status_code == 401


def test_health_needs_no_identity(client):
    """So a load balancer can reach it."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["audit_chain_intact"] is True


# --- reading -------------------------------------------------------------------


def test_the_inbox_shows_only_approvals_addressed_to_the_caller(client, pending):
    _, approval = pending

    mine = client.get("/approvals", headers=DANA).json()
    assert [a["approval_id"] for a in mine] == [approval.approval_id]

    # Marcus is Dana's backup, but nothing has escalated to him yet.
    assert client.get("/approvals", headers=MARCUS).json() == []


def test_the_detail_view_carries_what_a_reviewer_needs_to_decide(client, pending):
    _, approval = pending
    body = client.get(f"/approvals/{approval.approval_id}", headers=DANA).json()

    assert "4812" in body["summary"]
    assert body["action"] == "workflow po_reroute@v3"
    assert body["alternatives_considered"], "a reviewer should see what was set aside"
    assert body["proposal_digest"] == approval.proposal_digest


def test_one_person_cannot_read_another_persons_approval(client, pending):
    _, approval = pending
    response = client.get(f"/approvals/{approval.approval_id}", headers=MARCUS)
    assert response.status_code == 403


def test_the_audit_narrative_is_served(client, pending):
    run, _ = pending
    body = client.get(f"/runs/{run.run_id}/audit", headers=DANA).json()

    assert "What the agent saw" in body["narrative"]
    assert "Who approved what" in body["narrative"]


# --- deciding ------------------------------------------------------------------


def test_approving_over_http_executes_the_plan(client, harness, pending):
    """The same outcome the CLI produces, through a different door."""
    run, approval = pending
    before = erp.get_purchase_order(harness.store, "PO-77812")["status"]
    assert before == "open"

    response = client.post(
        f"/approvals/{approval.approval_id}/approve",
        json={"note": "Go ahead."},
        headers=DANA,
    )

    assert response.status_code == 200
    assert response.json()["state"] == RunState.COMPLETED.value
    assert erp.get_purchase_order(harness.store, "PO-77812")["status"] == "cancelled"
    assert any(
        po["replaces_po"] == "PO-77812"
        for po in erp.list_purchase_orders(harness.store, part_id="P-4471")
    )


def test_rejecting_over_http_writes_nothing(client, harness, pending):
    _, approval = pending
    before = erp.list_purchase_orders(harness.store, part_id="P-4471")

    response = client.post(
        f"/approvals/{approval.approval_id}/reject",
        json={"note": "Not this week."},
        headers=MARCUS if False else DANA,
    )

    assert response.json()["state"] == RunState.REJECTED.value
    assert erp.list_purchase_orders(harness.store, part_id="P-4471") == before


def test_one_person_cannot_decide_anothers_approval(client, harness, pending):
    """The rule that would be a real hole without it.

    Enforced in ApprovalService rather than in the route, so the CLI gets it too —
    which is the point of keeping the front end thin.
    """
    _, approval = pending
    response = client.post(
        f"/approvals/{approval.approval_id}/approve", json={}, headers=MARCUS
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "harmony_error"
    assert erp.get_purchase_order(harness.store, "PO-77812")["status"] == "open"


def test_an_approval_cannot_be_answered_twice(client, pending):
    _, approval = pending
    first = client.post(f"/approvals/{approval.approval_id}/approve", json={}, headers=DANA)
    second = client.post(f"/approvals/{approval.approval_id}/approve", json={}, headers=DANA)

    assert first.status_code == 200
    assert second.status_code == 409


def test_deciding_a_missing_approval_is_a_404(client):
    response = client.post("/approvals/APR-nope/approve", json={}, headers=DANA)
    assert response.status_code == 404


def test_an_escalated_approval_moves_to_the_backups_inbox(client, harness, pending):
    """End to end over HTTP: Dana does not answer, she is out tomorrow, and the
    request appears in Marcus's inbox instead — bound to the same plan."""
    import datetime as _dt

    from harmony.schedule.worker import Worker

    _, original = pending
    harness.advance_clock(harness.clock.end_of_day() + _dt.timedelta(minutes=1))
    Worker(harness).drain()

    assert client.get("/approvals", headers=DANA).json() == []

    inbox = client.get("/approvals", headers=MARCUS).json()
    assert len(inbox) == 1
    assert inbox[0]["escalated"] is True
    assert inbox[0]["originally_for"] == "u-101"

    detail = client.get(f"/approvals/{inbox[0]['approval_id']}", headers=MARCUS).json()
    assert detail["proposal_digest"] == original.proposal_digest, (
        "the backup must be answering the same question, not a re-planned one"
    )
