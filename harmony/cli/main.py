"""The command line: one binary, several entrypoints.

The seam that would let this become several services is the durable task queue, not
this file. ``harmony worker`` already runs independently of the commands that create
work for it, and a worker killed mid-task leaves the task in the queue for the next
one. Splitting them across processes is a deployment decision; nothing in the code
would change.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


def _configure_stdio() -> None:
    """Force UTF-8 output.

    Windows consoles still default to a legacy code page, and the demo's output is
    full of arrows, box drawing and the odd £ sign. Without this a reviewer on
    Windows gets a UnicodeEncodeError instead of a demo, which is a poor first
    impression of a harness whose whole pitch is that it fails legibly.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - already redirected
                pass


_configure_stdio()

from harmony.audit.explain import RunExplainer
from harmony.llm.replay import build_client
from harmony.runtime.discovery import load_deployment
from harmony.runtime.harness import Harness
from harmony.runtime.orchestrator import Orchestrator
from harmony.schedule.worker import Worker

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Harmony — an extendable agent harness for enterprise work.",
)
approvals_app = typer.Typer(no_args_is_help=True, help="Review and decide pending approvals.")
clock_app = typer.Typer(no_args_is_help=True, help="Inspect and advance simulated time.")
audit_app = typer.Typer(no_args_is_help=True, help="Read the audit ledger.")
catalog_app = typer.Typer(no_args_is_help=True, help="Inspect what is registered.")
demo_app = typer.Typer(no_args_is_help=True, help="Run the scripted scenarios.")

app.add_typer(approvals_app, name="approvals")
app.add_typer(clock_app, name="clock")
app.add_typer(audit_app, name="audit")
app.add_typer(catalog_app, name="catalog")
app.add_typer(demo_app, name="demo")

console = Console()

DEFAULT_DB = ".harmony/harmony.db"


def _harness(*, db: str | None = None, start_at: str | None = None, seed: bool = False) -> Harness:
    """Build the harness the CLI acts through.

    ``seed=False`` by default: the commands operate on an existing database, and
    only ``harmony init`` creates one. A CLI that silently re-seeded would quietly
    undo the state a demo had built up.
    """
    path = Path(db or os.environ.get("HARMONY_DB", DEFAULT_DB))
    if not seed and not path.exists():
        raise typer.BadParameter(
            f"no database at {path}. Run `harmony init` first, or pass --db.",
            param_hint="--db",
        )

    return Harness.build(
        load_deployment(),
        db_path=path,
        start_at=start_at,
        llm=build_client(),
        seed=seed,
    )


# --- setup ---------------------------------------------------------------------


@app.command()
def init(
    db: Optional[str] = typer.Option(None, help="Database path."),
    start_at: str = typer.Option("2026-09-02T08:00:00", help="Where to start the clock."),
    force: bool = typer.Option(False, "--force", help="Delete an existing database first."),
) -> None:
    """Create the database, apply migrations, and load Northfield's seed data."""
    path = Path(db or os.environ.get("HARMONY_DB", DEFAULT_DB))
    if force and path.exists():
        path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = path.with_name(path.name + suffix)
            if extra.exists():
                extra.unlink()

    harness = _harness(db=str(path), start_at=start_at, seed=True)
    console.print(
        Panel(
            f"[bold]{harness.deployment.name}[/bold]\n"
            f"database    {path}\n"
            f"clock       {harness.clock.now().isoformat()}\n"
            f"tools       {len(harness.tools.names())}\n"
            f"detectors   {len(harness.detectors)}\n"
            f"workflows   {', '.join(harness.workflows.keys())}\n"
            f"profiles    {', '.join(harness.profiles.ids())}",
            title="initialised",
            border_style="green",
        )
    )


@app.command()
def status(db: Optional[str] = typer.Option(None)) -> None:
    """Show the clock, open work, and pending approvals."""
    harness = _harness(db=db)
    console.print(f"[bold]clock[/bold]  {harness.clock.now().isoformat()}")

    runs = harness.runs.recent(10)
    table = Table("run", "state", "principal", "trigger", title="recent runs")
    for run in runs:
        table.add_row(run.run_id, run.state.value, run.principal_id, run.trigger.value)
    console.print(table)

    pending = harness.tasks.pending()
    table = Table("task", "kind", "fires", title="scheduled work")
    for task in pending:
        table.add_row(task.task_id, task.kind, task.fire_at.isoformat())
    console.print(table)

    open_approvals = harness.approvals.all_open()
    table = Table("approval", "of", "reason", title="awaiting a decision")
    for approval in open_approvals:
        table.add_row(approval.approval_id, approval.requested_of, approval.reason[:60])
    console.print(table)

    ok, broken = harness.audit_log.verify_chain()
    console.print(
        "[green]audit chain verified[/green]"
        if ok
        else f"[red]AUDIT CHAIN BROKEN at {broken}[/red]"
    )


# --- the loop ------------------------------------------------------------------


@app.command()
def detect(
    user: str = typer.Option(..., "--user", "-u", help="Whose agent should look."),
    detector: Optional[str] = typer.Option(None, help="Run only this detector."),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Run a user's detectors and report what they found, without acting."""
    harness = _harness(db=db)
    results = Orchestrator(harness).detect(user, detector_ids=[detector] if detector else None)

    if not results:
        console.print("[dim]nothing found[/dim]")
        return

    for result in results:
        colour = {"raised": "yellow", "suppressed": "dim", "superseded": "cyan"}[
            result.outcome.value
        ]
        console.print(
            Panel(
                f"{result.item.title}\n\n"
                f"[dim]severity[/dim]  {result.item.severity.value}\n"
                f"[dim]subjects[/dim]  {', '.join(str(s) for s in result.item.subjects)}\n"
                f"[dim]evidence[/dim]  "
                + "\n            ".join(
                    f"{e.source}:{e.ref} — {e.detail}" for e in result.item.evidence
                ),
                title=f"{result.outcome.value}  ·  {result.item.item_id}",
                border_style=colour,
            )
        )


@app.command()
def run(
    user: str = typer.Option(..., "--user", "-u"),
    detector: Optional[str] = typer.Option(None),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Detect, and take everything found through the loop to a decision."""
    harness = _harness(db=db)
    runs = Orchestrator(harness).detect_and_run(
        user, detector_ids=[detector] if detector else None
    )
    if not runs:
        console.print("[dim]nothing to act on[/dim]")
        return
    for agent_run in runs:
        console.print(f"{agent_run.run_id}  [bold]{agent_run.state.value}[/bold]")


@app.command()
def worker(
    once: bool = typer.Option(False, "--once", help="Run a single tick and stop."),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Drain due scheduled work.

    Under a simulated clock there is nothing to wait for: everything due at the
    current instant is run, and then the process exits. Advance the clock to make
    more work due.
    """
    harness = _harness(db=db)
    w = Worker(harness)
    ran = w.tick() if once else w.drain()
    if not ran:
        console.print("[dim]nothing due[/dim]")
        return
    for task in ran:
        console.print(f"fired [bold]{task.kind}[/bold]  {task.task_id}  {task.payload}")


# --- approvals -----------------------------------------------------------------


@approvals_app.command("list")
def approvals_list(
    user: Optional[str] = typer.Option(None, "--user", "-u"),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Show approvals waiting on a decision."""
    harness = _harness(db=db)
    items = harness.approvals.open_for(user) if user else harness.approvals.all_open()
    if not items:
        console.print("[dim]no approvals pending[/dim]")
        return

    table = Table("approval", "requested of", "expires", "reason")
    for approval in items:
        table.add_row(
            approval.approval_id,
            approval.requested_of,
            approval.expires_at.strftime("%Y-%m-%d %H:%M") if approval.expires_at else "—",
            approval.reason,
        )
    console.print(table)


@approvals_app.command("show")
def approvals_show(approval_id: str, db: Optional[str] = typer.Option(None)) -> None:
    """Show what a decision would authorise."""
    harness = _harness(db=db)
    approval = harness.approvals.get(approval_id)
    if approval is None:
        raise typer.BadParameter(f"no approval '{approval_id}'")
    proposal = harness.proposals.get(approval.proposal_id)
    if proposal is None:
        raise typer.BadParameter(f"approval '{approval_id}' has no stored proposal")

    approver = harness.directory.try_get(approval.requested_of)
    console.print(
        Panel(
            f"[bold]{proposal.summary}[/bold]\n\n"
            f"{proposal.reasoning}\n\n"
            f"[dim]action[/dim]      {proposal.action.describe()}\n"
            f"[dim]asked of[/dim]    {approver.label if approver else approval.requested_of}\n"
            f"[dim]because[/dim]     {approval.reason}\n"
            f"[dim]expires[/dim]     {approval.expires_at}\n"
            f"[dim]bound to[/dim]    proposal digest {approval.proposal_digest[:12]}\n"
            + (
                "\n[dim]alternatives considered[/dim]\n  "
                + "\n  ".join(proposal.alternatives_considered)
                if proposal.alternatives_considered
                else ""
            ),
            title=f"approval {approval_id}",
            border_style="yellow",
        )
    )


@approvals_app.command("approve")
def approvals_approve(
    approval_id: str,
    by: Optional[str] = typer.Option(None, "--by", help="Who is deciding."),
    note: str = typer.Option("", "--note"),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Approve, and execute what it authorises."""
    harness = _harness(db=db)
    approval = harness.approvals.get(approval_id)
    if approval is None:
        raise typer.BadParameter(f"no approval '{approval_id}'")

    agent_run = Orchestrator(harness).approve(
        approval_id, decided_by=by or approval.requested_of, note=note
    )
    console.print(f"{agent_run.run_id}  [bold]{agent_run.state.value}[/bold]")


@approvals_app.command("reject")
def approvals_reject(
    approval_id: str,
    by: Optional[str] = typer.Option(None, "--by"),
    note: str = typer.Option("", "--note"),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Reject. Nothing is executed."""
    harness = _harness(db=db)
    approval = harness.approvals.get(approval_id)
    if approval is None:
        raise typer.BadParameter(f"no approval '{approval_id}'")

    agent_run = Orchestrator(harness).reject(
        approval_id, decided_by=by or approval.requested_of, note=note
    )
    console.print(f"{agent_run.run_id}  [bold]{agent_run.state.value}[/bold]")


# --- clock ---------------------------------------------------------------------


@clock_app.command("show")
def clock_show(db: Optional[str] = typer.Option(None)) -> None:
    """Show simulated time and what is due."""
    harness = _harness(db=db)
    now = harness.clock.now()
    console.print(f"[bold]{now.isoformat()}[/bold]  ({now:%A})")
    due = harness.tasks.due(now)
    console.print(f"{len(due)} task(s) due now, {len(harness.tasks.pending())} pending")


@clock_app.command("advance")
def clock_advance(
    target: str = typer.Argument(..., help="ISO date or datetime to advance to."),
    drain: bool = typer.Option(True, "--drain/--no-drain", help="Run work that becomes due."),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Move simulated time forward, firing anything that becomes due."""
    harness = _harness(db=db)
    after = harness.advance_clock(target)
    console.print(f"clock → [bold]{after.isoformat()}[/bold] ({after:%A})")

    if drain:
        ran = Worker(harness).drain()
        for task in ran:
            console.print(f"  fired [bold]{task.kind}[/bold]  {task.payload}")
        if not ran:
            console.print("  [dim]nothing became due[/dim]")


# --- audit ---------------------------------------------------------------------


@audit_app.command("explain")
def audit_explain(
    run_id: str,
    fmt: str = typer.Option("text", "--format", help="text or md."),
    verbose: bool = typer.Option(False, "--verbose", help="Show every payload field."),
    out: Optional[Path] = typer.Option(None, "--out", help="Write to a file."),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Reconstruct a run from the audit log alone."""
    harness = _harness(db=db)
    explainer = RunExplainer(harness.audit_log)
    text = (
        explainer.explain_markdown(run_id, verbose=verbose)
        if fmt == "md"
        else explainer.explain(run_id, verbose=verbose)
    )
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(f"written to {out}")
    else:
        console.print(text, highlight=False)


@audit_app.command("verify")
def audit_verify(db: Optional[str] = typer.Option(None)) -> None:
    """Recompute the hash chain over the whole ledger."""
    harness = _harness(db=db)
    ok, broken = harness.audit_log.verify_chain()
    if ok:
        console.print("[green]chain intact[/green] — every entry hashes its predecessor")
    else:
        console.print(f"[red]chain broken at event {broken}[/red]")
        raise typer.Exit(1)


@audit_app.command("runs")
def audit_runs(db: Optional[str] = typer.Option(None)) -> None:
    """List runs that have audit history."""
    harness = _harness(db=db)
    for run_id in harness.audit_log.all_runs():
        console.print(run_id)


# --- catalogs ------------------------------------------------------------------


@catalog_app.command("tools")
def catalog_tools(db: Optional[str] = typer.Option(None)) -> None:
    """Every registered tool, its scopes, and how it is undone."""
    harness = _harness(db=db)
    table = Table("tool", "system", "writes", "scopes", "compensation")
    for spec in harness.tools.all():
        table.add_row(
            spec.name,
            spec.system,
            "yes" if spec.writes else "—",
            ", ".join(sorted(spec.scopes)) or "—",
            spec.compensation or ("irreversible" if spec.writes else "—"),
        )
    console.print(table)


@catalog_app.command("detectors")
def catalog_detectors(db: Optional[str] = typer.Option(None)) -> None:
    """Every registered detector."""
    harness = _harness(db=db)
    table = Table("detector", "systems", "targeted", "description")
    for spec in harness.detectors.all():
        table.add_row(
            spec.id,
            ", ".join(sorted(spec.systems)),
            "yes" if spec.targeted else "—",
            spec.description,
        )
    console.print(table)


@catalog_app.command("workflows")
def catalog_workflows(db: Optional[str] = typer.Option(None)) -> None:
    """Every loaded workflow, and the steps it will run."""
    harness = _harness(db=db)
    for definition in harness.workflows.all():
        table = Table("#", "step", "kind", "tool", "undone by", title=definition.key)
        for index, step in enumerate(definition.steps, 1):
            table.add_row(
                str(index),
                step.id,
                step.kind.value,
                step.tool or "—",
                step.compensation.tool
                if step.compensation
                else ("irreversible" if step.irreversible else "—"),
            )
        console.print(table)


@catalog_app.command("profiles")
def catalog_profiles(db: Optional[str] = typer.Option(None)) -> None:
    """Every agent profile and what it binds."""
    harness = _harness(db=db)
    for profile in harness.profiles.all():
        console.print(
            Panel(
                f"{profile.description}\n\n"
                f"[dim]assigned to[/dim]  {', '.join(profile.assigned_to)}\n"
                f"[dim]detectors[/dim]    {', '.join(profile.detectors)}\n"
                f"[dim]providers[/dim]    {', '.join(profile.providers)}\n"
                f"[dim]tools[/dim]        {', '.join(profile.tools)}\n"
                f"[dim]workflows[/dim]    {', '.join(profile.workflows) or '(free-form planning only)'}",
                title=profile.id,
            )
        )


# --- demos ---------------------------------------------------------------------


@demo_app.command("list")
def demo_list() -> None:
    """Show the scenarios this deployment ships."""
    deployment = load_deployment()
    table = Table("name", "description", title=f"{deployment.name} scenarios")
    for name, fn in deployment.demos.items():
        summary = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        table.add_row(name, summary)
    console.print(table)


@demo_app.command("run")
def demo_run(
    name: str = typer.Argument(..., help="Scenario name; see `harmony demo list`."),
    db: Optional[str] = typer.Option(None),
    interactive: bool = typer.Option(
        False, "--interactive", help="Decide the approval yourself."
    ),
) -> None:
    """Run one of the deployment's scripted scenarios."""
    deployment = load_deployment()
    scenario = deployment.demos.get(name)
    if scenario is None:
        raise typer.BadParameter(
            f"no scenario '{name}'. Available: {sorted(deployment.demos)}"
        )

    kwargs: dict[str, object] = {"db_path": db}
    if "auto_approve" in scenario.__code__.co_varnames:
        kwargs["auto_approve"] = not interactive
    scenario(console, **kwargs)


# --- http ----------------------------------------------------------------------


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind."),
    port: int = typer.Option(8000, help="Port to bind."),
    db: Optional[str] = typer.Option(None),
) -> None:
    """Serve the approval surface over HTTP.

    The same orchestrator the CLI drives, behind a second front end. Approvals are
    read and decided at /approvals; a run's narrative is at /runs/{id}/audit.
    Interactive docs at /docs.
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise typer.BadParameter(
            "the HTTP surface needs extra dependencies: pip install -e '.[http]'"
        ) from exc

    from harmony.http.app import create_app

    harness = _harness(db=db)
    console.print(
        f"serving {harness.deployment.name} on http://{host}:{port}  (docs at /docs)"
    )
    console.print("[dim]authenticate with an X-Harmony-User header[/dim]")
    uvicorn.run(create_app(harness), host=host, port=port, log_level="warning")


# --- evaluation ----------------------------------------------------------------


@app.command()
def eval(
    live: bool = typer.Option(
        False, "--live", help="Call the model for real instead of replaying."
    ),
    case: Optional[str] = typer.Option(None, help="Run one case by id."),
    verbose: bool = typer.Option(False, "--verbose", help="Show passing checks too."),
) -> None:
    """Check recommendation quality against the deployment's golden cases.

    In replay mode this is a regression test on the harness. With --live it is a
    regression test on the model and the prompts, which is what you want before
    shipping a prompt change.
    """
    from harmony.eval.runner import load_cases, run_eval

    deployment = load_deployment()
    if deployment.eval_cases_path is None:
        console.print("[dim]this deployment ships no evaluation cases[/dim]")
        raise typer.Exit(0)

    cases = load_cases(deployment.eval_cases_path)
    if case:
        cases = [c for c in cases if c.id == case]
        if not cases:
            raise typer.BadParameter(f"no case with id '{case}'")

    mode = "live" if live else os.environ.get("HARMONY_LLM", "replay")
    if live:
        os.environ["HARMONY_LLM"] = "live"

    console.print(f"running {len(cases)} case(s) in [bold]{mode}[/bold] mode")
    console.print()
    report = run_eval(deployment, cases, llm=build_client(), mode=mode)

    for result in report.results:
        mark = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        console.print(f"{mark}  {result.case_id}")
        if result.error:
            console.print(f"        [red]{result.error}[/red]")
        for check in result.checks:
            if check.passed and not verbose:
                continue
            tick = "[green]ok[/green]" if check.passed else "[red]no[/red]"
            detail = f" — {check.detail}" if check.detail else ""
            console.print(f"        {tick}  {check.name}{detail}")
        if result.summary and (verbose or not result.passed):
            console.print(f'        [dim]said: "{result.summary[:150]}"[/dim]')

    console.print()
    console.print(
        f"[bold]{report.passed} passed, {report.failed} failed[/bold] "
        f"({len(report.results)} cases, {mode} mode)"
    )
    if not report.ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
