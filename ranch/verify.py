"""Browser verification — the verify stage of the loop (proto-Inspector).

`ranch verify` spawns a FRESH-context skeptic session armed with exactly two
capabilities: the Playwright MCP browser (headless) and the `record_verdict`
reporting tool. It receives acceptance criteria + a target URL, judges each
criterion by ACTING in the real UI (click, type, observe — never by reading
code), captures a screenshot per criterion, and files one structured verdict.

Independence is the point: the verifier shares no context with the session
that wrote the code (no CLAUDE.md, no repo tools), so it can't inherit the
developer's self-persuasion. See docs/foreman.md §7 (generator/evaluator)
and §8.1 (the test rig).

The feedback loop: a failing verdict's summary + per-criterion evidence is
rendered by `VerdictInput.to_fix_brief()` into the brief for a fix-it dev
session (`--fix`), which edits the worktree; the operator then re-verifies.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from claude_code_sdk import ClaudeSDKClient, ClaudeCodeOptions, AssistantMessage, TextBlock, ToolUseBlock
from claude_code_sdk.types import HookContext
from claude_code_sdk import HookMatcher
from pydantic import ValidationError
from rich.console import Console
from rich.rule import Rule

from .config import RANCH_HOME
from .db import db_session, init_db
from .models import Verdict
from .runner.messages import VerdictInput
from .runner.tools import ranch_mcp

console = Console()

VERDICT_TOOL = "mcp__ranch__record_verdict"
DEFAULT_VERIFY_BUDGET_SECONDS = 420.0

VERIFY_SYSTEM_PROMPT = """\
You are an adversarial QA verifier. Your ONLY job is to judge whether a web app
satisfies its acceptance criteria — by ACTING in a real browser.

## Stance
Assume every criterion is FAILING until you prove it passes through direct
interaction. You are the check that can say no; do not extend goodwill.

## Method — for EACH criterion, in order:
1. Exercise it for real: navigate, click, type, read the live DOM. Never infer
   from source code, and never mark a criterion passed without having performed
   the interaction that proves it.
2. Probe one level beyond the literal words (e.g. "reset works" → also reset
   after several increments, and reset twice). Cheap robustness probes only —
   stay on-criterion.
3. Capture ONE screenshot at the decisive moment, named `crit<N>-pass.png` or
   `crit<N>-fail.png`.

## Reporting
When every criterion is judged, call `record_verdict` EXACTLY ONCE with:
- per-criterion: passed, evidence ("did X, observed Y" — concrete values), and
  the screenshot filename
- summary: on any failure, write it for the developer who must fix it —
  expected vs actual, the element involved, exact repro steps. Be precise, not
  polite.

## Hard rules
- Browser tools and `record_verdict` only. You cannot read or edit files.
- You do not fix anything. You do not suggest scope changes. You judge.
- If the page fails to load at all, fail every criterion with that evidence.
"""


def _artifacts_dir(ticket: str | None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    d = RANCH_HOME / "artifacts" / f"verify-{ticket or 'adhoc'}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _initial_prompt(url: str, criteria: list[str]) -> str:
    crit_md = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1))
    return (
        f"Target app: {url}\n\n"
        f"Acceptance criteria to verify:\n{crit_md}\n\n"
        "Open the target in the browser and judge every criterion per your method. "
        "Screenshots go to your configured output directory — just pass filenames. "
        "Finish by calling record_verdict exactly once."
    )


def make_verdict_hook(sink: dict) -> HookMatcher:
    """Capture + validate the record_verdict payload into `sink['verdict']`."""

    async def on_post_tool_use(input_data: dict, tool_use_id, context: HookContext) -> dict:
        if input_data.get("tool_name") != VERDICT_TOOL:
            return {}
        try:
            v = VerdictInput.model_validate(input_data.get("tool_input") or {})
        except ValidationError as exc:
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"record_verdict validation error — fix and re-file: {exc}",
            }}
        sink["verdict"] = v
        return {}

    return HookMatcher(matcher=VERDICT_TOOL, hooks=[on_post_tool_use])


async def run_verify(
    *,
    url: str,
    criteria: list[str],
    ticket: str | None = None,
    run_id: int | None = None,
    cwd: Path | None = None,
    budget_seconds: float = DEFAULT_VERIFY_BUDGET_SECONDS,
    storage_state: str | None = None,
) -> VerdictInput | None:
    """Run one verification session. Returns the verdict (also persisted), or
    None if the session ended without filing one."""
    init_db()
    artifacts = _artifacts_dir(ticket)

    pw_args = ["@playwright/mcp@latest", "--headless", "--isolated", "--output-dir", str(artifacts)]
    if storage_state:
        pw_args += ["--storage-state", storage_state]

    options = ClaudeCodeOptions(
        cwd=str(cwd or artifacts),
        system_prompt=VERIFY_SYSTEM_PROMPT,   # deliberate: NO CLAUDE.md, no repo context — independence
        mcp_servers={
            "ranch": ranch_mcp,
            "playwright": {"command": "npx", "args": pw_args},
        },
        allowed_tools=["mcp__playwright", VERDICT_TOOL],
        hooks={"PostToolUse": [make_verdict_hook(sink := {})]},
        permission_mode="acceptEdits",
    )

    console.print(Rule(f"[bold magenta]VERIFY — {ticket or 'ad-hoc'} @ {url}"))
    console.print(f"[dim]{len(criteria)} criteria · artifacts → {artifacts}[/dim]")

    from claude_code_sdk._errors import MessageParseError

    try:
        async with asyncio.timeout(budget_seconds):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(_initial_prompt(url, criteria))
                while True:
                    try:
                        async for msg in client.receive_response():
                            if isinstance(msg, AssistantMessage):
                                for block in msg.content:
                                    if isinstance(block, TextBlock) and block.text.strip():
                                        console.print(block.text, end="", highlight=False)
                                    elif isinstance(block, ToolUseBlock):
                                        console.print(f"\n[dim]→ {block.name}[/dim]")
                    except MessageParseError as e:
                        # Same wrinkle the orchestrator handles: this SDK version
                        # can't parse rate_limit_event notifications — skip and drain on.
                        if "rate_limit" in str(e).lower():
                            continue
                        raise
                    break
    except TimeoutError:
        console.print(f"\n[yellow]Verify budget of {budget_seconds}s exhausted.[/yellow]")

    verdict: VerdictInput | None = sink.get("verdict")

    with db_session() as db:
        db.add(Verdict(
            run_id=run_id,
            ticket=ticket,
            target_url=url,
            overall_pass=int(bool(verdict and verdict.overall_pass)),
            payload_json=verdict.model_dump_json() if verdict else json.dumps({"error": "no verdict filed"}),
            artifacts_dir=str(artifacts),
        ))

    console.print()
    if verdict is None:
        console.print("[red]✗ Session ended WITHOUT filing a verdict — treat as failed.[/red]")
        return None

    icon = "[green]✓ PASS[/green]" if verdict.overall_pass else "[red]✗ FAIL[/red]"
    console.print(Rule(f"VERDICT: {icon}"))
    for c in verdict.criteria:
        mark = "[green]✓[/green]" if c.passed else "[red]✗[/red]"
        console.print(f" {mark} {c.criterion}")
        console.print(f"    [dim]{c.evidence}[/dim]")
        if c.screenshot:
            console.print(f"    [dim cyan]📷 {artifacts / c.screenshot}[/dim cyan]")
    console.print(f"\n[bold]Summary:[/bold] {verdict.summary}")
    return verdict


async def run_fix(verdict: VerdictInput, *, url: str, cwd: Path,
                  ticket: str | None, artifacts_dir: str | None,
                  budget_seconds: float = 600.0) -> None:
    """Spawn a dev session whose brief IS the failing verdict (the feedback loop)."""
    from .runner.orchestrator import Orchestrator

    brief = verdict.to_fix_brief(url, artifacts_dir)
    console.print(Rule("[bold yellow]FIX SESSION — verdict feedback → development"))
    orch = Orchestrator(
        agent="verify-fix",
        cwd=cwd,
        ticket=ticket,
        brief=brief,
        free=True,             # small targeted fix — no plan→push ceremony
        auto_approve=True,
        budget_seconds=budget_seconds,
    )
    await orch.run()


# ─── CLI ─────────────────────────────────────────────────────────────

import click


@click.command("verify")
@click.option("--url", required=True, help="URL of the running app to verify against")
@click.option("--criterion", "-c", "criteria", multiple=True,
              help="An acceptance criterion (repeatable). If omitted, pulled from --run's latest dossier.")
@click.option("--run", "run_id", type=int, default=None,
              help="Run whose acceptance criteria to verify (names come from its dossier)")
@click.option("--ticket", default=None, help="Ticket label for artifacts/verdict rows")
@click.option("--cwd", type=click.Path(exists=True, file_okay=False), default=None,
              help="Worktree (used by --fix to apply changes)")
@click.option("--budget", type=float, default=DEFAULT_VERIFY_BUDGET_SECONDS, show_default=True)
@click.option("--storage-state", default=None,
              help="Playwright storageState JSON for pre-authenticated sessions")
@click.option("--fix", is_flag=True,
              help="On failure, spawn a dev session briefed with the verdict, in --cwd")
def verify_cmd(url, criteria, run_id, ticket, cwd, budget, storage_state, fix):
    """Verify acceptance criteria by driving the real UI in a headless browser."""
    criteria = list(criteria)
    if not criteria and run_id is not None:
        from .models import Dossier
        init_db()
        with db_session() as db:
            d = (db.query(Dossier).filter_by(run_id=run_id)
                   .order_by(Dossier.id.desc()).first())
            if d:
                payload = json.loads(d.payload_json)
                criteria = [c.get("name") or c.get("criterion") or str(c)
                            for c in payload.get("acceptance") or []]
    if not criteria:
        raise click.UsageError("No criteria: pass -c/--criterion or --run with an acceptance-bearing dossier.")

    async def _main():
        verdict = await run_verify(
            url=url, criteria=criteria, ticket=ticket, run_id=run_id,
            cwd=Path(cwd) if cwd else None,
            budget_seconds=budget, storage_state=storage_state,
        )
        failed = verdict is None or not verdict.overall_pass
        if failed and fix:
            if not cwd:
                raise click.UsageError("--fix requires --cwd (the worktree to fix in).")
            if verdict is not None:
                with db_session() as db:
                    row = db.query(Verdict).order_by(Verdict.id.desc()).first()
                    adir = row.artifacts_dir if row else None
                await run_fix(verdict, url=url, cwd=Path(cwd), ticket=ticket,
                              artifacts_dir=adir)
                console.print("\n[bold]Fix session done — re-run verify to confirm.[/bold]")
        raise SystemExit(0 if not failed else 1)

    asyncio.run(_main())
