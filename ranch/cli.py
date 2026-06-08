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


def _fetch_latest_dossier(run_id: int):
    """Return (run, payload_dict | None) for the latest dossier on a run.

    Caller is responsible for handling the not-found and no-dossier cases.
    """
    import json as _json
    from .models import Dossier, Run

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            return None, None
        latest = (
            db.query(Dossier)
            .filter_by(run_id=run_id)
            .order_by(Dossier.created_at.desc())
            .first()
        )
        # Detach from session — caller will read these as plain values.
        run_snapshot = {
            "id": run.id,
            "agent": run.agent,
            "ticket": run.ticket,
            "state": run.state,
        }
        if not latest:
            return run_snapshot, None
        payload = _json.loads(latest.payload_json)
        payload["_updated_at"] = latest.created_at.isoformat()
        return run_snapshot, payload


def _render_dossier_panel(run: dict, payload: dict | None):
    """Build a Rich Panel renderable for one run's latest dossier."""
    from rich.panel import Panel
    from rich.text import Text

    title = f"Run #{run['id']}  ·  {run['agent']} / {run['ticket'] or 'ad-hoc'}"
    if payload is None:
        return Panel(Text("no dossier yet", style="dim"), title=title, border_style="dim")

    state_color = {
        "researching": "blue",
        "planning": "magenta",
        "coding": "cyan",
        "testing": "yellow",
        "judging": "yellow",
        "parked": "bold yellow",
    }.get(payload["state"], "white")

    body = Text()
    body.append(f"State: ", style="bold")
    body.append(f"{payload['state']}\n", style=state_color)
    body.append("Just did: ", style="bold")
    body.append(f"{payload['just_did']}\n")
    if payload.get("blocker"):
        body.append("Blocker: ", style="bold yellow")
        body.append(f"{payload['blocker']}\n", style="yellow")

    plan = payload.get("plan") or []
    if plan:
        body.append("\nPlan\n", style="bold")
        for step in plan:
            mark = {"done": "✓", "in_progress": "▸", "pending": "·"}.get(step["status"], "·")
            mark_style = {"done": "green", "in_progress": "yellow", "pending": "dim"}.get(step["status"], "white")
            body.append(f"  {mark} ", style=mark_style)
            body.append(f"{step['step']}\n")
            if step.get("notes"):
                body.append(f"      {step['notes']}\n", style="dim")

    options = payload.get("options") or []
    if options:
        body.append("\nOptions\n", style="bold")
        for opt in options:
            body.append(f"  • ", style="dim")
            body.append(f"{opt['label']}", style="bold")
            body.append(f" — {opt['description']}\n")

    files = payload.get("files_touched") or []
    if files:
        listing = ", ".join(files[:8]) + ("..." if len(files) > 8 else "")
        body.append(f"\nFiles touched ({len(files)}): {listing}\n", style="dim")

    details = payload.get("details")
    if details:
        # Compact preview — the UI's expand view will show the full thing.
        # Show first ~3 lines or 240 chars, whichever is smaller, so the CLI
        # panel stays usable when scanning many runs at once.
        lines = details.strip().splitlines()
        preview = "\n".join(lines[:3])
        if len(preview) > 240:
            preview = preview[:237] + "..."
        elif len(lines) > 3:
            preview += "\n..."
        body.append("\nDetails:\n", style="bold")
        body.append(f"{preview}\n", style="dim")

    return Panel(body, title=title, border_style=state_color)


@cli.command("dossier")
@click.argument("run_id", type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON instead of rendered text.")
@click.option("--watch", "watch", is_flag=True, help="Refresh the dossier in place every --interval seconds.")
@click.option("--interval", default=2.0, type=float, help="Refresh interval in seconds (with --watch).")
def dossier_cmd(run_id, as_json, watch, interval):
    """Show the latest dossier (agent self-report) for a run."""
    import json as _json

    run, payload = _fetch_latest_dossier(run_id)
    if run is None:
        console.print(f"[red]Run #{run_id} not found[/red]")
        raise click.Abort()

    if as_json:
        if watch:
            console.print("[red]--watch and --json are mutually exclusive[/red]")
            raise click.Abort()
        click.echo(_json.dumps(payload, indent=2) if payload else "null")
        return

    if not watch:
        if payload is None:
            console.print(f"[yellow]Run #{run_id} has no dossier yet.[/yellow]")
            return
        console.print(_render_dossier_panel(run, payload))
        return

    # Live mode — repaint in place until Ctrl-C or the run reaches a terminal state.
    from rich.live import Live
    import time

    terminal_states = {"completed", "stopped", "error"}
    with Live(_render_dossier_panel(run, payload), console=console, refresh_per_second=4) as live:
        try:
            while True:
                time.sleep(interval)
                run, payload = _fetch_latest_dossier(run_id)
                if run is None:
                    break
                live.update(_render_dossier_panel(run, payload))
                if run["state"] in terminal_states:
                    break
        except KeyboardInterrupt:
            pass


@cli.group("labs")
def labs_group():
    """Labs deploy to per-hand Dokku app (Phase H9)."""


@labs_group.command("deploy")
@click.argument("run_id", type=int)
@click.option("--branch", default=None, help="Source branch to push (defaults to Run.branch_name).")
@click.option("--force", is_flag=True, help="Pass --force to git push (fine on dev envs).")
@click.option("--health-timeout", default=180.0, type=float, help="Seconds to wait for the URL to respond after push.")
def labs_deploy_cmd(run_id, branch, force, health_timeout):
    """Push the run's branch to its agent's Dokku app, then health-check.

    Reads the [dokku] + [agents.<name>.dokku] config blocks from
    ~/.ranch/config.toml (sensible defaults: dev-<agent> on
    dokku@178.105.80.165, URL <agent>.staging.citemed.com).
    """
    from .labs import deploy_run_to_labs

    console.print(f"[cyan]Deploying run #{run_id} to labs...[/cyan]")
    result = deploy_run_to_labs(
        run_id,
        source_branch=branch,
        force=force,
        health_timeout_seconds=health_timeout,
    )

    if result.url:
        console.print(f"[dim]URL: {result.url}[/dim]")
    console.print(f"[dim]Elapsed: {result.elapsed_seconds:.1f}s[/dim]")
    if result.push_output:
        # Show the last ~10 lines of push output so the operator can
        # spot build errors at a glance.
        lines = result.push_output.splitlines()
        tail = "\n".join(lines[-10:]) if len(lines) > 10 else result.push_output
        console.print("[dim]push (tail):[/dim]")
        console.print(tail)

    if result.ok:
        console.print(f"[green]✓ Deploy live at {result.url}[/green]")
        return

    console.print(f"[red]✗ Deploy failed[/red]")
    if result.reason:
        console.print(f"[red]  reason: {result.reason}[/red]")
    if result.health and result.health.error:
        console.print(f"[red]  health: {result.health.error}[/red]")
    raise click.Abort()


@cli.group("pr")
def pr_group():
    """PR draft + open (Phase H10)."""


@pr_group.command("draft")
@click.argument("run_id", type=int)
@click.option("--figma", default=None, help="Optional figma URL to link.")
@click.option("--jira-base", default=None, help="Jira base URL for linking the ticket (e.g. https://yourorg.atlassian.net).")
@click.option("--out", default=None, type=click.Path(), help="Write the body to a file instead of stdout.")
def pr_draft_cmd(run_id, figma, jira_base, out):
    """Render a PR title + body for a completed run, no remote calls."""
    from .pr_draft import render_draft

    try:
        draft, _ = render_draft(run_id, figma_url=figma, jira_base_url=jira_base)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise click.Abort()

    if out:
        from pathlib import Path as _Path
        _Path(out).write_text(f"# {draft.title}\n\n{draft.body}")
        console.print(f"[dim]Wrote draft → {out}[/dim]")
        return

    console.print(f"[bold cyan]Title:[/bold cyan] {draft.title}\n")
    # Body is markdown — emit with click.echo so Rich doesn't mangle [x] / [kind] tags
    click.echo(draft.body)


@pr_group.command("open")
@click.argument("run_id", type=int)
@click.option("--figma", default=None, help="Optional figma URL to link.")
@click.option("--jira-base", default=None, help="Jira base URL for linking the ticket.")
@click.option("--ready", is_flag=True, help="Open as ready-for-review instead of draft.")
@click.option("--base-branch", default=None, help="Override the PR base branch.")
@click.option("--platform", default=None, type=click.Choice(["bb", "gh"]), help="Force a backend. Default: auto-detect via .git/config.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def pr_open_cmd(run_id, figma, jira_base, ready, base_branch, platform, yes):
    """Actually fire `bb pr create` (or gh) using the rendered draft."""
    from pathlib import Path as _Path
    from .pr_draft import render_draft
    from .runner.pr_backend import (
        PRBackendError, detect_platform, get_backend,
    )

    try:
        draft, artifacts = render_draft(run_id, figma_url=figma, jira_base_url=jira_base)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise click.Abort()

    cwd = _Path(artifacts.cwd)
    pf = platform or detect_platform(cwd)
    if not pf:
        console.print("[red]Could not detect a PR platform (bb or gh) for this worktree.[/red]")
        raise click.Abort()

    console.print(f"[bold cyan]Title:[/bold cyan] {draft.title}\n")
    # Body is markdown — emit raw to avoid Rich markup parsing
    click.echo(draft.body)
    console.print()
    console.print(f"[dim]Backend: {pf}  ·  draft: {not ready}  ·  cwd: {cwd}[/dim]")

    if not yes:
        if not click.confirm("Open this PR?", default=False):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    backend = get_backend(pf)
    try:
        pr_id, url = backend.create_pr(
            title=draft.title, body=draft.body, cwd=cwd,
            draft=not ready, base_branch=base_branch,
        )
    except PRBackendError as e:
        console.print(f"[red]{pf} pr create failed:[/red] {e}")
        raise click.Abort()

    # Persist the PR linkage to the Run row so the existing poll-pr / respond-pr
    # commands can find this PR without re-discovering by branch.
    from .models import Run
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if run:
            run.pr_id = pr_id
            run.pr_platform = pf
            run.pr_url = url

    console.print(f"[green]Opened PR #{pr_id}[/green]  {url}")


@cli.group("hand")
def hand_group():
    """Ranch hand — virtual engineer daemon (Phase H11)."""


@hand_group.command("start")
@click.argument("name")
@click.option("--poll", default=30.0, type=float, help="Poll interval in seconds (default 30).")
@click.option("--project", default=None, help="Restrict triage to a single Jira project key.")
def hand_start_cmd(name, poll, project):
    """Start the ranch hand daemon for an agent. Runs in the foreground."""
    import asyncio as _asyncio
    from .config import AGENTS, reload_agents
    from .hand import RanchHand, _read_pid, _pid_alive

    reload_agents()
    if name not in AGENTS:
        console.print(f"[red]Unknown agent '{name}'. Configure it in ~/.ranch/config.toml.[/red]")
        raise click.Abort()

    existing_pid = _read_pid(name)
    if existing_pid and _pid_alive(existing_pid):
        console.print(f"[red]Ranch hand '{name}' is already running (pid {existing_pid}).[/red]")
        raise click.Abort()

    hand = RanchHand(
        name=name,
        cwd=AGENTS[name].worktree,
        poll_seconds=poll,
        jira_project=project,
    )
    try:
        _asyncio.run(hand.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")


@hand_group.command("stop")
@click.argument("name")
def hand_stop_cmd(name):
    """Request a graceful stop of the ranch hand daemon."""
    from .hand import request_stop
    if request_stop(name):
        console.print(f"[green]Stop signal sent to ranch hand '{name}'.[/green]")
    else:
        console.print(f"[yellow]Ranch hand '{name}' is not running.[/yellow]")


@hand_group.command("status")
@click.option("--name", default=None, help="Show status for one hand only.")
def hand_status_cmd(name):
    """Show what each ranch hand is doing."""
    from .hand import get_hand_status, list_all_hand_statuses
    if name:
        statuses = [get_hand_status(name)]
    else:
        statuses = list_all_hand_statuses()

    if not statuses:
        console.print("[yellow]No hands configured.[/yellow]")
        return

    table = Table(title="Ranch hands", show_header=True)
    table.add_column("Hand")
    table.add_column("State")
    table.add_column("PID", width=8)
    table.add_column("Run", width=8)
    table.add_column("Ticket")
    table.add_column("Dossier")
    table.add_column("Detail")
    state_styles = {"running": "green", "stopped": "dim", "missing": "red", "idle": "yellow"}
    for s in statuses:
        table.add_row(
            s.name,
            f"[{state_styles.get(s.state, 'white')}]{s.state}[/{state_styles.get(s.state, 'white')}]",
            str(s.pid) if s.pid else "—",
            f"#{s.current_run_id}" if s.current_run_id else "—",
            s.current_ticket or "—",
            s.current_dossier_state or "—",
            s.detail,
        )
    console.print(table)


@cli.command("propose")
@click.argument("ticket", type=str)
@click.option("--agent", default=None, help="Agent whose worktree to run in. Required if --cwd isn't given.")
@click.option("--cwd", default=None, type=click.Path(exists=True, file_okay=False, resolve_path=True), help="Override the cwd; defaults to the agent's configured worktree.")
@click.option("--budget", default=None, type=float, help="Override the wall-clock budget in seconds (default 180).")
@click.option("--auto-approve", is_flag=True, help="Don't block waiting for !approve (use when chaining into ranch run).")
def propose_cmd(ticket, agent, cwd, budget, auto_approve):
    """Run a bounded plan + acceptance proposal session for a ticket (Phase H6).

    Reads the saved scope bundle from ~/.ranch/scopes/<ticket>.md, runs an
    SDK session with file-modification tools disabled, and produces a final
    parked dossier with the plan + acceptance criteria.
    """
    import asyncio as _asyncio
    from pathlib import Path as _Path
    from .config import AGENTS, reload_agents
    from .propose import (
        DEFAULT_PROPOSE_BUDGET_SECONDS,
        PROPOSE_ALLOWED_TOOLS,
        PROPOSE_SYSTEM_PROMPT,
        ProposeError,
        build_propose_brief,
        resolve_scope_markdown,
    )
    from .runner.orchestrator import Orchestrator

    try:
        scope_md = resolve_scope_markdown(ticket)
    except ProposeError as e:
        console.print(f"[red]{e}[/red]")
        raise click.Abort()

    if cwd:
        worktree = _Path(cwd)
        agent_name = agent or "ad-hoc"
    elif agent:
        reload_agents()
        if agent not in AGENTS:
            console.print(f"[red]Unknown agent '{agent}'. Configure it in ~/.ranch/config.toml.[/red]")
            raise click.Abort()
        worktree = AGENTS[agent].worktree
        agent_name = agent
    else:
        console.print("[red]Must specify --agent or --cwd.[/red]")
        raise click.Abort()

    brief = build_propose_brief(ticket, scope_md)
    budget_seconds = budget if budget is not None else DEFAULT_PROPOSE_BUDGET_SECONDS

    orch = Orchestrator(
        agent=agent_name,
        cwd=worktree,
        ticket=ticket,
        brief=brief,
        free=True,
        auto_approve=auto_approve,
        allowed_tools_override=PROPOSE_ALLOWED_TOOLS,
        budget_seconds=budget_seconds,
        append_system_prompt_override=PROPOSE_SYSTEM_PROMPT,
    )

    console.print(f"[bold cyan]Propose session — ticket {ticket} / agent {agent_name} / budget {budget_seconds:.0f}s[/bold cyan]")
    console.print(f"[dim]cwd: {worktree}[/dim]")
    console.print(f"[dim]Tools restricted to: {', '.join(PROPOSE_ALLOWED_TOOLS)}[/dim]\n")

    try:
        _asyncio.run(orch.run())
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/yellow]")
        return

    console.print(f"\n[bold]Proposal result — run #{orch.run_id}[/bold]")
    console.print(f"[dim]Inspect with: ranch dossier {orch.run_id}[/dim]")


@cli.command("scope")
@click.argument("ticket_key", type=str)
@click.option("--save", is_flag=True, help="Persist the bundle to ~/.ranch/scopes/<key>.md for downstream consumption by `ranch propose` / `ranch run`.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of markdown (for the ranch hand scheduler).")
@click.option("--cwd", default=None, type=click.Path(exists=True, file_okay=False, resolve_path=True), help="Worktree to use for `bb pr list` discovery. Default: current directory.")
def scope_cmd(ticket_key, save, as_json, cwd):
    """Build a context bundle (epic + sisters + PRs + design links) for a ticket."""
    import json as _json
    from pathlib import Path as _Path
    from .scope import build_scope, render_scope_markdown, save_scope
    from .triage import JiraClient, JiraConfig, JiraConfigError

    try:
        cfg = JiraConfig.load()
    except JiraConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise click.Abort()

    work_cwd = _Path(cwd) if cwd else _Path.cwd()
    try:
        with JiraClient(cfg) as client:
            scope = build_scope(ticket_key, jira=client, cwd=work_cwd)
    except Exception as e:
        console.print(f"[red]Failed to build scope:[/red] {e}")
        raise click.Abort()

    if as_json:
        click.echo(_json.dumps(scope.to_dict(), indent=2))
    else:
        click.echo(render_scope_markdown(scope))

    if save:
        path = save_scope(scope)
        console.print(f"[dim]Saved → {path}[/dim]")


@cli.command("triage")
@click.option("--agent", default=None, help="Exclude tickets already in flight for this agent (default: anyone).")
@click.option("--project", default=None, help="Filter to a single Jira project key (e.g. ECD).")
@click.option("--top", "top_n", default=10, type=int, help="Show only the top N candidates.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON suitable for scripting (e.g. by the ranch hand scheduler).")
def triage_cmd(agent, project, top_n, as_json):
    """Rank assigned Jira tickets by viability (Phase H4)."""
    import json as _json
    from .triage import (
        JiraClient,
        JiraConfig,
        JiraConfigError,
        in_flight_ticket_keys_for_agent,
        triage,
    )

    try:
        cfg = JiraConfig.load()
    except JiraConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise click.Abort()

    in_flight = in_flight_ticket_keys_for_agent(agent)

    try:
        with JiraClient(cfg) as client:
            tickets = client.list_assigned_to_me(project=project)
    except Exception as e:
        console.print(f"[red]Jira request failed:[/red] {e}")
        raise click.Abort()

    ranked = triage(tickets, in_flight)
    top = ranked[:top_n]

    if as_json:
        out = [
            {
                "key": t.key,
                "summary": t.summary,
                "status": t.status,
                "priority": t.priority,
                "has_figma_link": t.has_figma_link,
                "score": {
                    "total": s.total,
                    "status": s.status,
                    "design_present": s.design_present,
                    "ac_clarity": s.ac_clarity,
                    "priority": s.priority,
                    "age": s.age,
                },
            }
            for t, s in top
        ]
        click.echo(_json.dumps(out, indent=2))
        return

    if not top:
        console.print("[yellow]No viable tickets found.[/yellow]")
        if in_flight:
            console.print(f"[dim]({len(in_flight)} ticket(s) excluded as already in flight: {', '.join(sorted(in_flight))})[/dim]")
        return

    table = Table(title=f"Triage — top {len(top)} of {len(ranked)}", show_header=True)
    table.add_column("Rank", style="dim", width=4)
    table.add_column("Key", style="bold cyan")
    table.add_column("Status")
    table.add_column("Pri", width=8)
    table.add_column("Figma", justify="center", width=5)
    table.add_column("AC", justify="center", width=4)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Summary")
    for i, (t, s) in enumerate(top, 1):
        table.add_row(
            str(i),
            t.key,
            t.status,
            t.priority or "—",
            "✓" if t.has_figma_link else "—",
            "✓" if s.ac_clarity > 0 else "—",
            f"{s.total:.0f}",
            t.summary[:60] + ("…" if len(t.summary) > 60 else ""),
        )
    console.print(table)
    if in_flight:
        console.print(f"[dim]Excluded {len(in_flight)} ticket(s) already in flight: {', '.join(sorted(in_flight))}[/dim]")


@cli.command("fleet")
@click.option("--all", "show_all", is_flag=True, help="Include completed/stopped/error runs.")
@click.option("--watch", "watch", is_flag=True, help="Refresh in place every --interval seconds.")
@click.option("--interval", default=2.0, type=float, help="Refresh interval in seconds (with --watch).")
def fleet_cmd(show_all, watch, interval):
    """Show the latest dossier for every active run, grouped by agent."""
    from .models import Dossier, Run
    from rich.console import Group
    import time

    terminal_states = {"completed", "stopped", "error"}

    def build_view():
        with db_session() as db:
            q = db.query(Run)
            if not show_all:
                q = q.filter(~Run.state.in_(terminal_states))
            runs = q.order_by(Run.started_at.desc()).all()
            panels = []
            from rich.text import Text
            from rich.panel import Panel
            if not runs:
                return Panel(Text("No active runs.", style="dim"), title="Fleet", border_style="dim")
            for run in runs:
                run_snapshot = {
                    "id": run.id,
                    "agent": run.agent,
                    "ticket": run.ticket,
                    "state": run.state,
                }
                latest = (
                    db.query(Dossier)
                    .filter_by(run_id=run.id)
                    .order_by(Dossier.created_at.desc())
                    .first()
                )
                payload = None
                if latest:
                    import json as _json
                    payload = _json.loads(latest.payload_json)
                panels.append(_render_dossier_panel(run_snapshot, payload))
            return Group(*panels)

    if not watch:
        console.print(build_view())
        return

    from rich.live import Live
    with Live(build_view(), console=console, refresh_per_second=4) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(build_view())
        except KeyboardInterrupt:
            pass


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
