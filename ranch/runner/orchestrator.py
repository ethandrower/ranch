"""Checkpointed orchestrator — wraps ClaudeSDKClient with pause/resume and interjections."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_code_sdk import ClaudeSDKClient, ClaudeCodeOptions, AssistantMessage, SystemMessage, TextBlock, ToolUseBlock
from rich.console import Console
from rich.rule import Rule

from ranch.db import db_session
from ranch.models import Run, Checkpoint, Interjection
from ranch.runner.checkpoints import make_checkpoint_hook, APPROVAL_REQUIRED
from ranch.runner.messages import HumanDecision, HumanNote
from ranch.runner.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_FREE, initial_user_prompt
from ranch.runner.state import transition
from ranch.runner.tools import ranch_mcp

console = Console()


def _detect_branch(cwd: Path) -> str | None:
    """Return the current git branch in cwd, or None if unavailable.

    Best-effort — used for PR discovery. Never raises.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        branch = result.stdout.strip()
        return branch or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


class Orchestrator:
    def __init__(self, agent: str, cwd: Path, ticket: str | None, brief: str, free: bool = False, auto_approve: bool = False):
        self.agent = agent
        self.cwd = cwd
        self.ticket = ticket
        self.brief = brief
        self.free = free
        self.auto_approve = auto_approve
        self.run_id: int | None = None
        self.sdk_session_id: str | None = None

        # Checkpoint pause signalling
        self._awaiting_approval = False
        self._approval_ready = asyncio.Event()
        self._approval_result: str | None = None
        self._last_checkpoint_kind: str | None = None

        self.stop_requested = False

    # ─── Checkpoint callback (called from PostToolUse hook) ──────────

    async def on_checkpoint(self, kind: str, summary: str, payload: dict | None) -> None:
        self._last_checkpoint_kind = kind
        with db_session() as db:
            run = db.query(Run).filter_by(id=self.run_id).one()
            cp = Checkpoint(
                run_id=self.run_id,
                kind=kind,
                summary=summary,
                payload_json=json.dumps(payload) if payload else None,
            )
            db.add(cp)
            if kind in APPROVAL_REQUIRED:
                transition(run, "needs_approval", session=db)

        console.print(Rule(f"[bold yellow]CHECKPOINT: {kind}"))
        console.print(summary)
        if kind in APPROVAL_REQUIRED:
            self._awaiting_approval = True
            if self.auto_approve:
                console.print("[dim](auto-approve mode — firing approval immediately)[/dim]")
                self._approval_result = "approved"
                self._approval_ready.set()
            else:
                console.print("[dim]Waiting for: !approve  |  !reject <reason>  |  !stop[/dim]")

    def requires_approval(self, kind: str) -> bool:
        return kind in APPROVAL_REQUIRED

    # ─── Main run loop ───────────────────────────────────────────────

    async def run(self) -> None:
        # Two entry paths:
        # 1. Fresh run (foreground `ranch run`): create the Run row here.
        # 2. Dispatched run: `ranch dispatch` already created the row and set
        #    self.run_id before spawning this process — just transition it.
        with db_session() as db:
            if self.run_id is None:
                run = Run(
                    agent=self.agent,
                    ticket=self.ticket,
                    cwd=str(self.cwd),
                    initial_prompt=self.brief,
                    state="planning",
                    free=int(self.free),
                    auto_approve=int(self.auto_approve),
                )
                db.add(run)
                db.flush()
                self.run_id = run.id
            else:
                run = db.query(Run).filter_by(id=self.run_id).one()
                run.state = "planning"

        console.print(f"[bold cyan]Ranch run #{self.run_id} — {self.agent} / {self.ticket or 'ad-hoc'}[/bold cyan]")
        console.print("[dim]Commands: !note <text>  !approve  !reject <reason>  !stop[/dim]")
        console.print()

        # Use append_system_prompt (not system_prompt) so Claude Code's default
        # behavior — including auto-loading the worktree's CLAUDE.md — still
        # runs. Setting system_prompt= would suppress CLAUDE.md and the agent
        # would miss project conventions like "branch off develop, not main".
        options = ClaudeCodeOptions(
            cwd=str(self.cwd),
            append_system_prompt=SYSTEM_PROMPT_FREE if self.free else SYSTEM_PROMPT,
            mcp_servers={"ranch": ranch_mcp},
            allowed_tools=[
                "Read", "Write", "Edit", "Bash", "Grep", "Glob",
                "mcp__ranch__record_checkpoint", "mcp__ranch__log_decision",
            ],
            hooks={"PostToolUse": [make_checkpoint_hook(self)]},
            permission_mode="acceptEdits",
        )

        try:
            async with ClaudeSDKClient(options=options) as client:
                # Send the initial prompt
                await client.query(initial_user_prompt(self.ticket, self.brief, free=self.free))

                # Interjection channels:
                # - stdin loop (foreground dev UX) enqueues rows — skipped when
                #   stdin isn't a TTY (dispatched/detached runs have /dev/null)
                # - db_poll loop dispatches pending rows — always on unless
                #   auto-approve mode is active (no human driver)
                stdin_task = None
                poll_task = None
                if not self.auto_approve:
                    poll_task = asyncio.create_task(self._db_poll_loop(client))
                    if sys.stdin.isatty():
                        stdin_task = asyncio.create_task(self._stdin_loop())

                try:
                    await self._main_loop(client)
                finally:
                    for task in (stdin_task, poll_task):
                        if task is not None:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

        except Exception as e:
            await self._finalize(error=str(e))
            raise

        await self._finalize()

    async def _main_loop(self, client: ClaudeSDKClient) -> None:
        """Drain agent responses until the run finishes.

        Approval is handled inside the PostToolUse hook (see checkpoints.py),
        which awaits the decision and returns it as additionalContext on the
        same tool result. This loop just renders and waits for the agent to
        be done.
        """
        from claude_code_sdk._errors import MessageParseError
        while not self.stop_requested:
            try:
                async for msg in client.receive_response():
                    self._render(msg)
                    self._capture_session_id(msg)
                    if self.stop_requested:
                        return
            except MessageParseError as e:
                if "rate_limit" in str(e).lower():
                    console.print("[yellow]⏳ rate_limit_event — retrying...[/yellow]")
                    continue
                raise
            # The turn ended cleanly. The agent has either finished or paused
            # at a checkpoint awaiting hook-injected approval (which it gets
            # synchronously). Either way, no further driving is needed.
            break

    def _render(self, msg) -> None:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    console.print(block.text, end="", highlight=False)
                elif isinstance(block, ToolUseBlock):
                    console.print(f"\n[dim]→ {block.name}[/dim]")
        console.file.flush() if hasattr(console, 'file') else None

    def _capture_session_id(self, msg) -> None:
        if isinstance(msg, SystemMessage) and not self.sdk_session_id:
            sid = msg.data.get("session_id")
            if sid:
                self.sdk_session_id = sid
                with db_session() as db:
                    db.query(Run).filter_by(id=self.run_id).update(
                        {"sdk_session_id": sid}
                    )

    # ─── Interjection channels ───────────────────────────────────────
    #
    # Two channels feed the same pipeline:
    #   stdin_loop  — foreground `!cmd` syntax → enqueue row (processed_at=NULL)
    #   CLI commands — `ranch approve/reject/note/stop <run_id>` from any shell
    # A single db_poll_loop consumes pending rows and dispatches them.
    # The 500ms poll latency is fine for human-driven interjections.

    async def _stdin_loop(self) -> None:
        """Read `!cmd` lines from stdin and enqueue them as Interjection rows.

        Uses a daemon thread + asyncio.Queue so:
        - A blocked readline() doesn't prevent process exit at shutdown
        - EOF on stdin properly terminates the loop instead of busy-spinning
        """
        import threading

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def reader() -> None:
            try:
                for line in sys.stdin:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            except (EOFError, OSError, ValueError):
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=reader, daemon=True, name="ranch-stdin").start()

        while True:
            line = await queue.get()
            if line is None:  # EOF
                break
            line = line.strip()
            if line.startswith("!"):
                cmd, _, rest = line[1:].partition(" ")
                self._enqueue_interjection(cmd.lower(), rest)

    def _enqueue_interjection(self, kind: str, content: str) -> None:
        with db_session() as db:
            db.add(Interjection(run_id=self.run_id, kind=kind, content=content))

    async def _db_poll_loop(self, client: ClaudeSDKClient) -> None:
        """Poll the DB every 500ms for unprocessed interjections and dispatch them."""
        while not self.stop_requested:
            await asyncio.sleep(0.5)
            pending: list[tuple[str, str]] = []
            with db_session() as db:
                rows = (
                    db.query(Interjection)
                    .filter_by(run_id=self.run_id, processed_at=None)
                    .order_by(Interjection.id)
                    .all()
                )
                now = datetime.now(timezone.utc)
                for row in rows:
                    pending.append((row.kind, row.content or ""))
                    row.processed_at = now
            for kind, content in pending:
                await self._dispatch_interjection(kind, content, client)

    async def _dispatch_interjection(self, cmd: str, rest: str, client: ClaudeSDKClient) -> None:
        cmd = cmd.lower()

        if cmd == "stop":
            console.print("[yellow]Stopping run...[/yellow]")
            self.stop_requested = True
            self._approval_result = "stopped"
            self._approval_ready.set()

        elif cmd == "note":
            console.print(f"[dim]Note forwarded: {rest}[/dim]")
            await client.query(HumanNote(content=rest).to_prompt())

        elif cmd == "approve":
            console.print("[green]Approved.[/green]")
            self._approval_result = "approved"
            self._approval_ready.set()

        elif cmd == "reject":
            reason = rest or "(no reason given)"
            console.print(f"[red]Rejected: {reason}[/red]")
            self._approval_result = f"rejected — {reason}"
            self._approval_ready.set()

        else:
            console.print(f"[dim]Unknown command: !{cmd}[/dim]")

    def _record_decision(self, decision: str, note: str) -> None:
        with db_session() as db:
            cp = (
                db.query(Checkpoint)
                .filter_by(run_id=self.run_id, decision=None)
                .order_by(Checkpoint.id.desc())
                .first()
            )
            if cp:
                cp.decision = decision
                cp.decision_note = note or None
                cp.decided_at = datetime.now(timezone.utc)

            run = db.query(Run).filter_by(id=self.run_id).one()
            run.state = run.state_before_pause or "in_development"

    # ─── Finalize ────────────────────────────────────────────────────

    async def _finalize(self, error: str | None = None) -> None:
        exit_reason = "error" if error else ("stopped" if self.stop_requested else "completed")
        final_state = exit_reason  # maps 1:1 for terminal states

        # Capture the branch the agent pushed on so poll-pr can discover the
        # PR later via `bb/gh pr list --head <branch>`. Best-effort — missing
        # git, detached HEAD, or stopped runs just leave branch_name NULL.
        branch_name = _detect_branch(self.cwd)

        with db_session() as db:
            run = db.query(Run).filter_by(id=self.run_id).one()
            run.ended_at = datetime.now(timezone.utc)
            run.exit_reason = exit_reason
            run.state = final_state
            if branch_name:
                run.branch_name = branch_name

        console.print()
        if error:
            console.print(f"[red]Run #{self.run_id} errored:[/red] {error}")
        else:
            console.print(f"[green]Run #{self.run_id} {exit_reason}.[/green]")

        # Fire reflection as a fire-and-forget subprocess (same pattern as Phase 1 hooks)
        if self.ticket and exit_reason in {"completed", "stopped"}:
            import subprocess
            from pathlib import Path as _Path
            ranch_root = _Path(__file__).resolve().parent.parent.parent
            venv_python = ranch_root / ".venv" / "bin" / "python"
            subprocess.Popen(
                [str(venv_python), "-m", "ranch.reflect_cli", self.ticket],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(ranch_root),
            )


# ─── Resume support ──────────────────────────────────────────────────

async def resume_run(run_id: int) -> None:
    """Resume a paused or stopped run using its stored SDK session ID."""
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            console.print(f"[red]Run #{run_id} not found.[/red]")
            return
        if not run.sdk_session_id:
            console.print(f"[red]Run #{run_id} has no SDK session ID — cannot resume.[/red]")
            return

        # Show most recent undecided checkpoint for context
        last_cp = (
            db.query(Checkpoint)
            .filter_by(run_id=run_id, decision=None)
            .order_by(Checkpoint.id.desc())
            .first()
        )

        agent = run.agent
        ticket = run.ticket or ""
        brief = run.initial_prompt
        sdk_session_id = run.sdk_session_id
        cwd = Path(run.cwd)

    console.print(f"[cyan]Resuming run #{run_id} ({agent} / {ticket})[/cyan]")
    if last_cp:
        console.print(Rule(f"Last checkpoint: {last_cp.kind}"))
        console.print(last_cp.summary)

    orch = Orchestrator(agent=agent, cwd=cwd, ticket=ticket, brief=brief)
    orch.run_id = run_id

    options = ClaudeCodeOptions(
        cwd=str(cwd),
        system_prompt=SYSTEM_PROMPT,
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
        stdin_task = asyncio.create_task(orch._stdin_loop())
        poll_task = asyncio.create_task(orch._db_poll_loop(client))
        try:
            await orch._main_loop(client)
        finally:
            for task in (stdin_task, poll_task):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    await orch._finalize()
