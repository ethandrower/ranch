"""Tests for ranch.initiatives — Jira label → initiative_key resolution.

Validation target: Phase A of E2E issue #110.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from ranch.cli import cli
from ranch.db import db_session
from ranch.initiatives import (
    LABEL_PREFIX,
    default_initiative_for_hand,
    extract_initiative,
    initiative_exists,
    initiatives_for_hand,
    jql_label_clause,
    resolve_initiative_for_run,
)
from ranch.models import HandInitiative, Initiative, Run


# ─── extract_initiative ────────────────────────────────────────────


def test_extract_initiative_finds_first_matching_label():
    assert extract_initiative(["bug", "ranch-initiative:ref-mgmt", "p1"]) == "ref-mgmt"


def test_extract_initiative_case_insensitive_prefix():
    assert extract_initiative(["RANCH-INITIATIVE:scrapers"]) == "scrapers"


def test_extract_initiative_value_lowercased_for_dedup():
    """We lowercase the value so 'Ref-Mgmt' and 'ref-mgmt' don't pick up
    two different rows. Operators tend to be inconsistent with case."""
    assert extract_initiative(["ranch-initiative:Ref-Mgmt"]) == "ref-mgmt"


def test_extract_initiative_returns_none_when_absent():
    assert extract_initiative(["bug", "p1"]) is None
    assert extract_initiative([]) is None
    assert extract_initiative(None) is None  # type: ignore[arg-type]


def test_extract_initiative_strips_whitespace():
    assert extract_initiative(["  ranch-initiative:perf  "]) == "perf"


def test_extract_initiative_first_wins_on_multiple():
    """Multi-initiative labels are vanishingly rare — fail open by taking
    the first one, never crash."""
    assert extract_initiative([
        "ranch-initiative:ref-mgmt",
        "ranch-initiative:misc",
    ]) == "ref-mgmt"


# ─── initiatives_for_hand ──────────────────────────────────────────


def _seed_initiatives(s, hand: str, mapping: list[tuple[str, str, int, int]]) -> None:
    """mapping: [(key, label, is_default, sort_order), ...]"""
    keys_added = set()
    for key, label, is_default, order in mapping:
        if key not in keys_added:
            s.add(Initiative(key=key, label=label))
            keys_added.add(key)
    s.flush()
    for key, _, is_default, order in mapping:
        s.add(HandInitiative(
            hand_name=hand, initiative_key=key,
            is_default=is_default, sort_order=order,
        ))


def test_initiatives_for_hand_returns_sort_order():
    with db_session() as s:
        _seed_initiatives(s, "max", [
            ("misc", "Misc", 0, 2),
            ("ref-mgmt", "Reference Management", 1, 0),
            ("scrapers", "Scrapers", 0, 1),
        ])
    assert initiatives_for_hand("max") == ["ref-mgmt", "scrapers", "misc"]


def test_initiatives_for_hand_empty_when_unknown():
    assert initiatives_for_hand("nobody") == []


def test_default_initiative_prefers_marked():
    with db_session() as s:
        _seed_initiatives(s, "max", [
            ("ref-mgmt", "Reference Management", 0, 0),
            ("misc", "Misc", 1, 1),
        ])
    assert default_initiative_for_hand("max") == "misc"


def test_default_initiative_falls_back_to_first_sort_order():
    with db_session() as s:
        _seed_initiatives(s, "max", [
            ("scrapers", "Scrapers", 0, 0),
            ("perf", "Perf", 0, 1),
        ])
    assert default_initiative_for_hand("max") == "scrapers"


def test_default_initiative_none_when_hand_unknown():
    assert default_initiative_for_hand("nobody") is None


# ─── initiative_exists / jql_label_clause ──────────────────────────


def test_initiative_exists():
    with db_session() as s:
        s.add(Initiative(key="ref-mgmt", label="Reference Management"))
    assert initiative_exists("ref-mgmt") is True
    assert initiative_exists("nope") is False


def test_jql_clause_single_key():
    assert jql_label_clause(["ref-mgmt"]) == 'labels in ("ranch-initiative:ref-mgmt")'


def test_jql_clause_multiple_keys():
    out = jql_label_clause(["ref-mgmt", "misc"])
    assert out == 'labels in ("ranch-initiative:ref-mgmt", "ranch-initiative:misc")'


def test_jql_clause_empty_returns_empty_string():
    assert jql_label_clause([]) == ""
    assert jql_label_clause([None, "", "  "]) == ""  # type: ignore[list-item]


# ─── resolve_initiative_for_run ────────────────────────────────────


def test_operator_override_wins():
    with db_session() as s:
        _seed_initiatives(s, "max", [
            ("ref-mgmt", "Reference Management", 1, 0),
            ("misc", "Misc", 0, 1),
        ])
    resolved = resolve_initiative_for_run(
        operator_override="misc",
        ticket_labels=["ranch-initiative:ref-mgmt"],
        hand_name="max",
    )
    assert resolved == "misc"


def test_falls_to_ticket_label_when_no_override():
    with db_session() as s:
        _seed_initiatives(s, "max", [
            ("ref-mgmt", "Reference Management", 1, 0),
            ("scrapers", "Scrapers", 0, 1),
        ])
    resolved = resolve_initiative_for_run(
        operator_override=None,
        ticket_labels=["bug", "ranch-initiative:scrapers"],
        hand_name="max",
    )
    assert resolved == "scrapers"


def test_falls_to_hand_default_when_no_override_or_label():
    with db_session() as s:
        _seed_initiatives(s, "max", [
            ("ref-mgmt", "Reference Management", 1, 0),
        ])
    resolved = resolve_initiative_for_run(
        operator_override=None, ticket_labels=["bug"], hand_name="max",
    )
    assert resolved == "ref-mgmt"


def test_returns_none_when_nothing_matches():
    """Hand has no initiatives + ticket has no label + no operator override."""
    resolved = resolve_initiative_for_run(
        operator_override=None, ticket_labels=[], hand_name="ghost",
    )
    assert resolved is None


def test_unknown_key_in_override_falls_through():
    """If the operator types `--initiative typo`, we don't stamp it — we
    fall through to the next resolution layer."""
    with db_session() as s:
        _seed_initiatives(s, "max", [
            ("ref-mgmt", "Reference Management", 1, 0),
        ])
    resolved = resolve_initiative_for_run(
        operator_override="not-a-real-init",
        ticket_labels=["ranch-initiative:ref-mgmt"],
        hand_name="max",
    )
    assert resolved == "ref-mgmt"


# ─── JiraTicket.initiative derived property ────────────────────────


def test_jira_ticket_initiative_property():
    from ranch.triage import JiraTicket
    t = JiraTicket(
        key="ECD-1", summary="x", status="To Do", status_category="new",
        priority=None, created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        description="", labels=["ranch-initiative:ref-mgmt", "bug"],
    )
    assert t.initiative == "ref-mgmt"


def test_jira_ticket_initiative_none_when_no_label():
    from ranch.triage import JiraTicket
    t = JiraTicket(
        key="ECD-1", summary="x", status="To Do", status_category="new",
        priority=None, created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        description="", labels=["bug"],
    )
    assert t.initiative is None


# ─── route_label_for_hand ─────────────────────────────────────────


def test_route_label_for_hand():
    from ranch.initiatives import route_label_for_hand
    assert route_label_for_hand("max") == "ranch-max"
    assert route_label_for_hand("jeffy") == "ranch-jeffy"


def test_route_label_for_hand_normalizes_case_and_whitespace():
    from ranch.initiatives import route_label_for_hand
    assert route_label_for_hand("  MAX  ") == "ranch-max"


# ─── JiraClient.list_for_hand JQL building ─────────────────────────


def _make_fake_jc(captured: dict):
    from ranch.triage import JiraClient, JiraConfig

    class FakeJC(JiraClient):
        def __init__(self):
            # Skip parent __init__ — we don't want an HTTP client
            self._cfg = JiraConfig(url="https://x", email="e", api_token="t")
        def _search(self, jql: str) -> list:
            captured["jql"] = jql
            return []
    return FakeJC()


def test_list_for_hand_builds_routing_jql():
    captured: dict[str, str] = {}
    _make_fake_jc(captured).list_for_hand("max", assignee_account="ethan@citemed.io")
    jql = captured["jql"]
    assert 'assignee = "ethan@citemed.io"' in jql
    assert 'labels = "ranch-max"' in jql
    assert "statusCategory != Done" in jql


def test_list_for_hand_falls_back_to_current_user_when_no_account():
    captured: dict[str, str] = {}
    _make_fake_jc(captured).list_for_hand("max")
    assert "assignee = currentUser()" in captured["jql"]
    assert 'labels = "ranch-max"' in captured["jql"]


def test_list_assigned_to_me_no_label_filter():
    """The unscoped query (operator-eyeball view) doesn't add the routing
    label — that's the entire point of --all."""
    captured: dict[str, str] = {}
    _make_fake_jc(captured).list_assigned_to_me()
    assert "ranch-" not in captured["jql"]
    assert "assignee = currentUser()" in captured["jql"]


# ─── ranch dispatch --initiative ──────────────────────────────────


def test_dispatch_stamps_operator_initiative(monkeypatch, tmp_path):
    """Smoke: `ranch dispatch max --ticket ECD-1 --initiative misc --brief x`
    creates a Run with initiative_key="misc"."""
    # Stub the worktree path resolution so we don't need a real agent
    # config or a real subprocess spawn.
    from ranch import cli as _cli
    from ranch.config import Agent

    fake_agent = Agent(name="max", worktree=tmp_path)
    monkeypatch.setattr(_cli, "reload_agents", lambda: {"max": fake_agent})

    # Stub the detached subprocess spawn so the test stays in-process.
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 1})())

    with db_session() as s:
        s.add(Initiative(key="misc", label="Misc"))

    runner = CliRunner()
    result = runner.invoke(cli, [
        "dispatch", "max",
        "--ticket", "ECD-1",
        "--brief", "do the thing",
        "--initiative", "misc",
    ])
    assert result.exit_code == 0, result.output
    assert "misc" in result.output

    with db_session() as s:
        run = s.query(Run).filter_by(ticket="ECD-1").one()
        assert run.initiative_key == "misc"
