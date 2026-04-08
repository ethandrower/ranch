"""Click CLI for ranch."""
import click
from rich.console import Console
from rich.table import Table
from .db import init_db, db_session
from .models import Ticket, Feedback, Lesson, ReflectionRun
from .config import DB_PATH, CONFIG_FILE, write_default_config, reload_agents

console = Console()


@click.group()
def cli():
    """Ranch — memory and learning layer for Claude Code agent fleets."""
    pass


@cli.command()
def init():
    """Initialize the ranch database and agent config."""
    init_db()
    console.print(f"[green]✓[/green] Database initialized at {DB_PATH}")

    write_default_config()
    agents = reload_agents()

    console.print(f"[green]✓[/green] Config at {CONFIG_FILE}")
    console.print()

    if agents:
        console.print("[bold]Agent worktrees:[/bold]")
        for name, agent in agents.items():
            exists = agent.worktree.exists()
            marker = "[green]✓[/green]" if exists else "[red]✗[/red]"
            console.print(f"  {marker} {name:8} {agent.worktree}")
    else:
        console.print(
            f"[yellow]No agents configured yet.[/yellow] "
            f"Edit {CONFIG_FILE} to add your worktrees."
        )
    console.print()


@cli.command()
def status():
    """Show fleet status."""
    with db_session() as db:
        tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).limit(10).all()
        feedback_count = db.query(Feedback).count()
        lesson_count = db.query(Lesson).count()
        unprocessed = db.query(Feedback).filter(Feedback.extracted_to_lesson == 0).count()

    agents = reload_agents()
    table = Table(title="Ranch Status", show_header=True, header_style="bold cyan")
    table.add_column("Agent", style="cyan")
    table.add_column("Worktree")
    table.add_column("Active Ticket")
    for name, agent in agents.items():
        active = next(
            (t for t in tickets if t.agent_name == name and t.state != "done"), None
        )
        table.add_row(
            name,
            str(agent.worktree),
            active.ticket_id if active else "[dim]idle[/dim]",
        )
    console.print(table)
    console.print()
    console.print(
        f"[bold]Memory:[/bold] {feedback_count} feedback rows · "
        f"{lesson_count} lessons · {unprocessed} unprocessed"
    )


@cli.command()
@click.option("--limit", default=20, help="Max rows to show")
def feedback(limit):
    """List recent feedback rows."""
    with db_session() as db:
        rows = db.query(Feedback).order_by(Feedback.timestamp.desc()).limit(limit).all()
    table = Table(title="Recent Feedback", show_header=True)
    table.add_column("Time", style="dim")
    table.add_column("Agent")
    table.add_column("Ticket")
    table.add_column("Message", overflow="fold")
    for f in rows:
        table.add_row(
            f.timestamp.strftime("%m-%d %H:%M"),
            f.agent_name or "?",
            f.ticket_id or "?",
            (f.user_message or "")[:120],
        )
    console.print(table)


@cli.command()
@click.option("--category", default=None)
def lessons(category):
    """List lessons in the semantic memory."""
    with db_session() as db:
        q = db.query(Lesson).filter(Lesson.is_active == 1)
        if category:
            q = q.filter(Lesson.category == category)
        rows = q.order_by(Lesson.confidence.desc(), Lesson.times_reinforced.desc()).all()
    table = Table(title="Lessons", show_header=True)
    table.add_column("ID", style="dim")
    table.add_column("Conf")
    table.add_column("Reinf")
    table.add_column("Category")
    table.add_column("Lesson", overflow="fold")
    for l in rows:
        bar = "█" * l.confidence + "░" * (5 - l.confidence)
        table.add_row(str(l.id), bar, str(l.times_reinforced), l.category, l.content)
    console.print(table)


# ─── Phase 1 commands ────────────────────────────────────────

@cli.command()
@click.argument("ticket_id", required=False)
def reflect(ticket_id):
    """Run reflection on a ticket. Defaults to ticket on current git branch."""
    import subprocess
    from pathlib import Path
    from .feedback import detect_ticket_from_branch
    from .reflect import reflect_sync

    if not ticket_id:
        try:
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"], text=True
            ).strip()
            ticket_id = detect_ticket_from_branch(branch)
        except subprocess.CalledProcessError:
            pass
    if not ticket_id:
        console.print("[red]No ticket specified and could not detect one from the current branch.[/red]")
        raise click.Abort()

    console.print(f"[cyan]Reflecting on {ticket_id}...[/cyan]")
    result = reflect_sync(ticket_id)
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        return
    console.print(f"[green]✓[/green] {result['summary']}")
    console.print(f"  Processed: {result['feedback_count']} feedback rows")
    console.print(f"  Created:   {result['new_lessons']} new lessons")
    console.print(f"  Reinforced: {result['reinforced']} existing lessons")
    if result.get("cost_cents"):
        console.print(f"  Cost: ${result['cost_cents'] / 100:.4f}")


@cli.command()
@click.option("--tags", help="Comma-separated tags for context filtering")
@click.option("--out", type=click.Path(), help="Write to file instead of stdout")
def context(tags, out):
    """Print a markdown block of relevant lessons to inject into a new CC session."""
    from pathlib import Path
    from .context import get_relevant_lessons, format_context_markdown

    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    lessons_list = get_relevant_lessons(tags=tag_list or None)
    md = format_context_markdown(lessons_list)
    if out:
        Path(out).write_text(md)
        console.print(f"[green]✓[/green] Wrote {len(lessons_list)} lessons to {out}")
    else:
        click.echo(md)


# ─── Phase 2 commands ────────────────────────────────────────

@cli.command("run")
@click.argument("agent")
@click.option("--ticket", required=True, help="Ticket ID (e.g. ECD-123)")
@click.option("--brief", required=True, help="Plain-text brief or path to a .md file")
@click.option("--free", is_flag=True, default=False, help="Skip the plan→push workflow — brief is the full instruction")
@click.option("--auto-approve", is_flag=True, default=False, help="Auto-approve every checkpoint — for unattended evaluation runs")
def run_cmd(agent, ticket, brief, free, auto_approve):
    """Start a checkpointed run for an agent.

    By default the agent follows the plan→TDD→QA→pre-push workflow.
    Use --free for open-ended tasks (PR review, bug investigation, etc.)
    where that structure doesn't apply.
    Use --auto-approve to bypass interactive approval (for testing/evaluation).
    """
    import asyncio
    import os
    import sys
    from pathlib import Path
    from .config import reload_agents
    from .runner.orchestrator import Orchestrator

    agents = reload_agents()
    if agent not in agents:
        console.print(f"[red]Unknown agent '{agent}'. Known: {', '.join(agents)}[/red]")
        raise click.Abort()

    brief_text = Path(brief).read_text() if Path(brief).exists() else brief
    a = agents[agent]
    asyncio.run(
        Orchestrator(agent, a.worktree, ticket, brief_text, free=free, auto_approve=auto_approve).run()
    )
    # Force exit — defends against any lingering non-daemon threads (e.g. SDK internals)
    # that could otherwise keep the process alive after the run is finalized.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


@cli.command()
@click.argument("run_id", type=int)
def resume(run_id):
    """Resume a paused or stopped run by its ID."""
    import asyncio
    from .runner.orchestrator import resume_run
    asyncio.run(resume_run(run_id))


@cli.command("runs")
@click.option("--limit", default=20, help="Max rows to show")
@click.option("--agent", default=None, help="Filter by agent name")
def runs_cmd(limit, agent):
    """List recent runs and their states."""
    from .models import Run, Checkpoint, Interjection

    with db_session() as db:
        q = db.query(Run).order_by(Run.started_at.desc())
        if agent:
            q = q.filter(Run.agent == agent)
        rows = q.limit(limit).all()

    table = Table(title="Ranch Runs", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="dim")
    table.add_column("Agent", style="cyan")
    table.add_column("Ticket")
    table.add_column("State")
    table.add_column("Started")
    table.add_column("Exit")

    state_colors = {
        "completed": "green", "stopped": "yellow", "error": "red",
        "needs_approval": "bold yellow", "in_development": "cyan",
        "planning": "blue", "in_qa": "magenta",
    }
    for r in rows:
        color = state_colors.get(r.state, "white")
        table.add_row(
            str(r.id),
            r.agent,
            r.ticket or "—",
            f"[{color}]{r.state}[/{color}]",
            r.started_at.strftime("%m-%d %H:%M") if r.started_at else "—",
            r.exit_reason or "—",
        )
    console.print(table)


if __name__ == "__main__":
    cli()
