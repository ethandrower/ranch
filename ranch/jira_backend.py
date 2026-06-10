"""Resolve which Jira backend to use.

Default order:
1. **Trinity CLI** (`~/.local/bin/trinity` or `$TRINITY_BIN`) — the
   preferred path per the operator's global CLAUDE.md. Handles auth,
   token rotation, per-repo Bitbucket tokens, and doesn't lose the
   session mid-flight (which the official Atlassian MCP does).
2. **Legacy httpx JiraClient** — falls back when trinity isn't
   available or `RANCH_USE_TRINITY=0` forces it off. Requires
   `RANCH_JIRA_API_TOKEN` and `~/.ranch/config.toml`'s `[jira]` section.

Both implement the same minimal interface (`list_for_hand`,
`list_assigned_to_me`, `get_ticket`) so callers don't care which they
got. Returns a `(client_ctx_mgr, hand_account)` tuple so the CLI
can use `with client_ctx as client: ...` uniformly.
"""
from __future__ import annotations

import os
from contextlib import nullcontext
from typing import Any


def _trinity_available() -> bool:
    if os.environ.get("RANCH_USE_TRINITY", "1").strip() == "0":
        return False
    from .trinity_client import TRINITY_BIN_ENV, DEFAULT_TRINITY
    from shutil import which
    if os.environ.get(TRINITY_BIN_ENV, "").strip():
        return True
    if DEFAULT_TRINITY.exists():
        return True
    return which("trinity") is not None


def resolve_jira_client() -> tuple[Any, str]:
    """Pick a backend; return (context-manager-wrapped client, hand_account).

    Trinity is preferred. `hand_account` is the Jira identifier to put in
    the `assignee = "<x>"` JQL clause; trinity-mode pulls it from env
    (RANCH_HAND_ACCOUNT) or the legacy `[jira].hand_account` field, with
    a fallback to `currentUser()` when nothing's configured.
    """
    if _trinity_available():
        from .trinity_client import TrinityJiraClient
        # Trinity manages its own auth — we just need the hand_account
        # for the `assignee =` clause. Prefer env, then ~/.ranch/config,
        # then None (which the routing fn turns into `currentUser()`).
        hand_account = os.environ.get("RANCH_HAND_ACCOUNT", "").strip()
        if not hand_account:
            hand_account = _read_hand_account_from_config()
        return TrinityJiraClient(), hand_account

    # Legacy path
    from .triage import JiraClient, JiraConfig
    cfg = JiraConfig.load()
    return JiraClient(cfg), cfg.hand_account


def _read_hand_account_from_config() -> str:
    """Optional read of `[jira].hand_account` from ~/.ranch/config.toml
    so trinity-mode users can still configure the routing assignee
    without setting an env var. Failures are silent — caller can pass
    None to fall back to currentUser()."""
    try:
        import tomllib
        from .config import CONFIG_FILE
        if not CONFIG_FILE.exists():
            return ""
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        jira = data.get("jira") or {}
        return str(jira.get("hand_account") or jira.get("email") or "").strip()
    except Exception:
        return ""
