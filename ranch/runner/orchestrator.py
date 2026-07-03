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
from ranch.models import Run, Checkpoint, Dossier, Interjection
from ranch.runner.blocks import make_block_hook
from ranch.runner.checkpoints import make_checkpoint_hook, APPROVAL_REQUIRED
from ranch.runner.dossier import make_dossier_hook
from ranch.runner.judge_hook import make_judge_hook
from ranch.runner.messages import HumanDecision, HumanNote, RecordStateInput
from ranch.runner.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_FREE, initial_user_prompt
from ranch.runner.state import transition
from ranch.runner.tools import ranch_mcp, reset_judge_budget

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


DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Bash", "Grep", "Glob",
    "mcp__ranch__record_checkpoint", "mcp__ranch__log_decision",
    "mcp__ranch__record_state", "mcp__ranch__run_acceptance",
    "mcp__ranch__record_block",
]


class Orchestrator:
    def __init__(
        self,
        agent: str,
        cwd: Path,
        ticket: str | None,
        brief: str,
        free: bool = False,
        auto_approve: bool = False,
        *,
        allowed_tools_override: list[str] | None = None,
        budget_seconds: float | None = None,
        append_system_prompt_override: str | None = None,
        auto_approve_kinds: set[str] | None = None,
        one_shot: bool = False,
    ):
        self.agent = agent
        self.cwd = cwd
        self.ticket = ticket
        self.brief = brief
        self.free = free
        self.auto_approve = auto_approve
        # Per-kind override: when set, ONLY these checkpoint kinds auto-approve.
        # auto_approve=True is the "all kinds" shorthand. Lets the ranch hand
        # auto-approve plan_ready (already vetted at propose) while leaving
        # pre_push as a real human gate.
        self.auto_approve_kinds = auto_approve_kinds
        # One-shot mode: at a non-auto human gate, the session records its
        # checkpoint and EXITS cleanly (resumable) instead of blocking the tool
        # call. The Foreman resumes it via resume_run() once the operator
        # decides. Default off preserves the legacy in-session blocking path.
        self.one_shot = one_shot
        self.allowed_tools_override = allowed_tools_override
        self.budget_seconds = budget_seconds
        self.append_system_prompt_override = append_system_prompt_override
        self.run_id: int | None = None
        self.sdk_session_id: str | None = None

        # Checkpoint pause signalling
        self._awaiting_approval = False
        self._approval_ready = asyncio.Event()
        self._approval_result: str | None = None
        self._last_checkpoint_kind: str | None = None
        # Set when a one-shot run pauses at a human gate — drives the resumable
        # `paused_at_gate` exit reason in _finalize (vs. a terminal `stopped`).
        self._paused_at_gate = False

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
            # Auto-approve precedence: explicit per-kind list wins; else the
            # blanket `auto_approve` flag covers all kinds.
            kind_auto = self.auto_approve_kinds is not None and kind in self.auto_approve_kinds
            blanket_auto = self.auto_approve_kinds is None and self.auto_approve
            if kind_auto or blanket_auto:
                console.print(f"[dim](auto-approve fired for {kind})[/dim]")
                self._approval_result = "approved"
                self._approval_ready.set()
            elif self.one_shot:
                # One-shot: don't block in-session. Signal a clean stop so the
                # run exits at the gate (resumable); the Foreman resumes it once
                # the operator decides. The checkpoint hook returns a "paused —
                # do not proceed" instruction so the agent doesn't take the
                # gated action before the session winds down.
                console.print(
                    f"[dim](one-shot) paused at gate {kind} — exiting for operator "
                    f"review; resumable via `ranch resume`.[/dim]"
                )
                self._paused_at_gate = True
                self.stop_requested = True
            else:
                console.print(f"[dim]Waiting for: !approve  |  !reject <reason>  |  !stop  (gate: {kind})[/dim]")

    def requires_approval(self, kind: str) -> bool:
        return kind in APPROVAL_REQUIRED

    # ─── Dossier callback (called from PostToolUse hook) ─────────────

    async def on_state(self, dossier: RecordStateInput) -> None:
        """Persist a dossier snapshot. Non-blocking — purely informational."""
        with db_session() as db:
            row = Dossier(
                run_id=self.run_id,
                state=dossier.state,
                payload_json=dossier.model_dump_json(),
            )
            db.add(row)

        console.print(
            f"[dim cyan]→ dossier: state={dossier.state} just_did={dossier.just_did[:80]}[/dim cyan]"
        )

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

        # H8: every new run starts with a fresh judge budget
        reset_judge_budget()

        console.print(f"[bold cyan]Ranch run #{self.run_id} — {self.agent} / {self.ticket or 'ad-hoc'}[/bold cyan]")
        console.print("[dim]Commands: !note <text>  !approve  !reject <reason>  !stop[/dim]")
        console.print()

        # Use append_system_prompt (not system_prompt) so Claude Code's default
        # behavior — including auto-loading the worktree's CLAUDE.md — still
        # runs. Setting system_prompt= would suppress CLAUDE.md and the agent
        # would miss project conventions like "branch off develop, not main".
        effective_append = (
            self.append_system_prompt_override
            if self.append_system_prompt_override is not None
            else (SYSTEM_PROMPT_FREE if self.free else SYSTEM_PROMPT)
        )
        effective_tools = self.allowed_tools_override or DEFAULT_ALLOWED_TOOLS

        options = ClaudeCodeOptions(
            cwd=str(self.cwd),
            append_system_prompt=effective_append,
            mcp_servers={"ranch": ranch_mcp},
            allowed_tools=effective_tools,
            hooks={"PostToolUse": [
                make_checkpoint_hook(self),
                make_dossier_hook(self),
                make_judge_hook(self),
                make_block_hook(self),
            ]},
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
                budget_task = None
                if not self.auto_approve:
                    poll_task = asyncio.create_task(self._db_poll_loop(client))
                    if sys.stdin.isatty():
                        stdin_task = asyncio.create_task(self._stdin_loop())
                if self.budget_seconds is not None:
                    budget_task = asyncio.create_task(self._budget_watchdog())

                try:
                    await self._main_loop(client)
                finally:
                    for task in (stdin_task, poll_task, budget_task):
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
                    txt = block.text.strip()
                    if len(txt) >= 12:  # skip trivial fragments
                        self._emit_activity(txt, icon="💭")
                elif isinstance(block, ToolUseBlock):
                    console.print(f"\n[dim]→ {block.name}[/dim]")
                    self._emit_activity(block.name, detail=self._tool_detail(block), icon="→")
        console.file.flush() if hasattr(console, 'file') else None

    @staticmethod
    def _tool_detail(block) -> str | None:
        """A short human-readable summary of a tool call's target."""
        inp = getattr(block, "input", None) or {}
        for key in ("command", "file_path", "path", "pattern", "query", "url", "description"):
            v = inp.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:240]
        return None

    def _emit_activity(self, title: str, detail: str | None = None, icon: str = "·") -> None:
        """Tee an execute step into the event channel so the console can show a
        live activity feed (the agent's reasoning + tool calls). Best-effort —
        never let feed emission break the run."""
        if not self.run_id or not self.agent:
            return
        try:
            from ranch.events import emit_event
            emit_event(
                hand_name=self.agent, kind="activity", title=title[:240],
                detail=detail, ticket=self.ticket, icon=icon,
            )
        except Exception:
            pass

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

    async def _budget_watchdog(self) -> None:
        """If budget_seconds is set, signal stop after that many seconds.

        The main loop checks self.stop_requested between turns and exits
        cleanly — we don't cancel mid-tool-use, which keeps SDK state sane.
        """
        if self.budget_seconds is None:
            return
        await asyncio.sleep(self.budget_seconds)
        console.print(f"[yellow]Budget of {self.budget_seconds}s exhausted — requesting stop.[/yellow]")
        self.stop_requested = True
        # Unblock any in-flight checkpoint approval waiter so the loop can wind down
        self._approval_result = "stopped"
        self._approval_ready.set()

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
        if self._paused_at_gate and not error:
            # One-shot pause at a human gate — NOT terminal. Keep the run in
            # `needs_approval` so the Foreman can resume it on decision.
            exit_reason = "paused_at_gate"
            final_state = "needs_approval"
        else:
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

_HD_KINDS = {"plan_ready", "tests_green", "pre_push", "custom"}


async def resume_run(run_id: int, *, decision: str = "approved", reason: str | None = None) -> None:
    """Resume a run paused at a gate, injecting the operator's decision.

    The one-shot session exited at its gate; on resume we deliver the decision
    (approved/rejected) as the first message so the agent proceeds past the gate
    (or revises, on rejection). The resumed session stays one-shot, so a later
    gate pauses it again.
    """
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            console.print(f"[red]Run #{run_id} not found.[/red]")
            return
        if not run.sdk_session_id:
            console.print(f"[red]Run #{run_id} has no SDK session ID — cannot resume.[/red]")
            return

        # Most recent undecided checkpoint — the gate we're resuming from.
        last_cp = (
            db.query(Checkpoint)
            .filter_by(run_id=run_id, decision=None)
            .order_by(Checkpoint.id.desc())
            .first()
        )
        last_cp_kind = last_cp.kind if last_cp else None
        last_cp_summary = last_cp.summary if last_cp else None

        agent = run.agent
        ticket = run.ticket or ""
        brief = run.initial_prompt
        sdk_session_id = run.sdk_session_id
        cwd = Path(run.cwd)

    console.print(f"[cyan]Resuming run #{run_id} ({agent} / {ticket}) — {decision}[/cyan]")
    if last_cp_kind:
        console.print(Rule(f"Last checkpoint: {last_cp_kind}"))
        if last_cp_summary:
            console.print(last_cp_summary)

    orch = Orchestrator(agent=agent, cwd=cwd, ticket=ticket, brief=brief, one_shot=True)
    orch.run_id = run_id

    # Build the decision message + record it on the pending checkpoint.
    is_rejected = decision == "rejected"
    orch._record_decision("rejected" if is_rejected else "approved", reason or "")
    hd = HumanDecision(
        checkpoint_kind=last_cp_kind if last_cp_kind in _HD_KINDS else "custom",
        decision="rejected" if is_rejected else "approved",
        reason=reason,
        ticket=ticket or None,
    )
    resume_query = hd.to_prompt()

    options = ClaudeCodeOptions(
        cwd=str(cwd),
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"ranch": ranch_mcp},
        allowed_tools=[
            "Read", "Write", "Edit", "Bash", "Grep", "Glob",
            "mcp__ranch__record_checkpoint", "mcp__ranch__log_decision",
            "mcp__ranch__record_block",
        ],
        hooks={"PostToolUse": [
            make_checkpoint_hook(orch), make_dossier_hook(orch),
            make_judge_hook(orch), make_block_hook(orch),
        ]},
        permission_mode="acceptEdits",
        resume=sdk_session_id,
    )

    async with ClaudeSDKClient(options=options) as client:
        # Deliver the decision so the agent proceeds past (or revises at) the gate.
        await client.query(resume_query)
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
