"""Tests for the scope bundle assembly + rendering + persistence (Phase H5).

The Jira side is tested via a stub `JiraClient`-shaped object so we don't
hit the network. The bb side is tested by patching the module's
`_bb_pr_list_open` helper.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from ranch.scope import (
    PrSummary,
    Scope,
    _extract_referenced_tickets,
    build_scope,
    find_open_prs,
    load_scope_markdown,
    render_scope_markdown,
    save_scope,
    scope_path,
)
from ranch.triage import JiraTicket


def _ticket(**overrides) -> JiraTicket:
    base = dict(
        key="ECD-100",
        summary="Add /healthz endpoint",
        status="In Progress",
        status_category="indeterminate",
        priority="Medium",
        created=datetime(2026, 4, 1, tzinfo=timezone.utc),
        updated=datetime(2026, 4, 10, tzinfo=timezone.utc),
        description="Plain description.",
        comments=[],
        labels=[],
        has_figma_link=False,
    )
    base.update(overrides)
    return JiraTicket(**base)


class _StubJiraClient:
    """Mimics the methods build_scope uses, fed by a static catalog."""

    def __init__(self, tickets: dict[str, JiraTicket], parents: dict[str, str | None], sisters: dict[str, list[JiraTicket]]):
        self.tickets = tickets
        self.parents = parents
        self.sisters = sisters

    def get_ticket(self, key: str):
        return self.tickets[key], self.parents.get(key)

    def list_sisters(self, epic_key: str):
        return self.sisters.get(epic_key, [])


# ─── _extract_referenced_tickets ───────────────────────────────────


def test_extract_referenced_tickets_dedupes():
    assert _extract_referenced_tickets("ECD-1 fix and ECD-1 again with ECD-2") == ["ECD-1", "ECD-2"]


def test_extract_referenced_tickets_ignores_lowercase():
    assert _extract_referenced_tickets("ecd-1 ecd-2") == []


def test_extract_referenced_tickets_empty():
    assert _extract_referenced_tickets("") == []
    assert _extract_referenced_tickets(None) == []  # type: ignore[arg-type]


# ─── find_open_prs ─────────────────────────────────────────────────


def _bb_pr_payload(prs):
    """Build the shape `bb --json pr list` returns."""
    return prs


def test_find_open_prs_matches_branch_containing_key():
    fake_prs = [
        {"id": 42, "title": "Fix the thing",
         "source": {"branch": {"name": "feature/ECD-100-healthz"}},
         "links": {"html": {"href": "https://bitbucket.org/x/y/pull-requests/42"}},
         "author": {"display_name": "Ethan"}},
        {"id": 43, "title": "Unrelated",
         "source": {"branch": {"name": "chore/bump-deps"}},
         "links": {}, "author": {"display_name": "Other"}},
    ]
    with patch("ranch.scope._bb_pr_list_open", return_value=fake_prs):
        prs = find_open_prs(Path("/tmp"), ["ECD-100"])
    assert len(prs) == 1
    assert prs[0].id == "42"
    assert prs[0].branch == "feature/ECD-100-healthz"
    assert prs[0].author == "Ethan"
    assert "ECD-100" in prs[0].referenced_ticket_keys


def test_find_open_prs_matches_title_when_branch_lacks_key():
    fake_prs = [
        {"id": 50, "title": "ECD-200: refactor service",
         "source": {"branch": {"name": "feature/refactor-service"}},
         "links": {}, "author": {}},
    ]
    with patch("ranch.scope._bb_pr_list_open", return_value=fake_prs):
        prs = find_open_prs(Path("/tmp"), ["ECD-200"])
    assert len(prs) == 1
    assert prs[0].id == "50"
    assert "ECD-200" in prs[0].referenced_ticket_keys


def test_find_open_prs_empty_keys_returns_empty():
    with patch("ranch.scope._bb_pr_list_open", return_value=[{"id": 1, "title": "x"}]):
        assert find_open_prs(Path("/tmp"), []) == []


def test_find_open_prs_handles_bb_failure_gracefully():
    """If `bb` errors out, scope discovery shouldn't crash — just no PRs."""
    with patch("ranch.scope._bb_pr_list_open", return_value=[]):
        assert find_open_prs(Path("/tmp"), ["ECD-100"]) == []


# ─── build_scope ───────────────────────────────────────────────────


def test_build_scope_no_epic():
    """A ticket without a parent — no epic, no sisters."""
    t = _ticket(key="ECD-100", description="Just a single ticket.")
    stub = _StubJiraClient({"ECD-100": t}, parents={"ECD-100": None}, sisters={})

    with patch("ranch.scope._bb_pr_list_open", return_value=[]):
        scope = build_scope("ECD-100", jira=stub, cwd=Path("/tmp"))

    assert scope.ticket.key == "ECD-100"
    assert scope.epic is None
    assert scope.sisters == []
    assert scope.related_prs == []
    assert scope.design_links == []


def test_build_scope_with_epic_and_sisters():
    t = _ticket(key="ECD-100", summary="The work")
    epic = _ticket(key="ECD-001", summary="Big epic",
                   description="Epic spans https://figma.com/file/ABC")
    sister_a = _ticket(key="ECD-101", summary="Sibling A")
    sister_b = _ticket(key="ECD-102", summary="Sibling B", status="Done")

    stub = _StubJiraClient(
        {"ECD-100": t, "ECD-001": epic},
        parents={"ECD-100": "ECD-001", "ECD-001": None},
        sisters={"ECD-001": [t, sister_a, sister_b]},  # source ticket included; should be filtered
    )

    with patch("ranch.scope._bb_pr_list_open", return_value=[]):
        scope = build_scope("ECD-100", jira=stub, cwd=Path("/tmp"))

    assert scope.epic is not None
    assert scope.epic.key == "ECD-001"
    sister_keys = [s.key for s in scope.sisters]
    assert "ECD-100" not in sister_keys  # filtered out
    assert sister_keys == ["ECD-101", "ECD-102"]
    # Design link surfaced from epic description
    assert any("figma.com/file/ABC" in d for d in scope.design_links)


def test_build_scope_dedupes_design_links_across_ticket_and_epic():
    t = _ticket(key="ECD-100", description="Design: https://figma.com/file/X")
    epic = _ticket(key="ECD-001", description="Also https://figma.com/file/X and https://figma.com/file/Y")
    stub = _StubJiraClient(
        {"ECD-100": t, "ECD-001": epic},
        parents={"ECD-100": "ECD-001", "ECD-001": None},
        sisters={"ECD-001": []},
    )
    with patch("ranch.scope._bb_pr_list_open", return_value=[]):
        scope = build_scope("ECD-100", jira=stub, cwd=Path("/tmp"))
    # X appears in both ticket and epic — should be listed once
    figma_x_count = sum(1 for url in scope.design_links if "figma.com/file/X" in url)
    assert figma_x_count == 1
    assert any("figma.com/file/Y" in url for url in scope.design_links)


def test_build_scope_extracts_confluence_links():
    t = _ticket(key="ECD-100", description="See https://citemed.atlassian.net/wiki/spaces/ENG/pages/123")
    stub = _StubJiraClient({"ECD-100": t}, parents={"ECD-100": None}, sisters={})
    with patch("ranch.scope._bb_pr_list_open", return_value=[]):
        scope = build_scope("ECD-100", jira=stub, cwd=Path("/tmp"))
    assert len(scope.confluence_refs) == 1
    assert "wiki/spaces/ENG/pages/123" in scope.confluence_refs[0]


def test_build_scope_includes_prs_referencing_epic_or_sisters():
    t = _ticket(key="ECD-100")
    sister = _ticket(key="ECD-101")
    epic = _ticket(key="ECD-001")
    stub = _StubJiraClient(
        {"ECD-100": t, "ECD-001": epic},
        parents={"ECD-100": "ECD-001", "ECD-001": None},
        sisters={"ECD-001": [sister]},
    )
    fake_prs = [
        {"id": 1, "title": "WIP: ECD-101 sister work",
         "source": {"branch": {"name": "feature/ECD-101-foo"}},
         "links": {"html": {"href": "https://example/pull/1"}}, "author": {}},
    ]
    with patch("ranch.scope._bb_pr_list_open", return_value=fake_prs):
        scope = build_scope("ECD-100", jira=stub, cwd=Path("/tmp"))
    assert len(scope.related_prs) == 1
    assert scope.related_prs[0].id == "1"


def test_build_scope_skips_pr_discovery_when_no_cwd():
    """If caller passes cwd=None, bb is never invoked."""
    t = _ticket(key="ECD-100")
    stub = _StubJiraClient({"ECD-100": t}, parents={"ECD-100": None}, sisters={})

    def _should_not_be_called(_cwd):
        raise AssertionError("PR discovery should be skipped without cwd")

    with patch("ranch.scope._bb_pr_list_open", side_effect=_should_not_be_called):
        scope = build_scope("ECD-100", jira=stub, cwd=None)
    assert scope.related_prs == []


# ─── render_scope_markdown ─────────────────────────────────────────


def test_render_includes_ticket_header():
    t = _ticket(key="ECD-100", summary="Add /healthz", priority="High",
                labels=["backend"], assignee_email="max@example.com")
    md = render_scope_markdown(Scope(ticket=t))
    assert "# ECD-100 — Add /healthz" in md
    assert "**Status**: In Progress" in md
    assert "**Priority**: High" in md
    assert "**Assignee**: max@example.com" in md
    assert "**Labels**: backend" in md


def test_render_includes_epic_section_when_present():
    t = _ticket(key="ECD-100")
    epic = _ticket(key="ECD-001", summary="Epic", description="Epic-level context here.")
    md = render_scope_markdown(Scope(ticket=t, epic=epic))
    assert "## Epic — ECD-001: Epic" in md
    assert "Epic-level context here." in md


def test_render_includes_sisters_with_status_and_priority():
    t = _ticket(key="ECD-100")
    sisters = [
        _ticket(key="ECD-101", status="To Do", priority="High"),
        _ticket(key="ECD-102", status="Done", priority=None),
    ]
    md = render_scope_markdown(Scope(ticket=t, sisters=sisters))
    assert "## Sister tickets (2)" in md
    assert "`ECD-101` [To Do] (High)" in md
    assert "`ECD-102` [Done]" in md


def test_render_includes_prs_with_refs():
    t = _ticket(key="ECD-100")
    prs = [PrSummary(id="42", title="Fix it", branch="feature/ECD-100-fix",
                     url="https://bb/pr/42", referenced_ticket_keys=["ECD-100"])]
    md = render_scope_markdown(Scope(ticket=t, related_prs=prs))
    assert "## Open PRs in this epic (1)" in md
    assert "#42 `feature/ECD-100-fix`" in md
    assert "refs ECD-100" in md
    assert "https://bb/pr/42" in md


def test_render_omits_empty_sections():
    t = _ticket(key="ECD-100")
    md = render_scope_markdown(Scope(ticket=t))
    assert "## Sister tickets" not in md
    assert "## Open PRs" not in md
    assert "## Design references" not in md
    assert "## Confluence references" not in md


def test_render_includes_description_block():
    t = _ticket(key="ECD-100", description="Detailed problem statement here.")
    md = render_scope_markdown(Scope(ticket=t))
    assert "## Ticket description" in md
    assert "Detailed problem statement here." in md


# ─── Persistence ───────────────────────────────────────────────────


def test_save_and_load_scope_roundtrip(tmp_path, monkeypatch):
    """save_scope writes the markdown; load_scope_markdown reads it back."""
    monkeypatch.setattr("ranch.scope.SCOPES_DIR", tmp_path)
    monkeypatch.setattr("ranch.scope.scope_path",
                        lambda key: tmp_path / f"{key}.md")

    t = _ticket(key="ECD-100", summary="Roundtrip test")
    scope = Scope(ticket=t)

    path = save_scope(scope)
    assert path.exists()
    loaded = load_scope_markdown("ECD-100")
    assert loaded is not None
    assert "# ECD-100 — Roundtrip test" in loaded


def test_load_scope_markdown_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("ranch.scope.scope_path", lambda key: tmp_path / f"{key}.md")
    assert load_scope_markdown("DOES-NOT-EXIST") is None


# ─── Scope.to_dict ─────────────────────────────────────────────────


def test_scope_to_dict_omits_none_epic():
    t = _ticket(key="ECD-100")
    d = Scope(ticket=t).to_dict()
    assert d["epic"] is None
    assert d["sisters"] == []
    assert d["related_prs"] == []


def test_scope_to_dict_serializes_full_bundle():
    t = _ticket(key="ECD-100")
    epic = _ticket(key="ECD-001", summary="Epic")
    sister = _ticket(key="ECD-101")
    pr = PrSummary(id="42", title="X", branch="b", url="u", referenced_ticket_keys=["ECD-100"])
    scope = Scope(
        ticket=t, epic=epic, sisters=[sister], related_prs=[pr],
        design_links=["https://figma.com/file/X"],
        confluence_refs=["https://citemed.atlassian.net/wiki/x"],
    )
    d = scope.to_dict()
    assert d["ticket"]["key"] == "ECD-100"
    assert d["epic"]["key"] == "ECD-001"
    assert d["sisters"][0]["key"] == "ECD-101"
    assert d["related_prs"][0]["id"] == "42"
    assert d["design_links"] == ["https://figma.com/file/X"]
    assert d["confluence_refs"] == ["https://citemed.atlassian.net/wiki/x"]
