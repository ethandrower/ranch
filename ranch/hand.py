"""H11 — Ranch hand: the daemon that composes the pilot loop primitives.

MVP scope (this file):

  loop forever:
    if I have an in-flight run → leave it alone, sleep
    else if my last run is parked-and-awaiting-approval → leave it alone, sleep
    else → triage → scope → propose → park (next cycle sees the parked dossier)

The hand picks one ticket at a time in this MVP. Multi-ticket juggling
(the "real" ranch hand model where it parks one and works another) is a
follow-up — adding it requires a richer state machine that's worth
landing on top of a working single-ticket loop first.

Stop semantics: write a sentinel file `~/.ranch/hands/<name>.stop` to
request a graceful stop after the current step. The hand polls for it
between cycles.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from rich.console import Console

from .config import AGENTS, RANCH_HOME, reload_agents
from .db import db_session
from .models import Dossier, Run

console = Console()

HANDS_DIR = RANCH_HOME / "hands"
HANDS_DIR.mkdir(parents=True, exist_ok=True)

# How long after a propose finishes do we still consider it "awaiting approval"
# rather than stale enough to triage past? Keeps the hand from churning a
# new triage on a recently-parked ticket the operator hasn't seen yet.
AWAITING_APPROVAL_WINDOW = timedelta(hours=24)


@dataclass
class HandStatus:
    """Snapshot of what a hand is doing right now — surfaced by `ranch hand status`."""

    name: str
    pid: int | None
    state: str  # "running" | "idle" | "stopped" | "missing"
    current_run_id: int | None
    current_ticket: str | None
    current_dossier_state: str | None
    detail: str = ""


# ─── Sentinel files for daemon lifecycle ───────────────────────────


def _pid_file(name: str) -> Path:
    return HANDS_DIR / f"{name}.pid"


def _stop_file(name: str) -> Path:
    return HANDS_DIR / f"{name}.stop"


def _write_pid(name: str, pid: int) -> None:
    _pid_file(name).write_text(str(pid))


def _clear_pid(name: str) -> None:
    p = _pid_file(name)
    if p.exists():
        p.unlink()


def request_stop(name: str) -> bool:
    """Touch the stop sentinel. Returns False if no pid file (hand isn't running)."""
    if not _pid_file(name).exists():
        return False
    _stop_file(name).touch()
    return True


def _read_pid(name: str) -> int | None:
    p = _pid_file(name)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    """Check if a pid is still running. signal(0) is the POSIX 'is alive' probe."""
    import os
    import signal as _signal
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


# ─── Run lookups ───────────────────────────────────────────────────


TERMINAL_RUN_STATES = {"completed", "stopped", "error"}


def _active_run_for(agent: str) -> Optional["Run"]:
    """Return the most-recent non-terminal Run for this agent, or None."""
    with db_session() as db:
        run = (
            db.query(Run)
            .filter(Run.agent == agent)
            .filter(~Run.state.in_(TERMINAL_RUN_STATES))
            .order_by(Run.started_at.desc())
            .first()
        )
        if not run:
            return None
        return _snapshot_run(run)


def _last_parked_run_for(agent: str) -> Optional["Run"]:
    """Return the most-recent terminal Run whose latest dossier is parked AND
    that finished within AWAITING_APPROVAL_WINDOW. None otherwise.

    Used to detect 'a propose finished recently, the operator still needs to
    look at it — don't start a new triage cycle on top of it.'
    """
    with db_session() as db:
        cutoff = datetime.now(timezone.utc) - AWAITING_APPROVAL_WINDOW
        # Most recent terminal run for this agent
        run = (
            db.query(Run)
            .filter(Run.agent == agent)
            .filter(Run.state.in_(TERMINAL_RUN_STATES))
            .filter(Run.ended_at >= cutoff)
            .order_by(Run.ended_at.desc())
            .first()
        )
        if not run:
            return None
        # Is the latest dossier parked?
        latest = (
            db.query(Dossier)
            .filter_by(run_id=run.id)
            .order_by(Dossier.created_at.desc())
            .first()
        )
        if not latest or latest.state != "parked":
            return None
        return _snapshot_run(run)


@dataclass
class _RunSnapshot:
    id: int
    agent: str
    ticket: str | None
    state: str


def _snapshot_run(run: "Run") -> _RunSnapshot:
    return _RunSnapshot(id=run.id, agent=run.agent, ticket=run.ticket, state=run.state)


# ─── The hand itself ───────────────────────────────────────────────


class RanchHand:
    """One virtual engineer. Polls for work and drives the pilot loop."""

    def __init__(
        self,
        name: str,
        cwd: Path,
        *,
        poll_seconds: float = 30.0,
        idle_log_freq_minutes: float = 30.0,
        jira_project: str | None = None,
        triage_fn: Optional[Callable[[str | None], list[str]]] = None,
        scope_fn: Optional[Callable[[str], None]] = None,
        propose_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.name = name
        self.cwd = cwd
        self.poll_seconds = poll_seconds
        self.idle_log_freq_minutes = idle_log_freq_minutes
        self.jira_project = jira_project

        # Injection seams for tests + offline mode. Default impls hit Jira /
        # invoke H5+H6; the validation harness injects synthetic versions.
        self.triage_fn = triage_fn or self._default_triage
        self.scope_fn = scope_fn or self._default_scope
        self.propose_fn = propose_fn or self._default_propose

        self.stop_requested = False
        self._last_idle_log = datetime.now(timezone.utc) - timedelta(days=1)

    # ─── Default backends ────────────────────────────────────────

    def _default_triage(self, project: str | None) -> list[str]:
        """Live triage via Jira. Returns ticket keys in ranked order."""
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
            console.print(f"[yellow]{self.name}: Jira not configured — {e}[/yellow]")
            return []
        in_flight = in_flight_ticket_keys_for_agent(self.name)
        with JiraClient(cfg) as client:
            tickets = client.list_assigned_to_me(project=project)
        ranked = triage(tickets, in_flight)
        return [t.key for t, _ in ranked]

    def _default_scope(self, ticket_key: str) -> None:
        """Build + save the scope bundle via H5."""
        from .scope import build_scope, save_scope
        from .triage import JiraClient, JiraConfig
        with JiraClient(JiraConfig.load()) as client:
            scope = build_scope(ticket_key, jira=client, cwd=self.cwd)
        save_scope(scope)

    async def _default_propose(self, ticket_key: str) -> None:
        """Run a propose session via H6 against this hand's worktree."""
        from .propose import (
            DEFAULT_PROPOSE_BUDGET_SECONDS,
            PROPOSE_ALLOWED_TOOLS,
            PROPOSE_SYSTEM_PROMPT,
            build_propose_brief,
            resolve_scope_markdown,
        )
        from .runner.orchestrator import Orchestrator

        scope_md = resolve_scope_markdown(ticket_key)
        brief = build_propose_brief(ticket_key, scope_md)
        orch = Orchestrator(
            agent=self.name,
            cwd=self.cwd,
            ticket=ticket_key,
            brief=brief,
            free=True,
            auto_approve=True,  # propose finishes cleanly; the parked dossier IS the gate
            allowed_tools_override=PROPOSE_ALLOWED_TOOLS,
            budget_seconds=DEFAULT_PROPOSE_BUDGET_SECONDS,
            append_system_prompt_override=PROPOSE_SYSTEM_PROMPT,
        )
        await orch.run()

    # ─── Loop ────────────────────────────────────────────────────

    def _check_stop_signal(self) -> bool:
        """True if the operator dropped a stop sentinel for us."""
        return _stop_file(self.name).exists()

    def _consume_stop_signal(self) -> None:
        sp = _stop_file(self.name)
        if sp.exists():
            sp.unlink()

    def _maybe_log_idle(self, msg: str) -> None:
        """Throttle the 'idle' log so we don't spam every poll cycle."""
        now = datetime.now(timezone.utc)
        if now - self._last_idle_log >= timedelta(minutes=self.idle_log_freq_minutes):
            console.print(f"[dim]{self.name}: {msg}[/dim]")
            self._last_idle_log = now

    async def run(self) -> None:
        """Main daemon loop. Returns when stop is requested."""
        import os
        _write_pid(self.name, os.getpid())
        console.print(
            f"[bold cyan]ranch hand '{self.name}' started[/bold cyan]  "
            f"[dim](cwd={self.cwd}, poll={self.poll_seconds}s)[/dim]"
        )
        try:
            while not self.stop_requested:
                if self._check_stop_signal():
                    console.print(f"[yellow]{self.name}: stop signal received — exiting cleanly.[/yellow]")
                    self._consume_stop_signal()
                    break

                # 1. Anything already in flight? Don't double-pick.
                active = _active_run_for(self.name)
                if active:
                    self._maybe_log_idle(
                        f"run #{active.id} ({active.ticket}) in flight — leaving alone"
                    )
                    await asyncio.sleep(self.poll_seconds)
                    continue

                # 2. Recently parked & still within the operator-review window?
                parked = _last_parked_run_for(self.name)
                if parked:
                    self._maybe_log_idle(
                        f"run #{parked.id} ({parked.ticket}) parked — awaiting human review"
                    )
                    await asyncio.sleep(self.poll_seconds)
                    continue

                # 3. Nothing in flight, nothing parked → triage for new work.
                console.print(f"[cyan]{self.name}: no active work — triaging...[/cyan]")
                try:
                    candidates = self.triage_fn(self.jira_project)
                except Exception as e:
                    console.print(f"[red]{self.name}: triage failed — {e}[/red]")
                    await asyncio.sleep(self.poll_seconds)
                    continue

                if not candidates:
                    self._maybe_log_idle("no viable tickets — staying idle")
                    await asyncio.sleep(self.poll_seconds)
                    continue

                ticket = candidates[0]
                console.print(f"[bold green]{self.name}: picked {ticket}[/bold green]")

                # 4. Scope it
                try:
                    self.scope_fn(ticket)
                except Exception as e:
                    console.print(f"[red]{self.name}: scope failed for {ticket} — {e}[/red]")
                    await asyncio.sleep(self.poll_seconds)
                    continue

                # 5. Propose
                console.print(f"[cyan]{self.name}: proposing plan for {ticket}...[/cyan]")
                try:
                    await self.propose_fn(ticket)
                except Exception as e:
                    console.print(f"[red]{self.name}: propose failed for {ticket} — {e}[/red]")
                    await asyncio.sleep(self.poll_seconds)
                    continue

                console.print(f"[green]{self.name}: {ticket} parked at propose — next cycle will wait for review[/green]")
                # Loop continues: next iteration sees the parked run + waits

        finally:
            _clear_pid(self.name)
            console.print(f"[dim]{self.name}: stopped.[/dim]")


# ─── Status surface ────────────────────────────────────────────────


def get_hand_status(name: str) -> HandStatus:
    """One-shot status snapshot for `ranch hand status`."""
    pid = _read_pid(name)
    if pid is None:
        return HandStatus(name=name, pid=None, state="stopped",
                           current_run_id=None, current_ticket=None,
                           current_dossier_state=None,
                           detail="not running")
    if not _pid_alive(pid):
        # Stale pid file — daemon crashed without cleaning up
        return HandStatus(name=name, pid=pid, state="missing",
                           current_run_id=None, current_ticket=None,
                           current_dossier_state=None,
                           detail=f"pid {pid} not alive (stale pidfile?)")

    active = _active_run_for(name)
    if active:
        with db_session() as db:
            latest = (
                db.query(Dossier)
                .filter_by(run_id=active.id)
                .order_by(Dossier.created_at.desc())
                .first()
            )
            dstate = latest.state if latest else None
        return HandStatus(
            name=name, pid=pid, state="running",
            current_run_id=active.id, current_ticket=active.ticket,
            current_dossier_state=dstate,
            detail=f"working on {active.ticket}",
        )

    parked = _last_parked_run_for(name)
    if parked:
        return HandStatus(
            name=name, pid=pid, state="running",
            current_run_id=parked.id, current_ticket=parked.ticket,
            current_dossier_state="parked",
            detail=f"parked on {parked.ticket} — awaiting review",
        )

    return HandStatus(name=name, pid=pid, state="running",
                       current_run_id=None, current_ticket=None,
                       current_dossier_state=None,
                       detail="idle (no work)")


def list_all_hand_statuses() -> list[HandStatus]:
    """Aggregate status for every agent known to the config + every active pid file."""
    reload_agents()
    seen: set[str] = set()
    out: list[HandStatus] = []
    for name in AGENTS:
        out.append(get_hand_status(name))
        seen.add(name)
    # Also pick up pid files for hands not in config (ad-hoc test hands etc.)
    for pid_path in HANDS_DIR.glob("*.pid"):
        nm = pid_path.stem
        if nm not in seen:
            out.append(get_hand_status(nm))
    return out
