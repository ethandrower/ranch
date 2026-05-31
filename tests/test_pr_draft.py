"""Tests for H10 — PR draft assembly + backend create_pr methods.

Backend tests use subprocess mocks; the renderer tests are pure-Python
against synthetic Run + Dossier rows.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ranch.db import db_session, init_db
from ranch.models import Dossier, Run
from ranch.pr_draft import (
    PRDraft,
    RunArtifacts,
    _format_acceptance,
    _format_files_touched,
    _format_plan_progress,
    _strip_section,
    derive_title,
    gather_run_artifacts,
    render_draft,
    render_pr_body,
)


# ─── Helpers ───────────────────────────────────────────────────────


def _seed_run(ticket: str = "ECD-1234", branch: str = "feature/ECD-1234-foo",
              agent: str = "max", cwd: str = "/tmp", state: str = "completed") -> int:
    init_db()
    with db_session() as db:
        run = Run(
            agent=agent, ticket=ticket, cwd=cwd, initial_prompt="brief",
            state=state, branch_name=branch,
        )
        db.add(run); db.flush()
        return run.id


def _seed_dossier(run_id: int, payload: dict, state: str = "parked"):
    with db_session() as db:
        db.add(Dossier(run_id=run_id, state=state, payload_json=json.dumps(payload)))


# ─── _strip_section ───────────────────────────────────────────────


def test_strip_section_extracts_h2():
    text = "## Summary\n\nBuild a /healthz endpoint.\n\n## Plan\n\n1. a\n"
    assert "Build a /healthz endpoint." in _strip_section(text, "summary")
    assert "Plan" not in _strip_section(text, "summary")


def test_strip_section_handles_bold_wrap():
    """Agents sometimes wrap headers in bold: `## **Summary**`."""
    text = "## **Summary**\n\nbody\n\n## Plan\n\nx"
    assert "body" in _strip_section(text, "summary")


def test_strip_section_case_insensitive():
    text = "## SUMMARY\n\nbody\n\n## next\n\nz"
    assert "body" in _strip_section(text, "summary")


def test_strip_section_returns_empty_when_not_found():
    assert _strip_section("no headers", "summary") == ""


def test_strip_section_handles_regex_header_alternation():
    text = "## Risks / open questions\n\nbe careful\n\n"
    assert "be careful" in _strip_section(text, r"risks( / open questions)?")


# ─── _format_plan_progress ────────────────────────────────────────


def test_format_plan_renders_checklist():
    steps = [
        {"step": "Read code", "status": "done"},
        {"step": "Write tests", "status": "in_progress"},
        {"step": "Implement", "status": "pending"},
    ]
    out = _format_plan_progress(steps)
    assert "[x] Read code" in out
    assert "[~] Write tests" in out
    assert "[ ] Implement" in out


def test_format_plan_includes_notes():
    steps = [{"step": "x", "status": "done", "notes": "tricky"}]
    assert "_tricky_" in _format_plan_progress(steps)


def test_format_plan_empty():
    assert _format_plan_progress([]) == ""


# ─── _format_acceptance ──────────────────────────────────────────


def test_format_acceptance_includes_cmd_for_subprocess_kinds():
    out = _format_acceptance([
        {"kind": "unit_test", "name": "pytest", "cmd": "pytest tests/"},
        {"kind": "script", "name": "smoke", "cmd": "curl localhost"},
        {"kind": "http", "name": "healthz", "url": "http://localhost/h"},
    ])
    assert "pytest tests/" in out
    assert "curl localhost" in out
    assert "GET http://localhost/h" in out


def test_format_acceptance_empty():
    assert _format_acceptance([]) == ""


# ─── _format_files_touched ───────────────────────────────────────


def test_format_files_prefers_diff_stat():
    """When git diff stat is available, use it — it's authoritative."""
    out = _format_files_touched(
        files=["agent_said_this.py"],
        diff_stat=" foo.py | 1 +\n bar.py | 2 +-\n",
    )
    assert "foo.py" in out
    assert "agent_said_this.py" not in out


def test_format_files_falls_back_to_agent_list():
    out = _format_files_touched(files=["a.py", "b.py"], diff_stat="")
    assert "`a.py`" in out
    assert "`b.py`" in out


def test_format_files_handles_nothing():
    out = _format_files_touched(files=[], diff_stat="")
    assert "no diff captured" in out


# ─── derive_title ────────────────────────────────────────────────


def test_title_uses_summary_first_line():
    art = RunArtifacts(
        ticket="ECD-100", agent="max", branch_name="b", cwd="/tmp",
        final_details="## Summary\n\nAdd /healthz endpoint.\nMore detail.",
    )
    assert derive_title(art) == "ECD-100: Add /healthz endpoint"


def test_title_falls_back_to_just_did_when_no_summary():
    art = RunArtifacts(
        ticket="ECD-100", agent="max", branch_name="b", cwd="/tmp",
        final_just_did="Shipped the toast component.",
    )
    assert derive_title(art) == "ECD-100: Shipped the toast component"


def test_title_truncates_long_lines():
    art = RunArtifacts(
        ticket="ECD-100", agent="max", branch_name="b", cwd="/tmp",
        final_just_did="a" * 200,
    )
    title = derive_title(art)
    assert title.endswith("...")
    assert len(title) <= 90


def test_title_omits_ticket_prefix_when_already_present_in_summary():
    art = RunArtifacts(
        ticket="ECD-100", agent="max", branch_name="b", cwd="/tmp",
        final_just_did="ECD-100 — initial draft",
    )
    title = derive_title(art)
    # We don't double-prefix
    assert title.lower().count("ecd-100") == 1


def test_title_strips_bold_markers():
    art = RunArtifacts(
        ticket="ECD-100", agent="max", branch_name="b", cwd="/tmp",
        final_details="## Summary\n\n**Add the endpoint.**",
    )
    assert "**" not in derive_title(art)


# ─── render_pr_body ──────────────────────────────────────────────


def test_render_body_has_all_sections_when_inputs_present():
    art = RunArtifacts(
        ticket="ECD-100", agent="max", branch_name="feature/ECD-100", cwd="/tmp",
        final_just_did="Tests green, ready for review.",
        final_details=(
            "## Summary\n\nAdd /healthz.\n\n"
            "## Plan\n\n1. test\n2. impl\n\n"
            "## Acceptance criteria\n\nVerify the endpoint returns 200.\n\n"
            "## Complexity\n\nS\n\n"
            "## Risks / open questions\n\nNone.\n"
        ),
        plan_steps=[{"step": "Test", "status": "done"}],
        files_touched=["a.py"],
        acceptance=[{"kind": "unit_test", "name": "pytest", "cmd": "pytest"}],
        diff_stat=" a.py | 5 +\n",
        figma_url="https://figma.com/file/X",
        jira_base_url="https://example.atlassian.net",
    )
    body = render_pr_body(art)
    assert "## Summary" in body
    assert "Add /healthz." in body
    assert "### Plan progress" in body
    assert "## Changes" in body
    assert "a.py | 5 +" in body
    assert "## Testing" in body
    assert "[unit_test]" in body
    assert "### Manual verification" in body
    assert "Verify the endpoint returns 200." in body
    assert "## Open questions / risks" in body
    assert "## Linked" in body
    assert "https://example.atlassian.net/browse/ECD-100" in body
    assert "https://figma.com/file/X" in body
    assert "drafted from ranch run" in body


def test_render_body_omits_sections_with_no_input():
    art = RunArtifacts(
        ticket=None, agent="max", branch_name=None, cwd="/tmp",
        final_just_did="Did something.",
    )
    body = render_pr_body(art)
    assert "## Plan progress" not in body
    assert "## Open questions" not in body
    assert "## Testing" not in body  # nothing to put there
    assert "## Linked" not in body
    # But Summary + Changes are always present (changes shows "no diff captured")
    assert "## Summary" in body
    assert "## Changes" in body


def test_render_body_falls_back_to_ticket_code_when_no_jira_base():
    art = RunArtifacts(
        ticket="ECD-100", agent="max", branch_name="b", cwd="/tmp",
        final_just_did="x",
    )
    body = render_pr_body(art)
    assert "`ECD-100`" in body


# ─── gather + render_draft ───────────────────────────────────────


def test_gather_returns_dossier_state_and_run_metadata(tmp_path):
    rid = _seed_run(ticket="ECD-9", branch="feature/ECD-9", cwd=str(tmp_path))
    _seed_dossier(rid, {
        "plan": [{"step": "x", "status": "done"}],
        "just_did": "Tests green.",
        "state": "parked",
        "details": "## Summary\n\nA tiny thing.",
        "files_touched": ["foo.py"],
        "acceptance": [{"kind": "unit_test", "name": "pytest", "cmd": "pytest"}],
    })
    art = gather_run_artifacts(rid)
    assert art.ticket == "ECD-9"
    assert art.branch_name == "feature/ECD-9"
    assert art.final_state == "parked"
    assert art.files_touched == ["foo.py"]
    assert len(art.acceptance) == 1


def test_gather_raises_when_run_missing():
    init_db()
    with pytest.raises(ValueError, match="Run #999"):
        gather_run_artifacts(999)


def test_render_draft_end_to_end(tmp_path):
    rid = _seed_run(ticket="ECD-1", cwd=str(tmp_path))
    _seed_dossier(rid, {
        "plan": [], "just_did": "Ready.", "state": "parked",
        "details": "## Summary\n\nShipping it.",
    })
    draft, art = render_draft(rid, figma_url="https://figma.com/f")
    assert isinstance(draft, PRDraft)
    assert "ECD-1" in draft.title
    assert "Shipping it." in draft.body
    assert "figma.com/f" in draft.body
    assert art.figma_url == "https://figma.com/f"


# ─── Backend.create_pr — subprocess mocked ───────────────────────


def test_bb_backend_create_pr_passes_draft_flag(monkeypatch):
    from ranch.runner.pr_backend import BBBackend

    captured = {}

    def fake_run(argv, cwd, timeout=30.0):
        captured["argv"] = argv
        return json.dumps({"id": 42, "links": {"html": {"href": "https://bb/pr/42"}}})

    monkeypatch.setattr("ranch.runner.pr_backend._run", fake_run)
    pr_id, url = BBBackend().create_pr("title", "body", Path("/tmp"), draft=True)
    assert pr_id == "42"
    assert url == "https://bb/pr/42"
    assert "--draft" in captured["argv"]
    assert "-t" in captured["argv"] and "title" in captured["argv"]
    assert "-b" in captured["argv"] and "body" in captured["argv"]


def test_bb_backend_create_pr_skips_draft_when_false(monkeypatch):
    from ranch.runner.pr_backend import BBBackend

    captured = {}
    def fake_run(argv, cwd, timeout=30.0):
        captured["argv"] = argv
        return json.dumps({"id": 1, "links": {"html": {"href": "u"}}})

    monkeypatch.setattr("ranch.runner.pr_backend._run", fake_run)
    BBBackend().create_pr("t", "b", Path("/tmp"), draft=False)
    assert "--draft" not in captured["argv"]


def test_gh_backend_create_pr_parses_url_and_recovers_id(monkeypatch):
    from ranch.runner.pr_backend import GHBackend

    calls = []
    def fake_run(argv, cwd, timeout=30.0):
        calls.append(argv)
        if "create" in argv:
            return "https://github.com/o/r/pull/77\n"
        if "view" in argv:
            return json.dumps({"number": 77, "url": "https://github.com/o/r/pull/77"})
        return ""

    monkeypatch.setattr("ranch.runner.pr_backend._run", fake_run)
    pr_id, url = GHBackend().create_pr("title", "body", Path("/tmp"), draft=True)
    assert pr_id == "77"
    assert url.endswith("/pull/77")
    create_argv = calls[0]
    assert "--draft" in create_argv


def test_gh_backend_create_pr_falls_back_to_url_parse_on_view_failure(monkeypatch):
    """If `gh pr view` fails, derive the ID from the URL's tail."""
    from ranch.runner.pr_backend import GHBackend, PRBackendError

    def fake_run(argv, cwd, timeout=30.0):
        if "create" in argv:
            return "https://github.com/o/r/pull/55\n"
        raise PRBackendError("view failed")

    monkeypatch.setattr("ranch.runner.pr_backend._run", fake_run)
    pr_id, url = GHBackend().create_pr("t", "b", Path("/tmp"))
    assert pr_id == "55"
