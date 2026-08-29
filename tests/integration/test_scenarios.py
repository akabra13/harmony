"""The two scenarios, end to end.

These assert the brief's requirements directly rather than testing a unit. If one
fails, something the take-home actually asked for has stopped working.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from harmony.runtime.run import RunState, TriggerKind
from harmony.schedule.worker import Worker
from harmony.workflow.models import InstanceStatus
from northfield.systems import erp, quality

pytestmark = pytest.mark.integration


# --- Scenario A ----------------------------------------------------------------


def test_scenario_a_end_to_end(harness, orchestrator, shortfall_item):
    """Requirements 1-6: detect unprompted, gather, plan, gate, execute, follow up."""
    # 1. Detected without being asked.
    item = shortfall_item()
    assert item.detector_id == "material_shortfall"
    assert item.facts["projection"]["shortfall_qty"] == 120

    # 2. Context came from three systems, scoped to Dana.
    run = orchestrator.run_for_item(item, harness.profiles.for_user("u-101"))
    slices = {
        e.payload["system"]
        for e in harness.audit_log.for_run(run.run_id)
        if e.event_type.value == "context.slice_fetched"
    }
    assert slices == {"erp", "mail", "calendar"}

    # 3. Planned into the declared workflow, not a free-form sequence.
    proposal = harness.proposals.get(run.proposal_id)
    assert proposal.action.workflow == "po_reroute"
    assert proposal.action.version == 3

    # 4. Gated, and stopped for a human.
    assert run.state is RunState.AWAITING_APPROVAL
    approval = harness.approvals.for_run(run.run_id)[0]
    assert approval.requested_of == "u-101"

    # Nothing has been written yet.
    assert erp.get_purchase_order(harness.store, "PO-77812")["status"] == "open"

    # 5. Approved, then executed in the declared order.
    run = orchestrator.approve(approval.approval_id, decided_by="u-101")
    assert run.state is RunState.COMPLETED

    instance = harness.engine.instances_for_run(run.run_id)[0]
    definition = harness.workflows.get("po_reroute", 3)
    assert instance.status is InstanceStatus.COMPLETED
    assert list(instance.step_results) == [s.id for s in definition.steps]

    replacement = next(
        p
        for p in erp.list_purchase_orders(harness.store, part_id="P-4471")
        if p["replaces_po"] == "PO-77812"
    )
    assert replacement["supplier_id"] == "S-Z"
    assert erp.get_purchase_order(harness.store, "PO-77812")["status"] == "cancelled"
    assert quality.notifications_about(harness.store, "4812")

    # 6. A follow-up is scheduled, and it survives a restart because it is a row.
    follow_up = next(t for t in harness.tasks.pending() if t.kind == "detector.run")
    assert follow_up.payload["detector"] == "po_arrival_check"
    assert follow_up.payload["po_id"] == replacement["po_id"]


def test_the_follow_up_fires_and_re_enters_the_loop(harness, orchestrator, shortfall_item):
    """Requirement 6: re-check on the promised date, and run again if still missing."""
    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    approval = harness.approvals.for_run(run.run_id)[0]
    orchestrator.approve(approval.approval_id, decided_by="u-101")

    follow_up = next(t for t in harness.tasks.pending() if t.kind == "detector.run")
    harness.advance_clock(follow_up.fire_at)
    Worker(harness).drain()

    follow_up_runs = [
        r for r in harness.runs.recent(20) if r.trigger is TriggerKind.FOLLOW_UP
    ]
    assert follow_up_runs, "the scheduled check did not open a run"
    assert follow_up_runs[0].parent_run_id == run.run_id

    proposal = harness.proposals.get(follow_up_runs[0].proposal_id)
    assert proposal is not None, "the follow-up reached the planner"


def test_the_follow_up_stays_quiet_when_the_goods_arrived(
    harness, orchestrator, shortfall_item, open_grant
):
    """The other branch. An agent that alerts either way is not checking anything."""
    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    approval = harness.approvals.for_run(run.run_id)[0]
    orchestrator.approve(approval.approval_id, decided_by="u-101")

    follow_up = next(t for t in harness.tasks.pending() if t.kind == "detector.run")
    replacement_id = follow_up.payload["po_id"]

    # Goods-in books the delivery before the check runs.
    from harmony.tools.base import ToolCall

    session = harness.user_session(
        "u-101",
        run_id="RUN-receipt",
        profile_scopes=harness.profiles.for_user("u-101").scope_set(),
        purpose="test",
    )
    harness.invoker.invoke(
        session,
        ToolCall(
            tool="erp.record_goods_receipt",
            params={"po_id": replacement_id, "qty": 400},
            step_id="r1",
        ),
        grant=open_grant("erp.record_goods_receipt"),
    )

    harness.advance_clock(follow_up.fire_at)
    Worker(harness).drain()

    assert not [r for r in harness.runs.recent(20) if r.trigger is TriggerKind.FOLLOW_UP]


# --- Scenario B ----------------------------------------------------------------


def test_scenario_b_end_to_end(harness, orchestrator):
    """A different person, a different system, and the free-form path."""
    results = orchestrator.detect("u-202")
    assert len(results) == 1
    item = results[0].item
    assert item.detector_id == "lot_hold_allocation_risk"
    assert item.facts["coverage_available"] is True
    assert [a["lot_id"] for a in item.facts["alternative_lots"]] == ["L-2101"]

    run = orchestrator.run_for_item(item, harness.profiles.for_user("u-202"))
    proposal = harness.proposals.get(run.proposal_id)

    # No declared workflow: the model assembled the sequence itself.
    assert proposal.action.kind == "tools"
    assert [c.tool for c in proposal.action.calls] == [
        "quality.reallocate_lot",
        "production.notify_supervisor",
    ]

    approval = harness.approvals.for_run(run.run_id)[0]
    assert approval.requested_of == "u-202"
    run = orchestrator.approve(approval.approval_id, decided_by="u-202")
    assert run.state is RunState.COMPLETED

    assert quality.get_lot(harness.store, "L-2093")["allocated_to"] == []
    assert quality.get_lot(harness.store, "L-2101")["allocated_to"] == ["4820"]
    assert quality.get_lot(harness.store, "L-2093")["status"] == "hold"
    assert quality.notifications_about(harness.store, "4820")


def test_the_quality_manager_cannot_raise_a_purchase_order(harness):
    """Priya can flag a shortage. She cannot buy, and neither can her agent."""
    priya = harness.directory.get("u-202")
    profile = harness.profiles.for_user("u-202")

    assert "erp:po:create" not in priya.scopes
    assert "erp:po:create" not in profile.scopes
    assert "purchasing:shortage:flag" in priya.scopes


def test_scenario_b_needed_no_new_gate_rule(harness):
    """The brief asks whether Scenario B forced changes to the planner, gate or
    audit layer. It did not, and this asserts the gate half of that claim."""
    assert set(harness.gate.rule_ids) == {
        "scope",
        "human_approval_for_writes",
        "po_value_threshold",
        "approved_supplier",
    }


# --- detector precision --------------------------------------------------------


def test_the_detector_ignores_orders_that_start_after_supply_arrives(harness, orchestrator):
    """Production order 4835 also consumes P-4471 and starts on 2026-09-25, long
    after the replacement would land. A detector that flagged every order consuming
    a short part would raise it."""
    titles = [r.item.title for r in orchestrator.detect("u-101")]
    assert not any("4835" in t for t in titles)


def test_the_detector_ignores_a_healthy_component_of_an_at_risk_order(
    harness, orchestrator
):
    """4812 also consumes P-2218, which is comfortably covered."""
    titles = [r.item.title for r in orchestrator.detect("u-101")]
    assert any("P-4471" in t for t in titles)
    assert not any("P-2218" in t for t in titles)


def test_a_shipping_update_that_revises_nothing_raises_no_alarm(harness, orchestrator):
    """M-003 concerns PO-77790, reads like a delay notice, and reports none."""
    titles = [r.item.title for r in orchestrator.detect("u-101")]
    assert not any("P-2218" in t or "77790" in t for t in titles)


def test_the_delay_email_is_what_creates_the_problem(harness, orchestrator):
    """Without the extracted date, PO-77812 arrives 09-04 and there is no shortfall
    at all. This is the load-bearing case for having a model in the loop."""
    harness.store.execute("DELETE FROM messages WHERE message_id = 'M-001'")
    titles = [r.item.title for r in orchestrator.detect("u-101")]
    assert not any("4812" in t for t in titles)


# --- the audit narrative -------------------------------------------------------


def test_a_run_can_be_reconstructed_from_the_ledger_alone(
    harness, orchestrator, shortfall_item
):
    """Requirement 7, checked against the renderer that reads only audit_events."""
    from harmony.audit.explain import RunExplainer

    run = orchestrator.run_for_item(shortfall_item(), harness.profiles.for_user("u-101"))
    approval = harness.approvals.for_run(run.run_id)[0]
    orchestrator.approve(approval.approval_id, decided_by="u-101", note="Go ahead")

    narrative = RunExplainer(harness.audit_log).explain(run.run_id)

    for expected in [
        "What the agent saw",
        "What the agent concluded",
        "What the agent was allowed to do",
        "Who approved what",
        "What actually happened in each system",
        "P-4471",
        "po_reroute",
        "u-101",
        "Audit chain verified",
    ]:
        assert expected in narrative, f"the narrative never mentions {expected!r}"
