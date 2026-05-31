"""H10 — `ranch pr draft <run_id>` / `ranch pr open <run_id>`.

Gathers structured artifacts produced during a run (dossier, plan,
acceptance results, files touched, branch, ticket) and renders them
into a PR title + body. Operator can preview with `draft` or actually
fire `bb pr create --draft` (or `gh pr create --draft`) with `open`.

Per the H10 spec, the body sections are derived from data we already
collected through H1+H5+H6+H8, not from rerun analysis. That keeps PR
creation cheap and deterministic.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .db import db_session
from .models import Dossier, Run
from .scope import load_scope_markdown

# Same ticket-key shape used elsewhere
TICKET_KEY_RE = re.compile(r"\b([A-Z]+-\d+)\b")


# ─── Domain ────────────────────────────────────────────────────────


@dataclass
class RunArtifacts:
    """Everything we collected during a run that's PR-body-worthy."""

    ticket: str | None
    agent: str
    branch_name: str | None
    cwd: str
    # Dossier-derived
    final_state: str | None = None
    final_just_did: str = ""
    final_details: str = ""
    plan_steps: list[dict] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    acceptance: list[dict] = field(default_factory=list)
    # Git-derived
    diff_stat: str = ""
    # Optional decorations the operator provides at draft time
    figma_url: str | None = None
    jira_base_url: str | None = None


@dataclass
class PRDraft:
    """Title + body for the PR."""

    title: str
    body: str


# ─── Gather ────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path | str, timeout: float = 15.0) -> str:
    """Run a git command, return stdout (empty string on failure)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def gather_run_artifacts(run_id: int) -> RunArtifacts:
    """Pull every PR-body-relevant artifact from the DB + worktree git state."""
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            raise ValueError(f"Run #{run_id} not found")
        # Snapshot before exiting the session — Run is detached after.
        artifacts = RunArtifacts(
            ticket=run.ticket, agent=run.agent,
            branch_name=run.branch_name, cwd=run.cwd,
        )
        latest = (
            db.query(Dossier)
            .filter_by(run_id=run_id)
            .order_by(Dossier.created_at.desc())
            .first()
        )
        if latest:
            try:
                payload = json.loads(latest.payload_json)
            except (json.JSONDecodeError, TypeError):
                payload = {}
            artifacts.final_state = payload.get("state")
            artifacts.final_just_did = payload.get("just_did", "")
            artifacts.final_details = payload.get("details", "") or ""
            artifacts.plan_steps = payload.get("plan", []) or []
            artifacts.files_touched = payload.get("files_touched", []) or []
            artifacts.acceptance = payload.get("acceptance", []) or []

    # Diff stat against develop (preferred) or main
    cwd = Path(artifacts.cwd) if artifacts.cwd else Path.cwd()
    for base in ("origin/develop", "develop", "origin/main", "main"):
        stat = _git(["diff", "--stat", f"{base}...HEAD"], cwd)
        if stat:
            artifacts.diff_stat = stat
            break
    if not artifacts.diff_stat:
        # Fall back to "since last common ancestor with HEAD~10" — best-effort
        artifacts.diff_stat = _git(["diff", "--stat", "HEAD~10...HEAD"], cwd)

    return artifacts


# ─── Render ────────────────────────────────────────────────────────


def _strip_section(text: str, header_pattern: str) -> str:
    """Extract a single section from the agent's `details` markdown.

    Looks for `## <header>` (or `### <header>`) and returns the content
    up to the next same-or-higher-level header. Empty string if not found.

    The propose system prompt instructs the agent to use Summary / Plan /
    Acceptance criteria / Complexity / Risks headers — we mine those.
    """
    # Wrap header_pattern in a non-capturing group AND name the body group
    # so internal capture groups in `header_pattern` (e.g. `( / open questions)?`)
    # don't shift positional group indices.
    pat = re.compile(
        rf"^#{{1,4}}\s*\**(?:{header_pattern})\**\s*$(?P<body>.*?)(?=^#{{1,4}}\s|\Z)",
        re.MULTILINE | re.IGNORECASE | re.DOTALL,
    )
    m = pat.search(text)
    if not m:
        return ""
    return m.group("body").strip()


def _format_plan_progress(plan_steps: list[dict]) -> str:
    if not plan_steps:
        return ""
    lines = ["### Plan progress"]
    for step in plan_steps:
        status = step.get("status", "pending")
        mark = {"done": "[x]", "in_progress": "[~]", "pending": "[ ]"}.get(status, "[ ]")
        text = step.get("step", "")
        lines.append(f"- {mark} {text}")
        if step.get("notes"):
            lines.append(f"      _{step['notes']}_")
    return "\n".join(lines)


def _format_acceptance(acceptance: list[dict]) -> str:
    if not acceptance:
        return ""
    lines = ["### Acceptance checks"]
    for c in acceptance:
        kind = c.get("kind", "?")
        name = c.get("name", "(unnamed)")
        detail = ""
        if kind in ("unit_test", "script") and c.get("cmd"):
            detail = f" — `{c['cmd']}`"
        elif kind == "http" and c.get("url"):
            detail = f" — `GET {c['url']}`"
        lines.append(f"- **[{kind}]** {name}{detail}")
    return "\n".join(lines)


def _format_files_touched(files: list[str], diff_stat: str) -> str:
    """Prefer git diff --stat (authoritative) over the agent's reported list."""
    if diff_stat:
        return "```\n" + diff_stat.strip() + "\n```"
    if files:
        return "\n".join(f"- `{f}`" for f in files)
    return "_(no diff captured)_"


def derive_title(artifacts: RunArtifacts) -> str:
    """Title format: `<TICKET>: <summary line>`.

    Pulled from the dossier's just_did first sentence, with a sensible
    fallback. Ticket prefix is added if not already present.
    """
    summary_source = ""
    # Prefer the Summary section of `details` if present
    if artifacts.final_details:
        summary_section = _strip_section(artifacts.final_details, "summary")
        if summary_section:
            summary_source = summary_section.splitlines()[0]
    if not summary_source:
        summary_source = artifacts.final_just_did

    # First sentence, capped
    summary_source = summary_source.strip()
    # Strip leading "**bold**" or trailing period
    summary_source = re.sub(r"\*+", "", summary_source).strip().rstrip(".")
    first_sentence = re.split(r"(?<=[.!?])\s", summary_source, maxsplit=1)[0]
    if len(first_sentence) > 72:
        first_sentence = first_sentence[:69].rstrip() + "..."

    if artifacts.ticket and not artifacts.ticket.lower() in first_sentence.lower():
        return f"{artifacts.ticket}: {first_sentence}"
    return first_sentence or (artifacts.ticket or "PR")


def render_pr_body(artifacts: RunArtifacts) -> str:
    """Build the PR body from accumulated structured data."""
    sections: list[str] = []

    # Summary — agent's narration
    summary = _strip_section(artifacts.final_details, "summary")
    if summary:
        sections.append(f"## Summary\n\n{summary}")
    elif artifacts.final_just_did:
        sections.append(f"## Summary\n\n{artifacts.final_just_did}")

    # Plan progress
    plan_block = _format_plan_progress(artifacts.plan_steps)
    if plan_block:
        sections.append(plan_block)

    # Changes — prefer authoritative git diff stat
    changes = _format_files_touched(artifacts.files_touched, artifacts.diff_stat)
    sections.append(f"## Changes\n\n{changes}")

    # Testing — agent's `acceptance` is the machine-verifiable contract;
    # the prose AC from `details` is the human-readable version.
    testing_lines = ["## Testing"]
    accept_block = _format_acceptance(artifacts.acceptance)
    if accept_block:
        testing_lines.append("")
        testing_lines.append(accept_block)
    ac_prose = _strip_section(artifacts.final_details, "acceptance criteria")
    if ac_prose:
        testing_lines.append("")
        testing_lines.append("### Manual verification")
        testing_lines.append("")
        testing_lines.append(ac_prose)
    if len(testing_lines) > 1:
        sections.append("\n".join(testing_lines))

    # Risks / open questions
    risks = _strip_section(artifacts.final_details, r"risks( / open questions)?")
    if risks:
        sections.append(f"## Open questions / risks\n\n{risks}")

    # Linked
    linked_lines = []
    if artifacts.ticket and artifacts.jira_base_url:
        linked_lines.append(f"- Jira: {artifacts.jira_base_url.rstrip('/')}/browse/{artifacts.ticket}")
    elif artifacts.ticket:
        linked_lines.append(f"- Jira: `{artifacts.ticket}`")
    if artifacts.figma_url:
        linked_lines.append(f"- Design: {artifacts.figma_url}")
    if linked_lines:
        sections.append("## Linked\n\n" + "\n".join(linked_lines))

    # Footer attribution
    sections.append(
        "---\n_PR drafted from ranch run "
        f"on branch `{artifacts.branch_name or '?'}` by agent `{artifacts.agent}`._"
    )

    return "\n\n".join(sections).strip() + "\n"


def render_draft(run_id: int, *, figma_url: str | None = None,
                  jira_base_url: str | None = None) -> tuple[PRDraft, RunArtifacts]:
    """Convenience: gather + render in one shot."""
    artifacts = gather_run_artifacts(run_id)
    artifacts.figma_url = figma_url
    artifacts.jira_base_url = jira_base_url
    return PRDraft(title=derive_title(artifacts),
                    body=render_pr_body(artifacts)), artifacts
