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


class Orchestrator:
    def __init__(self, agent: str, cwd: Path, ticket: str, brief: str, free: bool = False, auto_approve: bool = False):
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
        with db_session() as db:
            run = Run(
                agent=self.agent,
                ticket=self.ticket,
                cwd=str(self.cwd),
                initial_prompt=self.brief,
                state="planning",
            )
            db.add(run)
            db.flush()
            self.run_id = run.id

        console.print(f"[bold cyan]Ranch run #{self.run_id} — {self.agent} / {self.ticket}[/bold cyan]")
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

                # Start stdin reader in background — skipped in auto-approve mode
                stdin_task = None
                if not self.auto_approve:
                    stdin_task = asyncio.create_task(self._stdin_loop(client))

                try:
                    await self._main_loop(client)
                finally:
                    if stdin_task is not None:
                        stdin_task.cancel()
                        try:
                            await stdin_task
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

    # ─── Stdin interjection loop ─────────────────────────────────────

    async def _stdin_loop(self, client: ClaudeSDKClient) -> None:
        """Read interjections from stdin via a daemon thread + asyncio.Queue.

        Using a daemon thread (instead of run_in_executor) ensures that:
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
                # EOF marker — main loop will exit
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=reader, daemon=True, name="ranch-stdin").start()

        while True:
            line = await queue.get()
            if line is None:  # EOF
                break
            line = line.strip()
            if line.startswith("!"):
                await self._handle_interjection(line, client)

    async def _handle_interjection(self, line: str, client: ClaudeSDKClient) -> None:
        cmd, _, rest = line[1:].partition(" ")
        cmd = cmd.lower()

        with db_session() as db:
            db.add(Interjection(run_id=self.run_id, kind=cmd, content=rest))

        if cmd == "stop":
            console.print("[yellow]Stopping run...[/yellow]")
            self.stop_requested = True
            # Unblock any pending approval wait
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

        with db_session() as db:
            run = db.query(Run).filter_by(id=self.run_id).one()
            run.ended_at = datetime.now(timezone.utc)
            run.exit_reason = exit_reason
            run.state = final_state

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
        stdin_task = asyncio.create_task(orch._stdin_loop(client))
        try:
            await orch._main_loop(client)
        finally:
            stdin_task.cancel()
            try:
                await stdin_task
            except asyncio.CancelledError:
                pass

    await orch._finalize()
