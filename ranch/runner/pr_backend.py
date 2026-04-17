"""Pluggable PR backends for Bitbucket (bb) and GitHub (gh).

Each backend implements:
  - discover_pr_by_branch(branch, cwd) -> (pr_id, pr_url) | None
  - fetch_comments(pr_id, cwd) -> list[FetchedComment]
  - post_reply(pr_id, body, cwd, reply_to=None) -> None

The backends shell out to the respective CLI tools. `cwd` is the agent's
worktree — both CLIs infer the repo from the working directory.

Comments are returned as plain dicts (not Pydantic models) so the poller can
hand them straight to the DB without validation round-trips.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class FetchedComment:
    """A normalized review comment ready to be stored as ReviewComment."""
    platform_comment_id: str
    author: str | None
    file_path: str | None
    line_number: int | None
    body: str
    created_at_remote: datetime | None


class PRBackendError(RuntimeError):
    """CLI invocation failed or returned unparseable output."""


def _run(argv: list[str], cwd: Path, timeout: float = 30.0) -> str:
    """Run a subprocess and return stdout. Raises PRBackendError on failure."""
    try:
        result = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        raise PRBackendError(f"{argv[0]} invocation failed: {e}") from e
    if result.returncode != 0:
        raise PRBackendError(
            f"{argv[0]} exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Bitbucket + GitHub both emit ISO-8601 with offset ("+00:00" or "Z")
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Bitbucket (bb) ───────────────────────────────────────────────


class BBBackend:
    platform = "bb"

    def discover_pr_by_branch(self, branch: str, cwd: Path) -> tuple[str, str] | None:
        """Find an OPEN PR whose source branch matches. Returns (id, url) or None.

        `bb pr list` doesn't support --head filtering, so we list all OPEN PRs
        and filter client-side. The list is small enough in practice.
        """
        raw = _run(["bb", "--json", "pr", "list", "--state", "OPEN", "--all"], cwd)
        try:
            prs = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PRBackendError(f"bb pr list returned non-JSON: {e}") from e
        for pr in prs:
            src_branch = (
                pr.get("source", {}).get("branch", {}).get("name")
            )
            if src_branch == branch:
                pr_id = str(pr.get("id"))
                url = (
                    pr.get("links", {}).get("html", {}).get("href")
                    or f"https://bitbucket.org/.../pull-requests/{pr_id}"
                )
                return pr_id, url
        return None

    def fetch_comments(self, pr_id: str, cwd: Path) -> list[FetchedComment]:
        raw = _run(["bb", "--json", "pr", "view", pr_id, "--comments"], cwd)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PRBackendError(f"bb pr view returned non-JSON: {e}") from e
        comments = data.get("comments", []) or []
        out: list[FetchedComment] = []
        for c in comments:
            if c.get("deleted") or c.get("pending"):
                continue
            inline = c.get("inline") or {}
            out.append(FetchedComment(
                platform_comment_id=str(c.get("id")),
                author=(c.get("user") or {}).get("display_name"),
                file_path=inline.get("path"),
                line_number=inline.get("to") or inline.get("from"),
                body=(c.get("content") or {}).get("raw", ""),
                created_at_remote=_parse_iso(c.get("created_on")),
            ))
        return out

    def post_reply(self, pr_id: str, body: str, cwd: Path, reply_to: str | None = None) -> None:
        argv = ["bb", "pr", "comment", pr_id, "--body", body]
        if reply_to:
            argv += ["--reply-to", str(reply_to)]
        _run(argv, cwd)


# ─── GitHub (gh) ──────────────────────────────────────────────────


class GHBackend:
    platform = "gh"

    def discover_pr_by_branch(self, branch: str, cwd: Path) -> tuple[str, str] | None:
        """gh pr list --head <branch> returns matching PRs."""
        raw = _run(
            ["gh", "pr", "list", "--head", branch, "--state", "open",
             "--json", "number,url", "--limit", "1"],
            cwd,
        )
        try:
            prs = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PRBackendError(f"gh pr list returned non-JSON: {e}") from e
        if not prs:
            return None
        pr = prs[0]
        return str(pr["number"]), pr.get("url", "")

    def fetch_comments(self, pr_id: str, cwd: Path) -> list[FetchedComment]:
        """gh surfaces comments in two streams: review comments (inline, on
        diffs) and issue comments (general PR thread). We merge both."""
        out: list[FetchedComment] = []

        # Inline review comments
        raw = _run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_id}/comments",
             "--paginate"],
            cwd,
        )
        try:
            review = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PRBackendError(f"gh api pulls/comments returned non-JSON: {e}") from e
        for c in review:
            out.append(FetchedComment(
                platform_comment_id=f"review:{c.get('id')}",
                author=(c.get("user") or {}).get("login"),
                file_path=c.get("path"),
                line_number=c.get("line") or c.get("original_line"),
                body=c.get("body") or "",
                created_at_remote=_parse_iso(c.get("created_at")),
            ))

        # Issue-style PR thread comments
        raw = _run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/issues/{pr_id}/comments",
             "--paginate"],
            cwd,
        )
        try:
            issue = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PRBackendError(f"gh api issues/comments returned non-JSON: {e}") from e
        for c in issue:
            out.append(FetchedComment(
                platform_comment_id=f"issue:{c.get('id')}",
                author=(c.get("user") or {}).get("login"),
                file_path=None,
                line_number=None,
                body=c.get("body") or "",
                created_at_remote=_parse_iso(c.get("created_at")),
            ))
        return out

    def post_reply(self, pr_id: str, body: str, cwd: Path, reply_to: str | None = None) -> None:
        # gh doesn't support inline threaded replies from the CLI easily; we
        # post as an issue-style comment, optionally quoting the parent.
        if reply_to:
            body = f"(reply to comment {reply_to})\n\n{body}"
        _run(["gh", "pr", "comment", pr_id, "--body", body], cwd)


# ─── Dispatch ─────────────────────────────────────────────────────


def get_backend(platform: str):
    if platform == "bb":
        return BBBackend()
    if platform == "gh":
        return GHBackend()
    raise ValueError(f"Unknown PR platform: {platform!r}")


def detect_platform(cwd: Path) -> str | None:
    """Best-effort platform detection from the git remote URL."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    url = result.stdout.strip()
    if "bitbucket.org" in url:
        return "bb"
    if "github.com" in url:
        return "gh"
    return None
