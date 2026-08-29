"""The gate: what it permits, what it refuses, and who it asks.

The brief names this as one of three areas that must be tested. What is worth
testing is not that the rules return the right enum — it is that the *composition*
holds: that a denial cannot be outvoted, that every rule's reasoning reaches the
ledger, and that a plan the gate refused leaves no trace in any system of record.
"""

from __future__ import annotations

import pytest

from harmony.gate.models import GateContext, RuleVerdict, Verdict
from harmony.gate.pipeline import Gate
from harmony.gate.rules import gate_rule
from harmony.plan.models import Proposal, ToolPlan, WorkflowInvocation
from harmony.runtime.run import RunState
from harmony.tools.base import ToolCall
from northfield.systems import erp


def _context(harness, session, proposal) -> GateContext:
    from harmony.runtime.orchestrator import Orchestrator

    return Orchestrator(harness)._gate_context(session, proposal)


def _reroute_proposal(**overrides) -> Proposal:
    params = {
        "at_risk_po_id": "PO-77812",
        "part_id": "P-4471",
        "production_order_id": "4812",
        "required_on_site_by": "2026-09-07",
        "qty": 400,
        "supervisor_id": "u-301",
        **overrides,
    }
    return Proposal(
        summary="Reroute PO-77812",
        reasoning="test",
        action=WorkflowInvocation(workflow="po_reroute", version=3, params=params),
    )


# --- scope ---------------------------------------------------------------------


def test_denies_when_the_principal_lacks_a_write_scope(harness):
    """A production planner can see the shortfall and cannot buy their way out."""
    profile = harness.profiles.for_user("u-303")
    session = harness.user_session(
        "u-303", run_id="RUN-x", profile_scopes=profile.scope_set(), purpose="test"
    )

    decision = harness.gate.evaluate(_context(harness, session, _reroute_proposal()))

    assert decision.verdict is Verdict.DENY
    scope_verdict = next(v for v in decision.rule_verdicts if v.rule_id == "scope")
    assert "erp:po:create" in scope_verdict.details["missing"]


def test_allows_when_every_required_scope_is_held(harness, dana_session):
    decision = harness.gate.evaluate(
        _context(harness, dana_session, _reroute_proposal())
    )
    scope_verdict = next(v for v in decision.rule_verdicts if v.rule_id == "scope")
    assert scope_verdict.verdict is Verdict.ALLOW


def test_a_denied_plan_writes_nothing(harness, orchestrator, shortfall_item):
    """The whole point of gating before executing."""
    before = erp.list_purchase_orders(harness.store, part_id="P-4471")

    item = shortfall_item("u-303")
    run = orchestrator.run_for_item(item, harness.profiles.for_user("u-303"))

    assert run.state is RunState.DENIED
    assert erp.list_purchase_orders(harness.store, part_id="P-4471") == before
    assert harness.approvals.for_run(run.run_id) == []


# --- human approval ------------------------------------------------------------


def test_any_write_requires_a_human(harness, dana_session):
    decision = harness.gate.evaluate(
        _context(harness, dana_session, _reroute_proposal())
    )
    assert decision.verdict is Verdict.REQUIRE_APPROVAL
    assert decision.approver_id == "u-101"


def test_a_read_only_plan_needs_no_approval(harness, dana_session):
    proposal = Proposal(
        summary="Look something up",
        reasoning="test",
        action=ToolPlan(
            calls=[
                ToolCall(
                    tool="erp.list_approved_suppliers_for_part",
                    params={"part_id": "P-4471"},
                    step_id="s0",
                )
            ]
        ),
    )
    decision = harness.gate.evaluate(_context(harness, dana_session, proposal))
    assert decision.verdict is Verdict.ALLOW


# --- value threshold -----------------------------------------------------------


def test_within_limit_stays_with_the_buyer(harness, dana_session):
    """400 units of P-4471 at the priciest qualified supplier is £18,600 — inside
    Dana's £25,000 limit, so no escalation."""
    decision = harness.gate.evaluate(
        _context(harness, dana_session, _reroute_proposal())
    )
    verdict = next(v for v in decision.rule_verdicts if v.rule_id == "po_value_threshold")
    assert verdict.verdict is Verdict.ALLOW
    assert verdict.details["value"] == pytest.approx(18600.0)
    assert decision.approver_id == "u-101"


def test_over_limit_escalates_to_the_manager(harness, dana_session):
    """40 servo drives at £940 is £37,600 — over the limit, so her director decides."""
    proposal = _reroute_proposal(
        at_risk_po_id="PO-77820",
        part_id="P-5540",
        production_order_id="4816",
        required_on_site_by="2026-09-08",
        qty=40,
    )
    decision = harness.gate.evaluate(_context(harness, dana_session, proposal))

    assert decision.verdict is Verdict.REQUIRE_APPROVAL
    assert decision.approver_id == "u-100"  # Grace, Dana's manager
    verdict = next(v for v in decision.rule_verdicts if v.rule_id == "po_value_threshold")
    assert verdict.details["value"] == pytest.approx(37600.0)


def test_the_value_bound_uses_the_priciest_qualified_supplier(harness, dana_session):
    """The gate runs before the workflow chooses, so it must bound from above.

    Meridian at £46.50 is dearer than Kestrel or Halstead, so that is the price the
    rule costs the order at. Erring downward would authorise spending nobody agreed
    to; erring upward only asks a more senior person.
    """
    decision = harness.gate.evaluate(
        _context(harness, dana_session, _reroute_proposal())
    )
    verdict = next(v for v in decision.rule_verdicts if v.rule_id == "po_value_threshold")
    assert verdict.details["priced_at"] == "S-Z"
    assert verdict.details["unit_price"] == pytest.approx(46.50)


# --- approved supplier ---------------------------------------------------------


def test_denies_an_order_to_a_supplier_not_qualified_for_the_part(harness, dana_session):
    """Apex Rapid Supply: approved vendor, cheapest price, next-day delivery, and
    not qualified for P-4471. The gate is what says no."""
    proposal = Proposal(
        summary="Order from Apex",
        reasoning="They are cheaper and faster",
        action=ToolPlan(
            calls=[
                ToolCall(
                    tool="erp.create_purchase_order",
                    params={
                        "part_id": "P-4471",
                        "supplier_id": "S-Q",
                        "qty": 400,
                        "need_by": "2026-09-07",
                    },
                    step_id="s0",
                )
            ]
        ),
    )
    decision = harness.gate.evaluate(_context(harness, dana_session, proposal))

    assert decision.verdict is Verdict.DENY
    verdict = next(v for v in decision.rule_verdicts if v.rule_id == "approved_supplier")
    assert verdict.details["violations"][0]["supplier_id"] == "S-Q"


# --- composition ---------------------------------------------------------------


def test_a_single_denial_outweighs_every_approval(harness, dana_session):
    """No amount of permissive verdicts can overturn one refusal."""
    gate = Gate(
        rules=[
            ("permissive_a", lambda ctx: RuleVerdict.allow("permissive_a", "fine")),
            ("refuses", lambda ctx: RuleVerdict.deny("refuses", "absolutely not")),
            ("permissive_b", lambda ctx: RuleVerdict.allow("permissive_b", "also fine")),
        ]
    )
    decision = gate.evaluate(_context(harness, dana_session, _reroute_proposal()))

    assert decision.verdict is Verdict.DENY
    assert decision.reasons == ["absolutely not"]


def test_every_rule_runs_even_after_one_denies(harness, dana_session):
    """Short-circuiting would be faster and would make the audit worse: a reviewer
    asking "would this have needed a director anyway?" deserves an answer."""
    profile = harness.profiles.for_user("u-303")
    session = harness.user_session(
        "u-303", run_id="RUN-y", profile_scopes=profile.scope_set(), purpose="test"
    )
    decision = harness.gate.evaluate(_context(harness, session, _reroute_proposal()))

    assert decision.verdict is Verdict.DENY
    assert {v.rule_id for v in decision.rule_verdicts} == set(harness.gate.rule_ids)


def test_a_rule_that_raises_is_treated_as_a_denial(harness, dana_session):
    """Failing open would make every future bug in a rule a silent authorisation."""

    def broken(ctx):
        raise RuntimeError("policy service unreachable")

    gate = Gate(rules=[("broken", broken)])
    decision = gate.evaluate(_context(harness, dana_session, _reroute_proposal()))

    assert decision.verdict is Verdict.DENY
    assert "policy service unreachable" in decision.reasons[0]


def test_the_most_senior_demanded_approver_wins(harness, dana_session):
    """Two rules, two approvers, one of whom manages the other."""
    gate = Gate(
        rules=[
            (
                "asks_dana",
                lambda ctx: RuleVerdict.approval("asks_dana", "buyer", approver_id="u-101"),
            ),
            (
                "asks_grace",
                lambda ctx: RuleVerdict.approval("asks_grace", "director", approver_id="u-100"),
            ),
        ]
    )
    decision = gate.evaluate(_context(harness, dana_session, _reroute_proposal()))

    assert decision.verdict is Verdict.REQUIRE_APPROVAL
    assert decision.approver_id == "u-100"


# --- auditing ------------------------------------------------------------------


def test_every_rule_verdict_reaches_the_ledger(harness, dana_session):
    """"Why did this need approval?" must be answerable from the log alone."""
    harness.gate.evaluate(_context(harness, dana_session, _reroute_proposal()))

    events = [
        e
        for e in harness.audit_log.for_run("RUN-test")
        if e.event_type.value == "gate.rule_evaluated"
    ]
    assert {e.payload["rule_id"] for e in events} == set(harness.gate.rule_ids)
    assert all(e.payload["reason"] for e in events)


def test_a_registered_rule_is_picked_up_automatically(harness, dana_session):
    """Adding a policy means adding a rule. Nothing else."""

    @gate_rule("test_only_no_orders_on_wednesday")
    def no_wednesdays(ctx):
        return RuleVerdict.deny("test_only_no_orders_on_wednesday", "not on a Wednesday")

    try:
        decision = Gate().evaluate(_context(harness, dana_session, _reroute_proposal()))
        assert decision.verdict is Verdict.DENY
        assert "test_only_no_orders_on_wednesday" in {
            v.rule_id for v in decision.rule_verdicts
        }
    finally:
        from harmony.gate.rules import GATE_RULES

        GATE_RULES._entries.pop("test_only_no_orders_on_wednesday", None)
