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
@click.argument("run_id", type=int, required=False)
def status(run_id):
    """Show fleet status, or detail for a specific run.

    With no arg: shows active (non-terminal) runs plus agent worktree summary.
    With RUN_ID: shows detailed state of that run, including PID liveness and
    the most recent undecided checkpoint.
    """
    from .models import Run, Checkpoint, Interjection
    from .runtime import is_alive, mark_orphans, TERMINAL_STATES

    # Always reap orphans before rendering so dead processes show as error
    reaped = mark_orphans()
    if reaped:
        console.print(f"[yellow]Reaped {len(reaped)} orphaned run(s): {reaped}[/yellow]")

    if run_id is not None:
        _render_run_detail(run_id, is_alive)
        return

    with db_session() as db:
        active_runs = (
            db.query(Run)
            .filter(~Run.state.in_(TERMINAL_STATES))
            .order_by(Run.started_at.desc())
            .all()
        )
        feedback_count = db.query(Feedback).count()
        lesson_count = db.query(Lesson).count()
        unprocessed = db.query(Feedback).filter(Feedback.extracted_to_lesson == 0).count()

    runs_table = Table(title="Active Runs", show_header=True, header_style="bold cyan")
    runs_table.add_column("ID", style="dim")
    runs_table.add_column("Agent", style="cyan")
    runs_table.add_column("Ticket")
    runs_table.add_column("State")
    runs_table.add_column("Mode")
    runs_table.add_column("PID")
    runs_table.add_column("Started")

    state_colors = {
        "needs_approval": "bold yellow",
        "in_development": "cyan",
        "planning": "blue",
        "in_qa": "magenta",
        "queued": "dim",
    }
    if active_runs:
        for r in active_runs:
            color = state_colors.get(r.state, "white")
            pid_marker = ""
            if r.pid:
                pid_marker = f"{r.pid}" if is_alive(r.pid) else f"[red]{r.pid} (dead)[/red]"
            runs_table.add_row(
                str(r.id),
                r.agent,
                r.ticket or "—",
                f"[{color}]{r.state}[/{color}]",
                r.dispatch_mode or "foreground",
                pid_marker,
                r.started_at.strftime("%m-%d %H:%M") if r.started_at else "—",
            )
        console.print(runs_table)
    else:
        console.print("[dim]No active runs.[/dim]")

    agents = reload_agents()
    if agents:
        console.print()
        agents_table = Table(title="Agents", show_header=True, header_style="bold cyan")
        agents_table.add_column("Agent", style="cyan")
        agents_table.add_column("Worktree")
        for name, agent in agents.items():
            agents_table.add_row(name, str(agent.worktree))
        console.print(agents_table)

    console.print()
    console.print(
        f"[bold]Memory:[/bold] {feedback_count} feedback rows · "
        f"{lesson_count} lessons · {unprocessed} unprocessed"
    )


def _render_run_detail(run_id: int, is_alive_fn):
    """Print full detail for a single run."""
    from .models import Run, Checkpoint, Interjection

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            console.print(f"[red]Run #{run_id} not found[/red]")
            raise click.Abort()

        pending_cp = (
            db.query(Checkpoint)
            .filter_by(run_id=run_id, decision=None)
            .order_by(Checkpoint.id.desc())
            .first()
        )
        recent_interjections = (
            db.query(Interjection)
            .filter_by(run_id=run_id)
            .order_by(Interjection.id.desc())
            .limit(5)
            .all()
        )
        # capture fields before session closes
        r = {
            "id": run.id, "agent": run.agent, "ticket": run.ticket,
            "state": run.state, "pid": run.pid, "log_path": run.log_path,
            "dispatch_mode": run.dispatch_mode, "started_at": run.started_at,
            "ended_at": run.ended_at, "exit_reason": run.exit_reason,
            "cwd": run.cwd, "sdk_session_id": run.sdk_session_id,
        }
        cp = None
        if pending_cp:
            cp = {
                "kind": pending_cp.kind, "summary": pending_cp.summary,
                "created_at": pending_cp.created_at,
            }
        interj = [
            {"kind": i.kind, "content": i.content, "created_at": i.created_at,
             "processed_at": i.processed_at}
            for i in recent_interjections
        ]

    alive = is_alive_fn(r["pid"]) if r["pid"] else None
    alive_str = (
        "[green]alive[/green]" if alive is True
        else ("[red]dead[/red]" if alive is False else "[dim]—[/dim]")
    )

    console.print(f"[bold cyan]Run #{r['id']}[/bold cyan]  {r['agent']} / {r['ticket'] or '—'}")
    console.print(f"  State:          [bold]{r['state']}[/bold]")
    console.print(f"  Dispatch mode:  {r['dispatch_mode']}")
    if r["pid"]:
        console.print(f"  PID:            {r['pid']}  ({alive_str})")
    if r["log_path"]:
        console.print(f"  Log:            {r['log_path']}")
    console.print(f"  Cwd:            {r['cwd']}")
    if r["sdk_session_id"]:
        console.print(f"  SDK session:    {r['sdk_session_id']}")
    console.print(f"  Started:        {r['started_at']}")
    if r["ended_at"]:
        console.print(f"  Ended:          {r['ended_at']}  ({r['exit_reason']})")

    if cp:
        console.print()
        console.print(f"[bold yellow]Pending checkpoint:[/bold yellow] {cp['kind']}")
        console.print(f"  {cp['summary']}")
        console.print(f"  Approve with: [cyan]ranch approve {r['id']}[/cyan]")

    if interj:
        console.print()
        console.print("[bold]Recent interjections:[/bold]")
        for i in reversed(interj):
            status_mark = "[dim](pending)[/dim]" if i["processed_at"] is None else ""
            content = (i["content"] or "").strip()
            display = f" — {content[:80]}" if content else ""
            console.print(f"  {i['created_at'].strftime('%H:%M:%S')}  !{i['kind']}{display}  {status_mark}")


@cli.command()
@click.option("--run", "run_ids", type=int, multiple=True, help="Watch specific run_id(s) (repeatable). Default: all non-terminal runs.")
@click.option("--timeout", type=float, default=None, help="Exit cleanly after N seconds if nothing changed")
def watch(run_ids, timeout):
    """Block until a watched run transitions state, then print and exit.

    Designed for /loop usage: `ranch watch --timeout 30` exits silently when
    nothing changed, or prints `<run_id> <state>` when something moved.
    """
    from .runtime import watch_for_change

    ids = list(run_ids) if run_ids else None
    result = watch_for_change(run_ids=ids, timeout_seconds=timeout)
    if result is None:
        return  # silent exit for /loop cadence
    rid, state = result
    console.print(f"Run #{rid} → [bold]{state}[/bold]")


@cli.command("poll-pr")
@click.argument("run_id", type=int)
@click.option("--pr", "pr_override", default=None, help="Force a specific PR id (bypasses auto-discovery)")
@click.option("--platform", default=None, type=click.Choice(["bb", "gh"]),
              help="Override platform detection (bb|gh)")
def poll_pr_cmd(run_id, pr_override, platform):
    """Fetch new PR review comments for a run. Loop-friendly: quiet when empty.

    On first call, auto-discovers the PR by matching Run.branch_name against
    `bb pr list` / `gh pr list --head`. Subsequent calls use the cached id.
    New comments are stored as ReviewComment rows. Re-running is idempotent.

    Designed for: /loop 10m ranch poll-pr <run_id>
    """
    from pathlib import Path
    from .db import db_session, init_db
    from .models import Run, ReviewComment
    from .runner.pr_backend import (
        detect_platform, get_backend, PRBackendError,
    )

    init_db()
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            console.print(f"[red]Run #{run_id} not found[/red]")
            raise click.Abort()
        branch = run.branch_name
        cwd = Path(run.cwd)
        pr_id = pr_override or run.pr_id
        pr_platform = platform or run.pr_platform or detect_platform(cwd)

    if not pr_platform:
        console.print("[red]Could not detect PR platform — pass --platform bb|gh[/red]")
        raise click.Abort()

    backend = get_backend(pr_platform)

    # Discover the PR if we don't have one yet
    if not pr_id:
        if not branch:
            console.print(f"[yellow]Run #{run_id} has no branch_name — cannot auto-discover PR[/yellow]")
            console.print("[dim]Pass --pr <id> to attach manually.[/dim]")
            raise click.Abort()
        try:
            found = backend.discover_pr_by_branch(branch, cwd)
        except PRBackendError as e:
            console.print(f"[red]PR discovery failed:[/red] {e}")
            raise click.Abort()
        if not found:
            # Loop-friendly: quiet exit when no PR exists yet
            return
        pr_id, pr_url = found
        with db_session() as db:
            db.query(Run).filter_by(id=run_id).update({
                "pr_id": pr_id, "pr_platform": pr_platform, "pr_url": pr_url,
            })
        console.print(f"[green]✓[/green] Discovered PR #{pr_id} for run #{run_id}")

    # Fetch + dedupe
    try:
        fetched = backend.fetch_comments(pr_id, cwd)
    except PRBackendError as e:
        console.print(f"[red]Comment fetch failed:[/red] {e}")
        raise click.Abort()

    new_rows: list[ReviewComment] = []
    with db_session() as db:
        existing = {
            pcid for (pcid,) in db.query(ReviewComment.platform_comment_id)
            .filter_by(run_id=run_id).all()
        }
        for c in fetched:
            if c.platform_comment_id in existing:
                continue
            row = ReviewComment(
                run_id=run_id,
                platform_comment_id=c.platform_comment_id,
                author=c.author,
                file_path=c.file_path,
                line_number=c.line_number,
                body=c.body,
                created_at_remote=c.created_at_remote,
            )
            db.add(row)
            new_rows.append(row)

    if not new_rows:
        console.print(f"[dim]no new comments on PR #{pr_id}[/dim]")
        return

    console.print(f"[bold yellow]{len(new_rows)} new comment(s) on PR #{pr_id}:[/bold yellow]")
    for c in new_rows:
        author = c.author or "?"
        loc = f" {c.file_path}:{c.line_number}" if c.file_path else ""
        snippet = (c.body or "").strip().replace("\n", " ")[:80]
        console.print(f"  [cyan]{author}[/cyan]{loc} — {snippet}")
    console.print()
    console.print(f"[dim]Respond with: [cyan]ranch respond-pr {run_id}[/cyan][/dim]")


@cli.command("resolve-comment")
@click.argument("run_id", type=int)
@click.argument("comment_id")
@click.option("--sha", default=None, help="Commit SHA that resolves this comment")
def resolve_comment_cmd(run_id, comment_id, sha):
    """Mark a review comment as resolved. Usually called by the agent after a fix commit."""
    from .db import db_session, init_db
    from .models import ReviewComment

    init_db()
    with db_session() as db:
        row = (
            db.query(ReviewComment)
            .filter_by(run_id=run_id, platform_comment_id=str(comment_id))
            .one_or_none()
        )
        if not row:
            console.print(f"[red]Comment {comment_id} not found on run #{run_id}[/red]")
            raise click.Abort()
        row.resolved = 1
        if sha:
            row.resolved_commit_sha = sha
    console.print(f"[green]✓[/green] Resolved comment {comment_id} on run #{run_id}")


@cli.command("respond-pr")
@click.argument("run_id", type=int)
def respond_pr_cmd(run_id):
    """Resume the agent with pending PR review comments as the brief.

    Uses the run's stored SDK session id to continue the same conversation. The
    agent runs a TRIAGE → FIX → PRE-PUSH workflow (see prompts.SYSTEM_PROMPT_PR_REVIEW).
    """
    import asyncio
    from pathlib import Path
    from .db import db_session, init_db
    from .models import Run, ReviewComment
    from .runner.orchestrator import Orchestrator
    from .runner.prompts import pr_review_initial_prompt, SYSTEM_PROMPT_PR_REVIEW
    from claude_code_sdk import ClaudeSDKClient, ClaudeCodeOptions
    from .runner.tools import ranch_mcp
    from .runner.checkpoints import make_checkpoint_hook

    init_db()
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            console.print(f"[red]Run #{run_id} not found[/red]")
            raise click.Abort()
        if not run.pr_id:
            console.print(f"[yellow]Run #{run_id} has no PR attached — run `ranch poll-pr {run_id}` first[/yellow]")
            raise click.Abort()
        if not run.sdk_session_id:
            console.print(f"[yellow]Run #{run_id} has no SDK session — cannot resume[/yellow]")
            raise click.Abort()

        pending = (
            db.query(ReviewComment)
            .filter_by(run_id=run_id, resolved=0)
            .order_by(ReviewComment.id)
            .all()
        )
        comment_dicts = [
            {
                "platform_comment_id": c.platform_comment_id,
                "author": c.author,
                "file_path": c.file_path,
                "line_number": c.line_number,
                "body": c.body,
            }
            for c in pending
        ]
        agent = run.agent
        ticket = run.ticket or ""
        pr_id = run.pr_id
        pr_platform = run.pr_platform or "bb"
        cwd = Path(run.cwd)
        sdk_session_id = run.sdk_session_id

    if not comment_dicts:
        console.print(f"[dim]No unresolved comments on PR #{pr_id}.[/dim]")
        return

    brief = pr_review_initial_prompt(ticket, pr_id, pr_platform, comment_dicts)

    console.print(f"[cyan]Resuming run #{run_id} for PR #{pr_id} review response[/cyan]")
    console.print(f"  {len(comment_dicts)} unresolved comment(s)")
    console.print()

    async def _go():
        orch = Orchestrator(agent, cwd, ticket, brief)
        orch.run_id = run_id

        options = ClaudeCodeOptions(
            cwd=str(cwd),
            append_system_prompt=SYSTEM_PROMPT_PR_REVIEW,
            mcp_servers={"ranch": ranch_mcp},
            allowed_tools=[
                "Read", "Write", "Edit", "Bash", "Grep", "Glob",
                "mcp__ranch__record_checkpoint", "mcp__ranch__log_decision",
            ],
            hooks={"PostToolUse": [make_checkpoint_hook(orch)]},
            permission_mode="acceptEdits",
            resume=sdk_session_id,
        )

        async with ClaudeSDKClient(options=options) as client:
            await client.query(brief)
            import asyncio as _a
            stdin_task = _a.create_task(orch._stdin_loop())
            poll_task = _a.create_task(orch._db_poll_loop(client))
            try:
                await orch._main_loop(client)
            finally:
                for t in (stdin_task, poll_task):
                    t.cancel()
                    try:
                        await t
                    except _a.CancelledError:
                        pass
        await orch._finalize()

    asyncio.run(_go())


@cli.command("log")
@click.argument("run_id", type=int)
def log_cmd(run_id):
    """Print the log file path for a dispatched run. Use with: tail -f $(ranch log <id>)"""
    from .models import Run

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            console.print(f"[red]Run #{run_id} not found[/red]")
            raise click.Abort()
        if not run.log_path:
            console.print(f"[yellow]Run #{run_id} has no log file (foreground run?)[/yellow]")
            raise click.Abort()
        click.echo(run.log_path)


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
@click.option("--ticket", required=False, default=None, help="Ticket ID (e.g. ECD-123); optional for ad-hoc runs")
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


@cli.command("dispatch")
@click.argument("agent")
@click.option("--ticket", required=False, default=None, help="Ticket ID (e.g. ECD-123); optional for ad-hoc runs")
@click.option("--brief", required=True, help="Plain-text brief or path to a .md file")
@click.option("--free", is_flag=True, default=False, help="Skip the plan→push workflow")
@click.option("--auto-approve", is_flag=True, default=False, help="Auto-approve every checkpoint")
def dispatch_cmd(agent, ticket, brief, free, auto_approve):
    """Start a run in the background and return immediately.

    Creates a Run row, spawns a detached orchestrator subprocess, writes the
    PID + log path, and exits. Interact with the running agent via:
      ranch approve|reject|note|stop <run_id>
      ranch status <run_id>
      tail -f $(ranch log <run_id>)  (once Plan C lands)
    """
    import subprocess
    import sys
    from pathlib import Path
    from .config import reload_agents, LOG_DIR
    from .db import db_session, init_db
    from .models import Run

    init_db()
    agents = reload_agents()
    if agent not in agents:
        console.print(f"[red]Unknown agent '{agent}'. Known: {', '.join(agents)}[/red]")
        raise click.Abort()

    brief_text = Path(brief).read_text() if Path(brief).exists() else brief
    a = agents[agent]

    # Create the Run row first so we can give the caller a run_id and hand
    # the ID to the detached child. State stays "queued" until the child
    # actually picks it up.
    with db_session() as db:
        run = Run(
            agent=agent,
            ticket=ticket,
            cwd=str(a.worktree),
            initial_prompt=brief_text,
            state="queued",
            free=int(free),
            auto_approve=int(auto_approve),
            dispatch_mode="background",
        )
        db.add(run)
        db.flush()
        run_id = run.id

    log_path = LOG_DIR / f"run_{run_id}.log"
    log_fh = open(log_path, "ab", buffering=0)

    # Detach: new session, stdio redirected to log file + /dev/null. The
    # child survives parent shell exit via start_new_session=True.
    proc = subprocess.Popen(
        [sys.executable, "-m", "ranch", "_run-detached", str(run_id)],
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
        cwd=str(a.worktree),
    )
    log_fh.close()  # child owns the fd now

    with db_session() as db:
        db.query(Run).filter_by(id=run_id).update(
            {"pid": proc.pid, "log_path": str(log_path)}
        )

    console.print(f"[green]✓[/green] Dispatched run [bold]#{run_id}[/bold] ({agent} / {ticket or 'ad-hoc'})")
    console.print(f"  PID:  {proc.pid}")
    console.print(f"  Log:  {log_path}")
    console.print(f"  Approve with: [cyan]ranch approve {run_id}[/cyan]")


@cli.command("_run-detached", hidden=True)
@click.argument("run_id", type=int)
def run_detached_cmd(run_id):
    """Internal: rehydrate a dispatched Run row and execute the orchestrator.

    Not meant to be invoked directly — use `ranch dispatch` instead.
    """
    import asyncio
    import os
    import sys
    from pathlib import Path
    from .db import db_session
    from .models import Run
    from .runner.orchestrator import Orchestrator

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            console.print(f"[red]Run #{run_id} not found[/red]")
            sys.exit(1)
        agent = run.agent
        ticket = run.ticket or ""
        brief = run.initial_prompt
        cwd = Path(run.cwd)
        free = bool(run.free)
        auto_approve = bool(run.auto_approve)

    orch = Orchestrator(agent, cwd, ticket, brief, free=free, auto_approve=auto_approve)
    orch.run_id = run_id  # pre-created — run() will reuse it
    asyncio.run(orch.run())

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


TERMINAL_STATES = {"completed", "stopped", "error"}


def _queue_interjection(run_id: int, kind: str, content: str) -> None:
    """Insert an Interjection row for the orchestrator's DB poll loop to consume."""
    from .models import Run, Interjection

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            console.print(f"[red]Run #{run_id} not found[/red]")
            raise click.Abort()
        if run.state in TERMINAL_STATES:
            console.print(
                f"[yellow]Warning: run #{run_id} is {run.state} — "
                f"interjection written but will not be consumed.[/yellow]"
            )
        db.add(Interjection(run_id=run_id, kind=kind, content=content))


@cli.command("approve")
@click.argument("run_id", type=int)
@click.option("--note", default="", help="Optional note attached to the approval")
def approve_cmd(run_id, note):
    """Approve the current checkpoint of a running run."""
    _queue_interjection(run_id, "approve", note)
    console.print(f"[green]✓[/green] Approval queued for run #{run_id}")


@cli.command("reject")
@click.argument("run_id", type=int)
@click.argument("reason", required=False, default="")
def reject_cmd(run_id, reason):
    """Reject the current checkpoint of a running run."""
    _queue_interjection(run_id, "reject", reason)
    console.print(f"[red]✗[/red] Rejection queued for run #{run_id}")


@cli.command("note")
@click.argument("run_id", type=int)
@click.argument("text", nargs=-1, required=True)
def note_cmd(run_id, text):
    """Send a note to a running agent mid-run."""
    msg = " ".join(text)
    _queue_interjection(run_id, "note", msg)
    console.print(f"[cyan]→[/cyan] Note queued for run #{run_id}: {msg[:80]}")


@cli.command("stop")
@click.argument("run_id", type=int)
def stop_cmd(run_id):
    """Stop a running run cleanly."""
    _queue_interjection(run_id, "stop", "")
    console.print(f"[yellow]■[/yellow] Stop queued for run #{run_id}")


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
