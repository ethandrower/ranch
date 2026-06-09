"""Tests for the propose module (Phase H6).

The live SDK session itself isn't tested here — that's the tier-2
integration test (running a real agent). These tests cover the pure-Python
plumbing: scope loading, brief construction, tool-set restriction, and
the orchestrator extension points propose relies on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ranch.propose import (
    DEFAULT_PROPOSE_BUDGET_SECONDS,
    PROPOSE_ALLOWED_TOOLS,
    PROPOSE_SYSTEM_PROMPT,
    ProposeError,
    build_propose_brief,
    resolve_scope_markdown,
)


# ─── Scope loading ─────────────────────────────────────────────────


def test_resolve_scope_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("ranch.scope.scope_path", lambda key: tmp_path / f"{key}.md")
    with pytest.raises(ProposeError, match="No saved scope for ECD-999"):
        resolve_scope_markdown("ECD-999")


def test_resolve_scope_returns_saved_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr("ranch.scope.scope_path", lambda key: tmp_path / f"{key}.md")
    md_path = tmp_path / "ECD-1.md"
    md_path.write_text("# ECD-1 — Saved scope\n\nbody")
    assert resolve_scope_markdown("ECD-1") == "# ECD-1 — Saved scope\n\nbody"


# ─── Brief construction ───────────────────────────────────────────


def test_brief_includes_ticket_key():
    brief = build_propose_brief("ECD-1234", "# scope content")
    assert "Ticket: ECD-1234" in brief


def test_brief_inlines_scope_bundle():
    scope = "# ECD-1234 — Add /healthz\n\n## Sister tickets\n- ECD-1235"
    brief = build_propose_brief("ECD-1234", scope)
    assert "SCOPE BUNDLE" in brief
    assert "Add /healthz" in brief
    assert "ECD-1235" in brief


def test_brief_strips_extra_whitespace():
    scope = "\n\n   # scope\n\n"
    brief = build_propose_brief("ECD-1", scope)
    # The scope is .strip()ed before insertion so the brief stays compact
    assert "\n\n\n\n   # scope" not in brief


# ─── Tool restriction ──────────────────────────────────────────────


def test_propose_tools_exclude_write_and_edit():
    assert "Write" not in PROPOSE_ALLOWED_TOOLS
    assert "Edit" not in PROPOSE_ALLOWED_TOOLS


def test_propose_tools_include_read_only_exploration():
    for tool in ("Read", "Grep", "Glob", "Bash"):
        assert tool in PROPOSE_ALLOWED_TOOLS, f"missing read-only tool: {tool}"


def test_propose_tools_include_mcp_state_and_checkpoint():
    assert "mcp__ranch__record_state" in PROPOSE_ALLOWED_TOOLS
    assert "mcp__ranch__record_checkpoint" in PROPOSE_ALLOWED_TOOLS


# ─── System prompt ─────────────────────────────────────────────────


def test_propose_system_prompt_forbids_modification():
    p = PROPOSE_SYSTEM_PROMPT
    # Must explicitly forbid Edit / Write / commits
    assert "may NOT use Edit, Write" in p or "may NOT use Edit/Write" in p or "NOT use Edit, Write" in p
    assert "commit" in p.lower()


def test_propose_system_prompt_requires_parked_final_state():
    p = PROPOSE_SYSTEM_PROMPT
    assert "state = \"parked\"" in p or 'state="parked"' in p or "parked" in p
    assert "Awaiting plan approval" in p
    assert "approve" in p and "reject" in p


def test_propose_system_prompt_specifies_acceptance_schema():
    """The propose prompt must teach the agent the v1 acceptance check kinds.

    Browser + figma_diff are v2 (H8 ships unit_test/script/http only); the
    prompt deliberately doesn't mention them so the agent doesn't emit
    checks the judge can't run.
    """
    p = PROPOSE_SYSTEM_PROMPT
    for kind in ("unit_test", "script", "http"):
        assert kind in p, f"acceptance kind '{kind}' missing from system prompt"
    # Must mention the contract is consumed by run_acceptance
    assert "run_acceptance" in p


def test_propose_system_prompt_requires_details_field():
    """details is what the operator reads in the Confluence-expand view."""
    assert "details" in PROPOSE_SYSTEM_PROMPT


# ─── Budget defaults ───────────────────────────────────────────────


def test_default_budget_is_bounded():
    assert 60 <= DEFAULT_PROPOSE_BUDGET_SECONDS <= 600  # 1-10 min reasonable range


# ─── Orchestrator extension points ─────────────────────────────────


def test_orchestrator_accepts_propose_overrides():
    """Sanity check the new kwargs land cleanly on Orchestrator."""
    from ranch.runner.orchestrator import Orchestrator

    orch = Orchestrator(
        agent="testbot",
        cwd=Path("/tmp"),
        ticket="ECD-1",
        brief="x",
        free=True,
        auto_approve=False,
        allowed_tools_override=PROPOSE_ALLOWED_TOOLS,
        budget_seconds=120.0,
        append_system_prompt_override=PROPOSE_SYSTEM_PROMPT,
    )
    assert orch.allowed_tools_override == PROPOSE_ALLOWED_TOOLS
    assert orch.budget_seconds == 120.0
    assert orch.append_system_prompt_override == PROPOSE_SYSTEM_PROMPT


def test_orchestrator_defaults_unchanged_when_no_overrides_passed():
    """The H6 extension is purely additive — vanilla orchestrators work as before."""
    from ranch.runner.orchestrator import Orchestrator

    orch = Orchestrator(agent="max", cwd=Path("/tmp"), ticket="ECD-1", brief="x")
    assert orch.allowed_tools_override is None
    assert orch.budget_seconds is None
    assert orch.append_system_prompt_override is None
