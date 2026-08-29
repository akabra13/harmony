"""Scenario A — the purchase-order reroute.

    "Part X will likely cause production order 4812 to miss its scheduled start.
     Supplier Y said the shipment is delayed until Tuesday. I can move the PO to
     Supplier Z and notify production. Want me to proceed?"

Nothing below constructs that sentence. The detector finds the shortfall by
arithmetic, the planner writes the sentence, the gate decides a human must agree,
and this file narrates what happens. Every state change goes through the same
orchestrator the CLI uses.
"""

from __future__ import annotations

import datetime as _dt

from rich.console import Console

from harmony.kernel.clock import parse_date
from harmony.runtime.orchestrator import Orchestrator
from northfield.demo._support import (
    act,
    approval_card,
    audit_summary,
    emphasis,
    fresh_harness,
    note,
    rows,
    workflow_table,
)
from northfield.systems import calendar, erp, mail, quality


def run_scenario_a(
    console: Console, *, db_path: str | None = None, auto_approve: bool = True, llm=None
):
    harness = fresh_harness(db_path, llm=llm)
    orchestrator = Orchestrator(harness)
    store = harness.store

    console.print()
    console.rule("[bold]SCENARIO A — a shortfall nobody reported[/bold]")

    # ------------------------------------------------------------------ act 1
    act(
        console,
        "1",
        "The position on Monday morning",
        f"Simulated time is {harness.clock.now():%A %d %B %Y, %H:%M}. Nobody has asked "
        "the agent anything.",
    )

    part = erp.get_part(store, "P-4471")
    order = erp.get_production_order(store, "4812")
    po = erp.get_purchase_order(store, "PO-77812")

    rows(
        console,
        "what the ERP says",
        ["", "record", "detail"],
        [
            [
                "part",
                part["part_id"],
                f"{part['description']} — {part['on_hand']} on hand, "
                f"{part['daily_usage']}/day  →  "
                f"{part['on_hand'] / part['daily_usage']:.0f} days of cover",
            ],
            [
                "production",
                order["prod_order_id"],
                f"{order['product']} on {order['line']}, starts "
                f"{order['scheduled_start']}, needs 120 of {part['part_id']}",
            ],
            [
                "purchase",
                po["po_id"],
                f"{po['qty']} units from {po['supplier_id']}, promised "
                f"{po['promised_date']}, {po['status']}",
            ],
        ],
    )
    note(
        console,
        "On the promised date of 09-04 there is no problem at all: the goods arrive "
        "three days before production starts.",
    )

    inbox, withheld = mail.inbox_for(store, "dana.whitfield@northfield-mfg.example")
    rows(
        console,
        f"Dana's inbox ({len(inbox)} readable, {withheld} withheld)",
        ["message", "from", "subject"],
        [[m["message_id"], m["from_addr"].split("@")[0], m["subject"]] for m in inbox],
    )
    emphasis(
        console,
        "The problem exists only in M-001, in prose: \"Revised ship date is Monday 9/7, "
        "which puts it on your dock Tuesday 9/8.\"",
    )

    # ------------------------------------------------------------------ act 2
    act(
        console,
        "2",
        "Detection — unprompted",
        "The mail provider extracts a date from the email; the detector does arithmetic "
        "on it. No model judgment touches the decision.",
    )

    results = [r for r in orchestrator.detect("u-101") if "4812" in r.item.title]
    if not results:
        console.print("[red]detector found nothing — the demo cannot continue[/red]")
        return None
    item = results[0].item
    projection = item.facts["projection"]

    rows(
        console,
        "the projection, computed in code",
        ["term", "value"],
        [
            ["on hand today", projection["on_hand_today"]],
            ["daily usage", projection["daily_usage"]],
            ["days until 4812 starts", projection["days_until_start"]],
            ["consumed before start", projection["baseline_consumption_before_start"]],
            ["arriving in time", f"{projection['incoming_qty_in_time']} units"],
            [
                "arriving too late",
                ", ".join(
                    f"{p['po_id']} on {p['effective_arrival']}"
                    + (" (revised by supplier)" if p["revised_by_supplier"] else "")
                    for p in projection["arriving_after_start"]
                ),
            ],
            ["projected on hand at start", projection["projected_on_hand"]],
            ["required by 4812", projection["required_qty"]],
            ["[bold red]shortfall[/bold red]", f"[bold red]{projection['shortfall_qty']}[/bold red]"],
        ],
    )

    rows(
        console,
        "evidence carried into the alert",
        ["source", "ref", "detail", "verbatim"],
        [[e.source, e.ref, e.detail, e.quote or "—"] for e in item.evidence],
    )
    note(
        console,
        "The supplier's own sentence travels with the finding, so a human can check "
        "the extraction rather than trust it.",
    )

    # ------------------------------------------------------------------ act 3
    act(
        console,
        "3",
        "Plan, then gate",
        "The planner decides a reroute is warranted and supplies its parameters. The "
        "gate then decides, in code, what is permitted.",
    )

    profile = harness.profiles.for_user("u-101")
    agent_run = orchestrator.run_for_item(item, profile)

    gate_events = [
        e
        for e in harness.audit_log.for_run(agent_run.run_id)
        if e.event_type.value == "gate.rule_evaluated"
    ]
    rows(
        console,
        "every rule, and what it decided",
        ["rule", "verdict", "reason"],
        [
            [e.payload["rule_id"], e.payload["verdict"], e.payload["reason"]]
            for e in gate_events
        ],
    )
    note(
        console,
        "po_value_threshold cannot know the final price — the workflow has not chosen a "
        "supplier yet — so it bounds the order at the most expensive qualified supplier. "
        "Erring upward is the safe direction for a spending limit.",
    )

    approvals = harness.approvals.for_run(agent_run.run_id)
    if not approvals:
        console.print(f"[red]expected an approval; run is {agent_run.state.value}[/red]")
        return agent_run

    approval = approvals[0]
    console.print()
    approval_card(console, harness, approval)

    # ------------------------------------------------------------------ act 4
    act(
        console,
        "4",
        "Dana is out tomorrow",
        "The escalation rule is already armed: this approval carries an end-of-day "
        "deadline and a scheduled check behind it.",
    )
    tomorrow = harness.clock.today() + _dt.timedelta(days=1)
    events = calendar.events_for(store, "u-101", day=tomorrow)
    rows(
        console,
        f"Dana's calendar for {tomorrow:%A %d %B}",
        ["event", "title", "out of office"],
        [[e["event_id"], e["title"], "yes" if e["out_of_office"] else "no"] for e in events],
    )
    pending = [t for t in harness.tasks.pending() if t.kind == "approval.escalate"]
    note(
        console,
        f"{len(pending)} escalation check scheduled for "
        f"{pending[0].fire_at:%Y-%m-%d %H:%M} if she has not answered by then. "
        "The failure suite runs that branch; here, she answers.",
    )

    # ------------------------------------------------------------------ act 5
    act(console, "5", "The decision")

    if not auto_approve:
        answer = console.input("[bold yellow]Approve? [y/N] [/bold yellow]").strip().lower()
        if answer not in ("y", "yes"):
            orchestrator.reject(
                approval.approval_id, decided_by=approval.requested_of, note="declined at prompt"
            )
            console.print("[red]rejected — nothing was written[/red]")
            return harness.runs.get(agent_run.run_id)
    else:
        note(console, "(auto-approving; use --interactive to decide yourself)")

    agent_run = orchestrator.approve(
        approval.approval_id,
        decided_by=approval.requested_of,
        note="Approved — keep Line 2 running.",
    )

    # ------------------------------------------------------------------ act 6
    act(
        console,
        "6",
        "The declared workflow executes",
        "Eight steps, in the order purchasing specified. The model chose a supplier "
        "from a list code computed, and drafted a message. It wrote nothing.",
    )
    workflow_table(console, harness, agent_run.run_id)

    rows(
        console,
        "purchase orders for P-4471 afterwards",
        ["po", "supplier", "qty", "status", "promised", "replaces"],
        [
            [
                p["po_id"],
                p["supplier_id"],
                p["qty"],
                p["status"],
                p["promised_date"],
                p["replaces_po"] or "—",
            ]
            for p in erp.list_purchase_orders(store, part_id="P-4471")
        ],
    )

    notifications = quality.notifications_about(store, "4812")
    for notification in notifications:
        note(console, f"production notified: {notification['subject']}")

    # ------------------------------------------------------------------ act 7
    replacement = next(
        p for p in erp.list_purchase_orders(store, part_id="P-4471") if p["replaces_po"]
    )
    follow_up = next((t for t in harness.tasks.pending() if t.kind == "detector.run"), None)

    act(
        console,
        "7",
        "The follow-up",
        "The brief says 'schedule a check for Tuesday'. Tuesday was the *delayed* "
        "supplier's date. Because the reroute succeeded, the replacement is due "
        f"{replacement['promised_date']} — and that is when a check is worth running.",
    )
    if follow_up:
        rows(
            console,
            "scheduled work, surviving any restart",
            ["task", "kind", "fires", "detector", "about"],
            [
                [
                    follow_up.task_id,
                    follow_up.kind,
                    f"{follow_up.fire_at:%Y-%m-%d}",
                    follow_up.payload.get("detector"),
                    follow_up.payload.get("po_id"),
                ]
            ],
        )

        act(
            console,
            "8",
            f"Advancing the clock to {follow_up.fire_at:%A %d %B}",
            "The shipment has not been received. The check re-enters the loop from the top.",
        )
        harness.advance_clock(follow_up.fire_at)
        from harmony.schedule.worker import Worker

        fired = Worker(harness).drain()
        for task in fired:
            note(console, f"fired {task.kind} — {task.payload.get('detector', '')}")

        follow_up_runs = [
            r
            for r in harness.runs.recent(10)
            if r.trigger.value == "follow_up" and r.run_id != agent_run.run_id
        ]
        for r in follow_up_runs:
            proposal = harness.proposals.get(r.proposal_id) if r.proposal_id else None
            console.print()
            emphasis(console, f"follow-up run {r.run_id} — {r.state.value}")
            if proposal:
                note(console, f'it now says: "{proposal.summary}"')

    # ------------------------------------------------------------------ act 9
    act(console, "9", "The audit trail", "Everything above is reconstructible from the ledger.")
    audit_summary(console, harness, agent_run.run_id)
    return agent_run
