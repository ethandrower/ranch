"""H5 — `ranch scope <ticket>`: build the agent's pre-flight context bundle.

A "scope" is everything the agent should know before it starts planning a
ticket: the ticket itself, its epic, sister tickets in the same epic,
any open PRs touching the epic, design links, and Confluence references.

The bundle is rendered to markdown and written to `~/.ranch/scopes/<key>.md`
so subsequent `ranch propose` / `ranch run` invocations can consume it
without re-querying.

Phase H5 of the Ranch hand epic (#70).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import RANCH_HOME
from .triage import (
    FIGMA_URL_RE,
    JiraClient,
    JiraConfig,
    JiraConfigError,
    JiraTicket,
)

SCOPES_DIR = RANCH_HOME / "scopes"
CONFLUENCE_URL_RE = re.compile(r"https?://[\w.-]+\.atlassian\.net/wiki/[^\s)>\]]+", re.IGNORECASE)
TICKET_KEY_RE = re.compile(r"\b([A-Z]+-\d+)\b")


# ─── Domain types ─────────────────────────────────────────────────


@dataclass
class PrSummary:
    """A normalized view of an open PR — agnostic to bb/gh shape."""

    id: str
    title: str
    branch: str
    url: str
    author: str | None = None
    referenced_ticket_keys: list[str] = field(default_factory=list)


@dataclass
class Scope:
    """The full pre-flight context bundle for a ticket."""

    ticket: JiraTicket
    epic: JiraTicket | None = None
    sisters: list[JiraTicket] = field(default_factory=list)
    related_prs: list[PrSummary] = field(default_factory=list)
    design_links: list[str] = field(default_factory=list)
    confluence_refs: list[str] = field(default_factory=list)

    @property
    def primary_key(self) -> str:
        return self.ticket.key

    def to_dict(self) -> dict:
        """JSON-safe representation for --json mode and ranch hand consumption."""
        return {
            "ticket": _ticket_to_dict(self.ticket),
            "epic": _ticket_to_dict(self.epic) if self.epic else None,
            "sisters": [_ticket_to_dict(s) for s in self.sisters],
            "related_prs": [
                {
                    "id": p.id, "title": p.title, "branch": p.branch, "url": p.url,
                    "author": p.author, "referenced_ticket_keys": p.referenced_ticket_keys,
                }
                for p in self.related_prs
            ],
            "design_links": self.design_links,
            "confluence_refs": self.confluence_refs,
        }


def _ticket_to_dict(t: JiraTicket) -> dict:
    return {
        "key": t.key,
        "summary": t.summary,
        "status": t.status,
        "priority": t.priority,
        "assignee_email": t.assignee_email,
        "labels": t.labels,
        "has_figma_link": t.has_figma_link,
        "created": t.created.isoformat(),
        "updated": t.updated.isoformat(),
        # description omitted from compact dict to keep JSON payloads bounded —
        # it's rendered in the markdown bundle instead
    }


# ─── PR discovery via bb / gh ──────────────────────────────────────


def _bb_pr_list_open(cwd: Path) -> list[dict]:
    """Return all open PRs as parsed JSON. Empty list if `bb` errors out
    (e.g. not a Bitbucket repo, auth issue) — PR discovery is best-effort."""
    try:
        result = subprocess.run(
            ["bb", "--json", "pr", "list", "--state", "OPEN", "--all"],
            cwd=str(cwd), capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout) or []
    except json.JSONDecodeError:
        return []


def _extract_referenced_tickets(text: str) -> list[str]:
    """Pull ticket keys (e.g. ECD-1234) out of arbitrary text."""
    seen: list[str] = []
    for m in TICKET_KEY_RE.findall(text or ""):
        if m not in seen:
            seen.append(m)
    return seen


def find_open_prs(cwd: Path, ticket_keys: Iterable[str]) -> list[PrSummary]:
    """Find open PRs whose branch name or title references any of the given keys.

    `cwd` should be inside a Bitbucket repo (the agent's worktree). Best-effort —
    if `bb` is missing or the repo isn't on Bitbucket, returns []. Future:
    layer in a GH backend the same way pr_backend.py does.
    """
    keys_lower = {k.lower() for k in ticket_keys}
    if not keys_lower:
        return []
    prs: list[PrSummary] = []
    for raw in _bb_pr_list_open(cwd):
        title = raw.get("title") or ""
        branch = (raw.get("source") or {}).get("branch", {}).get("name") or ""
        haystack = f"{branch} {title}".lower()
        referenced = _extract_referenced_tickets(branch + " " + title)
        if not any(k in haystack for k in keys_lower):
            continue
        url = (raw.get("links") or {}).get("html", {}).get("href") or ""
        author = (raw.get("author") or {}).get("display_name")
        prs.append(PrSummary(
            id=str(raw.get("id")),
            title=title,
            branch=branch,
            url=url,
            author=author,
            referenced_ticket_keys=referenced,
        ))
    return prs


# ─── Link extraction ───────────────────────────────────────────────


def _collect_text(ticket: JiraTicket) -> str:
    """Concatenate description + all comments for regex scanning."""
    return ticket.description + "\n" + "\n".join(ticket.comments)


def _extract_unique_urls(pattern: re.Pattern[str], *tickets: JiraTicket) -> list[str]:
    seen: list[str] = []
    for t in tickets:
        for m in pattern.findall(_collect_text(t)):
            if m not in seen:
                seen.append(m)
    return seen


# ─── Scope assembly ────────────────────────────────────────────────


def build_scope(ticket_key: str, *, jira: JiraClient, cwd: Path | None = None) -> Scope:
    """Assemble a Scope by querying Jira + (optionally) bb for PRs.

    `jira` is injected so tests can pass a stub; production callers use
    `JiraClient(JiraConfig.load())`.
    """
    ticket, epic_key = jira.get_ticket(ticket_key)

    epic: JiraTicket | None = None
    sisters: list[JiraTicket] = []
    if epic_key:
        epic, _ = jira.get_ticket(epic_key)
        sisters = [t for t in jira.list_sisters(epic_key) if t.key != ticket_key]

    # Design + Confluence links: drawn from the ticket itself and the epic.
    text_sources: list[JiraTicket] = [ticket]
    if epic:
        text_sources.append(epic)
    design_links = _extract_unique_urls(FIGMA_URL_RE, *text_sources)
    confluence_refs = _extract_unique_urls(CONFLUENCE_URL_RE, *text_sources)

    # PRs that mention this ticket or any sister.
    related_prs: list[PrSummary] = []
    if cwd is not None:
        all_keys = [ticket_key] + [s.key for s in sisters]
        if epic_key:
            all_keys.append(epic_key)
        related_prs = find_open_prs(cwd, all_keys)

    return Scope(
        ticket=ticket,
        epic=epic,
        sisters=sisters,
        related_prs=related_prs,
        design_links=design_links,
        confluence_refs=confluence_refs,
    )


# ─── Rendering ─────────────────────────────────────────────────────


def render_scope_markdown(scope: Scope) -> str:
    """Produce a human + agent-readable markdown bundle for the scope."""
    t = scope.ticket
    lines: list[str] = []
    lines.append(f"# {t.key} — {t.summary}")
    lines.append("")
    lines.append(f"- **Status**: {t.status or '?'}")
    if t.priority:
        lines.append(f"- **Priority**: {t.priority}")
    if t.assignee_email:
        lines.append(f"- **Assignee**: {t.assignee_email}")
    if t.labels:
        lines.append(f"- **Labels**: {', '.join(t.labels)}")
    lines.append(f"- **Updated**: {t.updated.isoformat()}")

    if scope.epic:
        e = scope.epic
        lines.append("")
        lines.append(f"## Epic — {e.key}: {e.summary}")
        lines.append(f"_Status: {e.status}_")
        if e.description.strip():
            lines.append("")
            lines.append(e.description.strip())

    if scope.sisters:
        lines.append("")
        lines.append(f"## Sister tickets ({len(scope.sisters)})")
        for s in scope.sisters:
            pri = f" ({s.priority})" if s.priority else ""
            lines.append(f"- `{s.key}` [{s.status}]{pri} — {s.summary}")

    if scope.related_prs:
        lines.append("")
        lines.append(f"## Open PRs in this epic ({len(scope.related_prs)})")
        for p in scope.related_prs:
            refs = f" — refs {', '.join(p.referenced_ticket_keys)}" if p.referenced_ticket_keys else ""
            lines.append(f"- #{p.id} `{p.branch}` — {p.title}{refs}")
            if p.url:
                lines.append(f"    {p.url}")

    if scope.design_links:
        lines.append("")
        lines.append("## Design references")
        for url in scope.design_links:
            lines.append(f"- {url}")

    if scope.confluence_refs:
        lines.append("")
        lines.append("## Confluence references")
        for url in scope.confluence_refs:
            lines.append(f"- {url}")

    if t.description.strip():
        lines.append("")
        lines.append("## Ticket description")
        lines.append("")
        lines.append(t.description.strip())

    return "\n".join(lines) + "\n"


# ─── Persistence ───────────────────────────────────────────────────


def scope_path(ticket_key: str) -> Path:
    """Where the saved scope for a ticket lives on disk."""
    return SCOPES_DIR / f"{ticket_key}.md"


def save_scope(scope: Scope) -> Path:
    """Write the markdown bundle to ~/.ranch/scopes/<key>.md, return the path."""
    SCOPES_DIR.mkdir(parents=True, exist_ok=True)
    path = scope_path(scope.primary_key)
    path.write_text(render_scope_markdown(scope))
    return path


def load_scope_markdown(ticket_key: str) -> str | None:
    """Return the saved markdown bundle for a ticket, or None if not saved.

    Used by `ranch propose` / `ranch run` to inject prior scope into the brief.
    """
    path = scope_path(ticket_key)
    if not path.exists():
        return None
    return path.read_text()
