"""Trinity-CLI–backed Jira client.

Replaces the raw `httpx`-against-`/rest/api/3/*` path in `triage.py` with
subprocess calls to the `trinity` CLI (`~/.local/bin/trinity`). Per the
operator's global CLAUDE.md: trinity is the preferred Atlassian path
because it handles auth + token rotation + per-repo Bitbucket tokens
without losing the session mid-flight (which the official MCP server
does).

Two upstream commands, one supplementary:

- `trinity --json jira search <JQL>` → bulk list, lightweight shape
  (no description, no comments). Used for the routing query.
- `trinity --json jira show <KEY>` → single issue, full shape (includes
  description). Called per-top-candidate to populate AC + figma
  signals for triage scoring.

Both return `JiraTicket` instances matching the existing shape so the
rest of ranch (`score_ticket`, `triage()`, hand pickup) doesn't change.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

from .triage import FIGMA_URL_RE, JiraTicket


TRINITY_BIN_ENV = "TRINITY_BIN"
DEFAULT_TRINITY = Path.home() / ".local" / "bin" / "trinity"


def trinity_path() -> str:
    """Resolve which trinity binary to use. Override via TRINITY_BIN env."""
    override = os.environ.get(TRINITY_BIN_ENV, "").strip()
    if override:
        return override
    if DEFAULT_TRINITY.exists():
        return str(DEFAULT_TRINITY)
    found = which("trinity")
    if not found:
        raise FileNotFoundError(
            "trinity CLI not found. Install it (see ~/.claude/CLAUDE.md) or "
            f"set {TRINITY_BIN_ENV} to its path."
        )
    return found


def _run_trinity(args: list[str], *, timeout: float = 30.0) -> dict:
    """Run `trinity --json <args>` and return the parsed JSON output.

    Raises CalledProcessError on non-zero exit. Bytes-mode capture so
    trinity's logging on stderr doesn't accidentally land in stdout.
    """
    bin_ = trinity_path()
    cmd = [bin_, "--json"] + args
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"trinity exited {proc.returncode}: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"trinity stdout not JSON: {e}\n--- stdout ---\n{proc.stdout[:500]}"
        )


# ─── Date parsing ──────────────────────────────────────────────────


def _parse_trinity_dt(s: str | None) -> datetime:
    """Trinity returns ISO timestamps with offsets like '+05:30' or '-0500'.
    Normalize to a UTC-aware datetime."""
    if not s:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    # Trinity is inconsistent — sometimes +HH:MM, sometimes +HHMM. Match
    # what triage._parse_jira_dt does.
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-5] + s[-5:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


# ─── Normalization ─────────────────────────────────────────────────


def _normalize_search_hit(hit: dict) -> JiraTicket:
    """Trinity's `jira search` lightweight shape:
    {key, id, summary, status, assignee, priority, type, labels, created, updated}
    """
    status = hit.get("status", "")
    # Trinity doesn't always emit status_category; infer from status_name
    # for the common cases. Triage falls through to "indeterminate" if unsure.
    status_lower = status.lower() if status else ""
    if "done" in status_lower or status_lower in ("closed", "resolved"):
        status_cat = "done"
    elif "to do" in status_lower or status_lower in ("open", "backlog", "new"):
        status_cat = "new"
    else:
        status_cat = "indeterminate"

    return JiraTicket(
        key=hit["key"],
        summary=hit.get("summary", ""),
        status=status,
        status_category=status_cat,
        priority=hit.get("priority"),
        created=_parse_trinity_dt(hit.get("created")),
        updated=_parse_trinity_dt(hit.get("updated")),
        description="",                  # search omits description
        comments=[],
        labels=hit.get("labels") or [],
        assignee_email=hit.get("assignee_email"),
        has_figma_link=False,            # can't tell without description
    )


def _normalize_show(issue: dict) -> JiraTicket:
    """Trinity's `jira show` full shape — includes description + nested
    assignee/reporter objects."""
    description = issue.get("description") or ""
    assignee = issue.get("assignee") or {}
    if isinstance(assignee, dict):
        email = assignee.get("email")
    else:
        email = None

    haystack = description
    has_figma = bool(FIGMA_URL_RE.search(haystack)) if description else False

    return JiraTicket(
        key=issue["key"],
        summary=issue.get("summary", ""),
        status=issue.get("status", ""),
        status_category=issue.get("status_category", "").lower() or "indeterminate",
        priority=issue.get("priority"),
        created=_parse_trinity_dt(issue.get("created")),
        updated=_parse_trinity_dt(issue.get("updated")),
        description=description,
        comments=[],  # trinity show doesn't include comments by default
        labels=issue.get("labels") or [],
        assignee_email=email,
        has_figma_link=has_figma,
    )


# ─── Client ───────────────────────────────────────────────────────


class TrinityJiraClient:
    """Subprocess-backed Jira client. Mirrors the interface of
    `ranch.triage.JiraClient` so triage / hand / dispatch swap in
    without behavior changes."""

    # Mirrors JiraClient's context-manager usage. Trinity owns its own
    # auth lifecycle — nothing to open/close here.
    def __enter__(self) -> "TrinityJiraClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def close(self) -> None:
        return None

    def list_for_hand(
        self,
        hand_name: str,
        *,
        assignee_account: str | None = None,
        project: str | None = None,
    ) -> list[JiraTicket]:
        """Per-hand routing query.

        assignee = <account> AND statusCategory != Done AND labels = "ranch-<hand>"
        """
        from .initiatives import route_label_for_hand
        route_label = route_label_for_hand(hand_name)
        assignee_clause = (
            f'assignee = "{assignee_account}"' if assignee_account else "assignee = currentUser()"
        )
        jql_parts = [
            assignee_clause,
            "statusCategory != Done",
            f'labels = "{route_label}"',
        ]
        if project:
            jql_parts.append(f"project = {project}")
        jql = " AND ".join(jql_parts) + " ORDER BY priority DESC, updated DESC"
        result = _run_trinity(["jira", "search", jql])
        return [_normalize_search_hit(h) for h in result.get("issues") or []]

    def list_assigned_to_me(self, *, project: str | None = None) -> list[JiraTicket]:
        """Unscoped operator-eyeball view."""
        jql_parts = ["assignee = currentUser()", "statusCategory != Done"]
        if project:
            jql_parts.append(f"project = {project}")
        jql = " AND ".join(jql_parts) + " ORDER BY priority DESC, updated DESC"
        result = _run_trinity(["jira", "search", jql])
        return [_normalize_search_hit(h) for h in result.get("issues") or []]

    def get_ticket(self, key: str) -> tuple[JiraTicket, str | None]:
        """Full-shape single ticket. Used by `ranch scope` to build the
        context bundle, and by dispatch when populating Run.initial_prompt.

        Returns (ticket, parent_epic_key). Trinity's `jira show` returns
        `epic_key` directly.
        """
        result = _run_trinity(["jira", "show", key])
        ticket = _normalize_show(result)
        return ticket, result.get("epic_key")

    def get_ticket_context(self, key: str) -> dict:
        """Description + comments + status fields, raw-ish JSON. Used by
        the side panel so the operator can read the ticket without
        flipping to Jira.

        Trinity's `jira show --comments` includes the comment thread on
        the same call — one subprocess hop, no extra round-trips.
        """
        return _run_trinity(["jira", "show", key, "--comments"])

    def list_transitions(self, key: str) -> dict:
        """Available Jira status transitions for an issue. Returns
        {current_status, transitions:[{id,name,to_status,to_category}]}."""
        return _run_trinity(["jira", "transitions", key])

    def transition(self, key: str, to_status: str, *, comment: str | None = None) -> str | None:
        """Move an issue to the named target status (matched case-insensitively
        against the transition's to_status or name). No-op if already there.
        Returns the resulting status name, or None if no matching transition is
        available from the current status."""
        info = self.list_transitions(key)
        current = (info.get("current_status") or "").strip()
        target = to_status.strip().lower()
        if current.lower() == target:
            return current  # already there
        match = next(
            (t for t in info.get("transitions") or []
             if (t.get("to_status") or "").strip().lower() == target
             or (t.get("name") or "").strip().lower() == target),
            None,
        )
        if not match:
            return None
        args = ["jira", "transition", key, "--id", str(match["id"])]
        if comment:
            args += ["--comment", comment]
        bin_ = trinity_path()
        proc = subprocess.run(
            [bin_, *args], capture_output=True, text=True, timeout=30.0, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"trinity transition exited {proc.returncode}: {proc.stderr.strip()}"
            )
        return match.get("to_status") or to_status

    def enrich_for_scoring(self, ticket: JiraTicket) -> JiraTicket:
        """Re-fetch a search-shape ticket with the full-show payload so
        triage scoring can see description + figma links. Use sparingly
        on the top-N candidates only — one extra trinity call per ticket.
        """
        full, _ = self.get_ticket(ticket.key)
        return full

    # Single-ticket form used by ranch scope (H5)
    def list_sisters(self, epic_key: str) -> list[JiraTicket]:
        """All tickets under the given epic (including Done) for context."""
        jql = f'parent = {epic_key} ORDER BY status ASC, updated DESC'
        result = _run_trinity(["jira", "search", jql])
        return [_normalize_search_hit(h) for h in result.get("issues") or []]
