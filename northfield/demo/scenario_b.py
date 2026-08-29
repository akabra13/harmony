"""Scenario B — a quality hold, handled by the free-form planner.

A different employee, a different system, a different set of permissions, and no
declared workflow. What is worth watching is how little of the harness changed to
make this work: a profile, a provider, a detector and three tools, all under
``northfield/``. The orchestrator, the planner, the gate and the audit layer are
byte-for-byte the same ones Scenario A ran through.

The permission boundary is the other thing to watch. Priya can reallocate a lot and
she can tell purchasing there is a shortage. She cannot raise a purchase order, and
neither can her agent — which is why the escalation path is a flag rather than an
order.
"""

from __future__ import annotations

from rich.console import Console

from harmony.runtime.orchestrator import Orchestrator
from northfield.demo._support import (
    act,
    approval_card,
    audit_summary,
    emphasis,
    fresh_harness,
    note,
    rows,
)
from northfield.systems import erp, quality


def run_scenario_b(console: Console, *, db_path: str | None = None, llm=None):
    harness = fresh_harness(db_path, llm=llm)
    orchestrator = Orchestrator(harness)
    store = harness.store

    console.print()
    console.rule("[bold]SCENARIO B — a lot on hold, three days out[/bold]")

    # ------------------------------------------------------------------ act 1
    act(
        console,
        "1",
        "A different person's agent",
        "Same harness, same loop. Everything that differs is configuration.",
    )

    priya = harness.directory.get("u-202")
    dana = harness.directory.get("u-101")
    profile = harness.profiles.for_user("u-202")

    rows(
        console,
        "two profiles, one loop",
        ["", "purchasing_manager", "quality_manager"],
        [
            ["person", dana.label, priya.label],
            ["detectors", "material_shortfall, po_arrival_check", "lot_hold_allocation_risk"],
            ["providers", "erp, mail, calendar", "quality, erp, mail"],
            ["workflows", "po_reroute", "[bold]none — free-form planning[/bold]"],
            [
                "can create a PO",
                "yes (erp:po:create)",
                "[bold red]no[/bold red]",
            ],
        ],
    )
    note(
        console,
        "Scenario B required no change to the orchestrator, the planner, the gate or "
        "the audit layer. Check with: git show --stat <scenario-b commit>",
    )

    # ------------------------------------------------------------------ act 2
    act(console, "2", "What quality is looking at")

    lots = quality.lots_for_part(store, "P-1188")
    rows(
        console,
        "lots of P-1188",
        ["lot", "qty", "status", "allocated to", "note"],
        [
            [
                lot["lot_id"],
                lot["qty"],
                lot["status"],
                ", ".join(lot["allocated_to"]) or "—",
                lot["hold_reason"] or "",
            ]
            for lot in lots
        ],
    )
    order = erp.get_production_order(store, "4820")
    note(
        console,
        f"{order['product']} ({order['prod_order_id']}) starts {order['scheduled_start']} "
        f"and needs 90 of P-1188, drawn from L-2093 — which is on hold.",
    )
    emphasis(
        console,
        "Three distractors sit in that table: a scrapped lot of the right part, a "
        "released lot already committed elsewhere, and a lot too small to cover alone.",
    )

    # ------------------------------------------------------------------ act 3
    act(console, "3", "Detection, then a plan the model assembled itself")

    results = orchestrator.detect("u-202")
    if not results:
        console.print("[red]detector found nothing[/red]")
        return None
    item = results[0].item

    rows(
        console,
        "what the detector found",
        ["field", "value"],
        [
            ["held lot", item.facts["held_lot"]["lot_id"]],
            ["hold reason", item.facts["held_lot"]["hold_reason"]],
            ["production order", item.facts["production_order"]["id"]],
            ["starts in", f"{item.facts['production_order']['days_until_start']} day(s)"],
            ["quantity required", item.facts["production_order"]["qty_required"]],
            [
                "alternative lots",
                ", ".join(
                    f"{alt['lot_id']} ({alt['qty']})" for alt in item.facts["alternative_lots"]
                )
                or "none",
            ],
            ["coverage available", item.facts["coverage_available"]],
        ],
    )
    note(
        console,
        "The detector reports whether a covering lot exists. Whether to use it, or "
        "escalate to purchasing instead, is the planner's judgment.",
    )

    agent_run = orchestrator.run_for_item(item, profile)
    proposal = harness.proposals.get(agent_run.proposal_id)

    rows(
        console,
        "the plan — assembled by the model, not declared",
        ["#", "tool", "why"],
        [
            [str(i), call.tool, call.rationale]
            for i, call in enumerate(proposal.action.calls, 1)
        ],
    )
    note(
        console,
        "No workflow governs this, so the model chose both the steps and their order. "
        "Every call is still checked against the catalog, scope-checked, gated and "
        "covered by an execution grant — it gets no resumption or compensation "
        "guarantees, which the executor's docstring is explicit about.",
    )

    # ------------------------------------------------------------------ act 4
    act(console, "4", "The same gate, unchanged")

    gate_events = [
        e
        for e in harness.audit_log.for_run(agent_run.run_id)
        if e.event_type.value == "gate.rule_evaluated"
    ]
    rows(
        console,
        "rules run against a quality plan",
        ["rule", "verdict", "reason"],
        [[e.payload["rule_id"], e.payload["verdict"], e.payload["reason"]] for e in gate_events],
    )
    note(
        console,
        "po_value_threshold and approved_supplier both ran and both had nothing to "
        "say. A rule that does not apply still records that it considered the plan.",
    )

    approvals = harness.approvals.for_run(agent_run.run_id)
    if not approvals:
        console.print(f"[red]expected an approval; run is {agent_run.state.value}[/red]")
        return agent_run
    approval = approvals[0]
    console.print()
    approval_card(console, harness, approval)

    # ------------------------------------------------------------------ act 5
    act(console, "5", "Approve, and execute")
    agent_run = orchestrator.approve(
        approval.approval_id, decided_by="u-202", note="L-2101 is good stock, go ahead."
    )
    console.print(f"run [bold]{agent_run.state.value}[/bold]")

    rows(
        console,
        "lots of P-1188 afterwards",
        ["lot", "qty", "status", "allocated to"],
        [
            [lot["lot_id"], lot["qty"], lot["status"], ", ".join(lot["allocated_to"]) or "—"]
            for lot in quality.lots_for_part(store, "P-1188")
        ],
    )
    for notification in quality.notifications_about(store, "4820"):
        note(console, f"production notified: {notification['subject']}")

    emphasis(
        console,
        "L-2093 stays on hold. The order moved to good stock, the build date did not "
        "change, and purchasing was never involved because it did not need to be.",
    )

    # ------------------------------------------------------------------ act 6
    act(console, "6", "The audit trail")
    audit_summary(console, harness, agent_run.run_id)
    return agent_run
