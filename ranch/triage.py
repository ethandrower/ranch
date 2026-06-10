"""H4 — `ranch triage`: rank assigned Jira tickets by viability.

Pure logic + a thin Jira REST client behind an interface so tests can
mock without touching the network.

Phase H4 of the Ranch hand epic (#70). Used by the hand's scheduler (#81)
to pick what to work on next.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol

import httpx
import tomllib

from .config import CONFIG_FILE

FIGMA_URL_RE = re.compile(r"https?://(?:www\.)?figma\.com/[^\s)>\]]+", re.IGNORECASE)
TICKET_ID_RE = re.compile(r"[A-Z]+-\d+")

# Status categories Jira Cloud exposes. Atlassian groups all workflow statuses
# into one of these three: "new" (TODO), "indeterminate" (in progress / review),
# "done" (terminal).
_VIABLE_CATEGORIES = {"new", "indeterminate"}

# Priority → points. Matches the default Jira priority ladder; unknown values
# fall through to 0.
_PRIORITY_SCORE = {
    "highest": 15,
    "high": 10,
    "medium": 5,
    "low": 0,
    "lowest": -5,
}


# ─── Config ────────────────────────────────────────────────────────


@dataclass
class JiraConfig:
    """Jira connection config, loaded from ~/.ranch/config.toml + env."""

    url: str  # e.g. "https://citemed.atlassian.net"
    email: str
    api_token: str  # from RANCH_JIRA_API_TOKEN env var
    # Phase A v2 — the Jira user whose tickets the ranch hands pick up.
    # Tickets must be assigned to this account AND carry a `ranch-<hand>`
    # label to be routed to a specific hand. Defaults to `email` if unset
    # so single-operator deploys work without extra config.
    hand_account: str = ""

    @classmethod
    def load(cls) -> "JiraConfig":
        if not CONFIG_FILE.exists():
            raise JiraConfigError(
                f"No config at {CONFIG_FILE}. Run `ranch init` first."
            )
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        jira = data.get("jira")
        if not jira:
            raise JiraConfigError(
                "No [jira] section in ~/.ranch/config.toml. Add:\n"
                "    [jira]\n"
                '    url = "https://yourorg.atlassian.net"\n'
                '    email = "you@example.com"\n'
                "Then set RANCH_JIRA_API_TOKEN to your API token "
                "(create one at https://id.atlassian.com/manage-profile/security/api-tokens)."
            )
        token = os.environ.get("RANCH_JIRA_API_TOKEN", "").strip()
        if not token:
            raise JiraConfigError(
                "RANCH_JIRA_API_TOKEN env var is unset or empty. "
                "Create a token at https://id.atlassian.com/manage-profile/security/api-tokens"
            )
        url = str(jira.get("url", "")).rstrip("/")
        email = str(jira.get("email", "")).strip()
        if not url or not email:
            raise JiraConfigError("[jira] section is missing `url` or `email`.")
        # hand_account precedence: env > config > fall back to email
        hand_account = (
            os.environ.get("RANCH_HAND_ACCOUNT", "").strip()
            or str(jira.get("hand_account", "")).strip()
            or email
        )
        return cls(url=url, email=email, api_token=token, hand_account=hand_account)


class JiraConfigError(RuntimeError):
    """Raised when Jira config is missing or invalid. Surface to the user."""


# ─── Domain types ──────────────────────────────────────────────────


@dataclass
class JiraTicket:
    """A normalized view of a Jira issue — agnostic to API shape so scoring
    logic doesn't have to care about ADF / field structures."""

    key: str
    summary: str
    status: str  # workflow status name e.g. "In Progress"
    status_category: str  # "new" | "indeterminate" | "done"
    priority: str | None  # "High" | None
    created: datetime
    updated: datetime
    description: str  # plain-text extraction
    comments: list[str] = field(default_factory=list)  # plain-text bodies
    labels: list[str] = field(default_factory=list)
    assignee_email: str | None = None
    has_figma_link: bool = False  # derived during extraction

    @property
    def age_days(self) -> float:
        return (datetime.now(timezone.utc) - self.created).total_seconds() / 86400

    @property
    def initiative(self) -> str | None:
        """The `ranch-initiative:<key>` label value, or None if absent."""
        from .initiatives import extract_initiative
        return extract_initiative(self.labels)


@dataclass
class ViabilityScore:
    """Total + per-axis breakdown. Higher is more viable."""

    total: float
    status: float
    design_present: float
    ac_clarity: float
    priority: float
    age: float
    in_flight_penalty: float = 0.0  # -1000 means "drop entirely"

    @property
    def dropped(self) -> bool:
        return self.in_flight_penalty <= -1000


# ─── Scoring (pure functions) ──────────────────────────────────────


def _has_acceptance_criteria(text: str) -> bool:
    """Heuristic — looks for explicit AC sections or numbered should/must lists."""
    lowered = text.lower()
    if "acceptance criteria" in lowered or re.search(r"\bac\s*:", lowered):
        return True
    # numbered "should/must" lines (e.g. "1. user should see X")
    return bool(re.search(r"^\s*\d+[.)]\s.+\b(should|must)\b", text, re.IGNORECASE | re.MULTILINE))


def score_ticket(
    ticket: JiraTicket,
    in_flight_ticket_keys: set[str],
) -> ViabilityScore:
    """Score a single ticket. Pure function — no I/O.

    Axes:
    - status: +30 for in-progress / ready-for-dev, +20 for to-do, 0 if blocked
    - design_present: +20 if a figma link is found anywhere
    - ac_clarity: +15 if description has explicit acceptance criteria
    - priority: per ladder (Highest +15 → Lowest -5)
    - age: log-bounded, max +10
    - in_flight_penalty: -1000 if this agent already has a non-terminal run on it
    """
    if ticket.key in in_flight_ticket_keys:
        return ViabilityScore(
            total=-1000.0,
            status=0, design_present=0, ac_clarity=0, priority=0, age=0,
            in_flight_penalty=-1000.0,
        )

    # Status — penalize anything that suggests the ticket isn't actionable
    status_lower = ticket.status.lower()
    if any(b in status_lower for b in ("blocked", "waiting", "on hold", "needs design", "needs info")):
        status_score = 0.0
    elif ticket.status_category == "indeterminate":  # in-progress, in-review, etc.
        status_score = 30.0
    elif ticket.status_category == "new":  # to-do, open, backlog
        status_score = 20.0
    else:
        status_score = 0.0

    design_score = 20.0 if ticket.has_figma_link else 0.0
    ac_score = 15.0 if _has_acceptance_criteria(ticket.description) else 0.0

    priority_score = _PRIORITY_SCORE.get((ticket.priority or "").lower(), 0)

    # Age — slight boost as tickets get older to prevent forever-pending.
    # Bounded: 7 days → ~5pts, 30 days → ~10pts.
    age = ticket.age_days
    if age <= 0:
        age_score = 0.0
    else:
        # log-ish curve: 1d=2, 7d=5, 30d=10
        import math
        age_score = min(10.0, math.log1p(age) * 2.5)

    total = status_score + design_score + ac_score + priority_score + age_score
    return ViabilityScore(
        total=total,
        status=status_score,
        design_present=design_score,
        ac_clarity=ac_score,
        priority=priority_score,
        age=age_score,
    )


def triage(
    tickets: Iterable[JiraTicket],
    in_flight_ticket_keys: set[str],
) -> list[tuple[JiraTicket, ViabilityScore]]:
    """Rank tickets by viability, descending. Dropped tickets (in-flight) excluded.

    Pure function — no I/O. Caller fetches tickets from somewhere (real Jira or
    a stub) and feeds them in.
    """
    scored = [(t, score_ticket(t, in_flight_ticket_keys)) for t in tickets]
    scored = [s for s in scored if not s[1].dropped]
    scored.sort(key=lambda pair: pair[1].total, reverse=True)
    return scored


# ─── Jira client ───────────────────────────────────────────────────


def _adf_to_text(node) -> str:
    """Walk an Atlassian Document Format tree and extract plain text.

    Jira Cloud returns description as ADF JSON. We don't render formatting —
    we just want the text for keyword/regex scanning.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_adf_to_text(n) for n in node)
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "text":
            return node.get("text", "")
        # Recurse into children
        return _adf_to_text(node.get("content", []))
    return ""


def _normalize_ticket(issue: dict) -> JiraTicket:
    """Map a Jira REST `issue` dict to our JiraTicket dataclass."""
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    status_cat = (status.get("statusCategory") or {}).get("key", "")
    priority = fields.get("priority") or {}
    assignee = fields.get("assignee") or {}

    description = _adf_to_text(fields.get("description"))
    comments_block = (fields.get("comment") or {}).get("comments") or []
    comments = [_adf_to_text(c.get("body")) for c in comments_block]

    haystack = description + "\n" + "\n".join(comments)
    has_figma = bool(FIGMA_URL_RE.search(haystack))

    created = _parse_jira_dt(fields.get("created"))
    updated = _parse_jira_dt(fields.get("updated"))

    return JiraTicket(
        key=issue["key"],
        summary=fields.get("summary", ""),
        status=status.get("name", ""),
        status_category=status_cat,
        priority=priority.get("name") if priority else None,
        created=created,
        updated=updated,
        description=description,
        comments=comments,
        labels=fields.get("labels") or [],
        assignee_email=assignee.get("emailAddress"),
        has_figma_link=has_figma,
    )


def _parse_jira_dt(s: str | None) -> datetime:
    """Parse Jira's ISO-with-offset timestamps. Falls back to epoch on missing."""
    if not s:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    # Jira returns e.g. "2026-04-18T12:34:56.789+0000" — datetime.fromisoformat
    # accepts +00:00 but not +0000, so normalize.
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-5] + s[-5:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s).astimezone(timezone.utc)


class JiraSource(Protocol):
    """Minimal interface — lets tests swap in a fake source."""

    def list_assigned_to_me(self, *, project: str | None = None) -> list[JiraTicket]: ...


class JiraClient:
    """Live Jira REST client. Auth via Basic (email + API token)."""

    def __init__(self, config: JiraConfig, *, timeout: float = 15.0):
        self._cfg = config
        self._client = httpx.Client(
            base_url=config.url,
            auth=(config.email, config.api_token),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # Jira Cloud's /rest/api/3/search returns issues with a configurable
    # field projection. We use one canonical projection across all queries so
    # _normalize_ticket has consistent input shape.
    _FIELDS = "summary,status,priority,created,updated,description,comment,labels,assignee,parent"

    def list_assigned_to_me(self, *, project: str | None = None) -> list[JiraTicket]:
        """All open tickets assigned to the authenticated user. Unscoped —
        used for the operator-eyeball view (`ranch triage --all`), not for
        per-hand routing."""
        jql_parts = ["assignee = currentUser()", "statusCategory != Done"]
        if project:
            jql_parts.append(f"project = {project}")
        jql = " AND ".join(jql_parts) + " ORDER BY priority DESC, updated DESC"
        return self._search(jql)

    def list_for_hand(
        self,
        hand_name: str,
        *,
        assignee_account: str | None = None,
        project: str | None = None,
    ) -> list[JiraTicket]:
        """Per-hand routing query (Phase A v2).

        Returns tickets that meet ALL of:
        - assigned to `assignee_account` (the ranch-hand user; defaults
          to `currentUser()` for single-operator dev),
        - statusCategory != Done,
        - labels include `ranch-<hand_name>`.

        Operators put a single ticket in front of a specific hand by
        adding the routing label; the assignee predicate keeps random
        team tickets out.
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
        return self._search(jql)

    def get_ticket(self, key: str) -> tuple[JiraTicket, str | None]:
        """Fetch one ticket. Returns (ticket, parent_epic_key) — parent_epic_key
        is None if this ticket isn't under an epic.

        Used by `ranch scope <ticket>` to build the context bundle (H5).
        """
        resp = self._client.get(f"/rest/api/3/issue/{key}", params={"fields": self._FIELDS})
        resp.raise_for_status()
        issue = resp.json()
        ticket = _normalize_ticket(issue)
        parent = (issue.get("fields") or {}).get("parent")
        parent_key = parent.get("key") if parent else None
        return ticket, parent_key

    def list_sisters(self, epic_key: str) -> list[JiraTicket]:
        """All tickets under the given epic, including any in Done state.

        Used by `ranch scope` so the agent can see what else has shipped or
        is in flight for this epic.
        """
        jql = f'parent = {epic_key} ORDER BY status ASC, updated DESC'
        return self._search(jql)

    def _search(self, jql: str) -> list[JiraTicket]:
        params = {"jql": jql, "fields": self._FIELDS, "maxResults": 100}
        resp = self._client.get("/rest/api/3/search", params=params)
        resp.raise_for_status()
        return [_normalize_ticket(issue) for issue in (resp.json() or {}).get("issues", [])]


# ─── In-flight detection (from ranch's own DB) ─────────────────────


def in_flight_ticket_keys_for_agent(agent: str | None = None) -> set[str]:
    """Return ticket keys this agent (or anyone, if agent is None) already has
    an active run on. Used to exclude double-pick during triage."""
    from .db import db_session
    from .models import Run

    terminal = {"completed", "stopped", "error"}
    keys: set[str] = set()
    with db_session() as db:
        q = db.query(Run.ticket).filter(~Run.state.in_(terminal))
        if agent:
            q = q.filter(Run.agent == agent)
        for (ticket,) in q.all():
            if ticket:
                # Normalize whatever's stored to a Jira-style key
                m = TICKET_ID_RE.search(ticket)
                if m:
                    keys.add(m.group(0))
                else:
                    keys.add(ticket)
    return keys
