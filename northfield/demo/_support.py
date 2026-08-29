"""Shared plumbing for the scripted demos.

The demos are narration over the real thing. They call the same orchestrator the
CLI does, against the same harness, with the same gate — there is no demo-only code
path, and nothing here writes to a system of record except through a tool. What
these helpers add is a fresh database per run and some formatting.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from harmony.llm.replay import build_client
from harmony.runtime.harness import Harness

START_AT = "2026-09-02T08:00:00"


def fresh_harness(
    db_path: str | None = None, *, start_at: str = START_AT, llm=None
) -> Harness:
    """Build a harness against a brand-new database.

    Each demo starts from the same known position so that its narrative holds and
    so a reviewer running it twice sees the same thing.
    """
    from northfield import DEPLOYMENT

    path = Path(db_path or os.path.join(tempfile.mkdtemp(prefix="harmony-demo-"), "demo.db"))
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists():
            candidate.unlink()

    return Harness.build(
        DEPLOYMENT, db_path=path, start_at=start_at, llm=llm or build_client(), seed=True
    )


# --- narration -----------------------------------------------------------------


def act(console: Console, number: str, title: str, subtitle: str = "") -> None:
    """A numbered beat in the story."""
    console.print()
    console.rule(f"[bold cyan]{number}[/bold cyan]  {title}", style="cyan")
    if subtitle:
        console.print(f"[dim]{subtitle}[/dim]")
    console.print()


def note(console: Console, text: str) -> None:
    console.print(f"[dim]›[/dim] {text}")


def emphasis(console: Console, text: str) -> None:
    console.print(f"[bold yellow]›[/bold yellow] [yellow]{text}[/yellow]")


def rows(console: Console, title: str, columns: list[str], data: list[list[Any]]) -> None:
    table = Table(*columns, title=title, title_justify="left", title_style="bold")
    for row in data:
        table.add_row(*[str(cell) for cell in row])
    console.print(table)


def approval_card(console: Console, harness: Harness, approval) -> None:
    """The moment the brief describes: what the manager is actually shown."""
    proposal = harness.proposals.get(approval.proposal_id)
    approver = harness.directory.try_get(approval.requested_of)
    console.print(
        Panel(
            f'[bold]"{proposal.summary}"[/bold]\n\n'
            f"[dim]reasoning[/dim]   {proposal.reasoning}\n\n"
            f"[dim]action[/dim]      {proposal.action.describe()}\n"
            f"[dim]asked of[/dim]    {approver.label if approver else approval.requested_of}\n"
            f"[dim]because[/dim]     {approval.reason}\n"
            f"[dim]expires[/dim]     {approval.expires_at:%Y-%m-%d %H:%M}\n"
            f"[dim]bound to[/dim]    proposal digest {approval.proposal_digest[:12]}"
            + (
                "\n\n[dim]alternatives considered[/dim]\n  · "
                + "\n  · ".join(proposal.alternatives_considered)
                if proposal.alternatives_considered
                else ""
            ),
            title=f"[bold yellow]APPROVAL REQUESTED  ·  {approval.approval_id}[/bold yellow]",
            border_style="yellow",
        )
    )


def workflow_table(console: Console, harness: Harness, run_id: str) -> None:
    """What the workflow actually did, step by step."""
    for instance in harness.engine.instances_for_run(run_id):
        definition = harness.workflows.get(
            instance.definition_name, instance.definition_version
        )
        table = Table(
            "#",
            "step",
            "kind",
            "result",
            title=f"{definition.key}  ·  {instance.status.value}",
            title_justify="left",
            title_style="bold",
        )
        for index, step in enumerate(definition.steps, 1):
            result = instance.step_results.get(step.id)
            table.add_row(
                str(index),
                step.id,
                step.kind.value,
                _summarise_output(result["output"]) if result else "[dim]not reached[/dim]",
            )
        console.print(table)

        if instance.compensation_log:
            table = Table("step", "outcome", "tool", title="rollback", title_style="bold red")
            for entry in instance.compensation_log:
                table.add_row(
                    entry["step_id"], entry["outcome"], entry.get("tool", "—")
                )
            console.print(table)


def _summarise_output(output: dict[str, Any]) -> str:
    """One readable line per step result."""
    interesting = ("po_id", "supplier_id", "lot_id", "task_id", "notification_id", "action")
    parts = [f"{k}={output[k]}" for k in interesting if k in output]
    if "supplier_ids" in output:
        parts.append(f"candidates={output['supplier_ids']}")
    if "justification" in output:
        parts.append(f'"{output["justification"][:70]}…"')
    if "subject" in output:
        parts.append(f'subject="{output["subject"]}"')
    return "  ".join(parts) or "[dim]ok[/dim]"


def audit_summary(console: Console, harness: Harness, run_id: str) -> None:
    """The count of what was recorded, as a pointer to the full narrative."""
    events = harness.audit_log.for_run(run_id)
    by_type: dict[str, int] = {}
    for event in events:
        by_type[event.event_type.value] = by_type.get(event.event_type.value, 0) + 1

    ok, _ = harness.audit_log.verify_chain()
    console.print(
        Panel(
            f"{len(events)} events recorded across {len(by_type)} event types.\n"
            f"hash chain: {'[green]verified[/green]' if ok else '[red]BROKEN[/red]'}\n\n"
            f"[dim]Read the full narrative with:[/dim]\n"
            f"  harmony audit explain {run_id}",
            title="audit",
            border_style="blue",
        )
    )
