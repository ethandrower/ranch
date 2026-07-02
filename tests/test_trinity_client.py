"""Tests for ranch.trinity_client — subprocess-backed Jira client.

We don't shell out to the real `trinity` binary; we monkeypatch the
subprocess call to return canned JSON matching trinity's actual output
shape (sampled from live runs against ECD-2076).
"""
from __future__ import annotations

import json

import pytest

from ranch.trinity_client import (
    TrinityJiraClient,
    _normalize_search_hit,
    _normalize_show,
    _parse_trinity_dt,
)


# ─── Shape fixtures (real trinity output) ──────────────────────────


SEARCH_HIT = {
    "key": "ECD-2076",
    "id": "30236",
    "summary": "Claims default to in_review and silently disappear from reports",
    "status": "In Progress",
    "assignee": "Ethan Drower",
    "assignee_id": "712020:8a829eca-ce74-4a15-a5b9-9fc5d33c7c4e",
    "reporter": "Ethan Drower",
    "priority": "High",
    "type": "Bug",
    "labels": ["ranch-max"],
    "created": "2026-06-02T00:35:26.623-0500",
    "updated": "2026-06-10T12:55:42.130-0500",
}

SHOW_RESULT = {
    "key": "ECD-2076",
    "id": "30236",
    "summary": "Claims default to in_review and silently disappear from reports",
    "description": "Claims created from the protocol / admin UI default to Claim.STATUS_IN_REVIEW. See https://www.figma.com/file/abc/design for the AC.",
    "status": "In Progress",
    "status_category": "In Progress",
    "assignee": {
        "name": "Ethan Drower",
        "account_id": "712020:8a829eca-ce74-4a15-a5b9-9fc5d33c7c4e",
        "email": "ethan@citemed.io",
    },
    "priority": "High",
    "type": "Bug",
    "labels": ["ranch-max"],
    "created": "2026-06-02T00:35:26.623-0500",
    "updated": "2026-06-10T12:55:42.130-0500",
    "epic_key": None,
}


# ─── Date parsing ──────────────────────────────────────────────────


def test_parse_dt_handles_compact_offset():
    """Trinity emits +HHMM or -HHMM; needs to be normalized to +HH:MM."""
    dt = _parse_trinity_dt("2026-06-02T00:35:26.623-0500")
    assert dt.year == 2026
    assert dt.month == 6


def test_parse_dt_handles_none():
    """No timestamp → epoch (so age computations don't crash)."""
    dt = _parse_trinity_dt(None)
    assert dt.year == 1970


# ─── Normalization ─────────────────────────────────────────────────


def test_search_hit_normalization():
    t = _normalize_search_hit(SEARCH_HIT)
    assert t.key == "ECD-2076"
    assert t.summary.startswith("Claims default")
    assert t.priority == "High"
    assert "ranch-max" in t.labels
    # search shape has no description → AC/figma fields default off
    assert t.description == ""
    assert t.has_figma_link is False
    # Status category inferred from status name
    assert t.status_category == "indeterminate"  # "In Progress"


def test_search_hit_status_category_inference():
    """The lightweight search shape doesn't include status_category — we
    infer from the status name for filter correctness."""
    hit = dict(SEARCH_HIT, status="Done")
    assert _normalize_search_hit(hit).status_category == "done"
    hit = dict(SEARCH_HIT, status="To Do")
    assert _normalize_search_hit(hit).status_category == "new"
    hit = dict(SEARCH_HIT, status="Backlog")
    assert _normalize_search_hit(hit).status_category == "new"
    hit = dict(SEARCH_HIT, status="In Review")
    assert _normalize_search_hit(hit).status_category == "indeterminate"


def test_show_normalization_pulls_assignee_email():
    t = _normalize_show(SHOW_RESULT)
    assert t.assignee_email == "ethan@citemed.io"
    assert "STATUS_IN_REVIEW" in t.description
    # figma URL in description → detected
    assert t.has_figma_link is True


def test_show_normalization_no_description():
    """Some tickets have no description — must not crash."""
    show = dict(SHOW_RESULT, description=None)
    t = _normalize_show(show)
    assert t.description == ""
    assert t.has_figma_link is False


# ─── Client smoke (subprocess mocked) ──────────────────────────────


def _fake_subprocess_runner(json_payload: dict):
    """Build a fake subprocess.run replacement that returns a CompletedProcess
    with `json_payload` on stdout."""
    class FakeCompleted:
        def __init__(self):
            self.returncode = 0
            self.stdout = json.dumps(json_payload)
            self.stderr = ""
    return lambda *a, **kw: FakeCompleted()


def test_client_list_for_hand_builds_routing_jql(monkeypatch):
    captured: dict[str, list[str]] = {}
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class FC:
            returncode = 0
            stdout = json.dumps({"issues": [SEARCH_HIT], "count": 1, "total": 1})
            stderr = ""
        return FC()
    monkeypatch.setattr("ranch.trinity_client.subprocess.run", fake_run)
    monkeypatch.setattr("ranch.trinity_client.trinity_path", lambda: "/fake/trinity")

    client = TrinityJiraClient()
    tickets = client.list_for_hand("max", assignee_account="ethan@citemed.io")

    # The JQL is the last positional arg to trinity
    jql = captured["cmd"][-1]
    assert 'assignee = "ethan@citemed.io"' in jql
    assert 'labels = "ranch-max"' in jql
    assert "statusCategory != Done" in jql

    assert len(tickets) == 1
    assert tickets[0].key == "ECD-2076"


def test_client_list_for_hand_falls_back_to_current_user(monkeypatch):
    captured: dict[str, list[str]] = {}
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class FC:
            returncode = 0
            stdout = json.dumps({"issues": []})
            stderr = ""
        return FC()
    monkeypatch.setattr("ranch.trinity_client.subprocess.run", fake_run)
    monkeypatch.setattr("ranch.trinity_client.trinity_path", lambda: "/fake/trinity")

    TrinityJiraClient().list_for_hand("max")
    jql = captured["cmd"][-1]
    assert "assignee = currentUser()" in jql
    assert 'labels = "ranch-max"' in jql


def test_client_get_ticket_returns_full_shape(monkeypatch):
    monkeypatch.setattr(
        "ranch.trinity_client.subprocess.run",
        _fake_subprocess_runner(SHOW_RESULT),
    )
    monkeypatch.setattr("ranch.trinity_client.trinity_path", lambda: "/fake/trinity")

    client = TrinityJiraClient()
    ticket, parent = client.get_ticket("ECD-2076")
    assert ticket.key == "ECD-2076"
    assert ticket.assignee_email == "ethan@citemed.io"
    assert parent is None


def test_client_raises_on_trinity_failure(monkeypatch):
    def fake_run(cmd, **kw):
        class FC:
            returncode = 2
            stdout = ""
            stderr = "trinity: authentication failed"
        return FC()
    monkeypatch.setattr("ranch.trinity_client.subprocess.run", fake_run)
    monkeypatch.setattr("ranch.trinity_client.trinity_path", lambda: "/fake/trinity")

    with pytest.raises(RuntimeError, match="trinity exited"):
        TrinityJiraClient().list_for_hand("max")


def test_client_raises_on_malformed_json(monkeypatch):
    def fake_run(cmd, **kw):
        class FC:
            returncode = 0
            stdout = "not json"
            stderr = ""
        return FC()
    monkeypatch.setattr("ranch.trinity_client.subprocess.run", fake_run)
    monkeypatch.setattr("ranch.trinity_client.trinity_path", lambda: "/fake/trinity")

    with pytest.raises(RuntimeError, match="not JSON"):
        TrinityJiraClient().list_for_hand("max")


# ─── Backend resolver picks trinity by default ─────────────────────


def test_resolver_picks_trinity_when_binary_present(monkeypatch, tmp_path):
    """When `trinity` is on PATH AND RANCH_USE_TRINITY isn't 0, the
    resolver returns a TrinityJiraClient, not the legacy JiraClient."""
    from ranch.jira_backend import resolve_jira_client

    monkeypatch.delenv("RANCH_USE_TRINITY", raising=False)
    monkeypatch.setenv("TRINITY_BIN", str(tmp_path / "trinity"))
    (tmp_path / "trinity").write_text("#!/bin/sh\necho '{}'\n")
    (tmp_path / "trinity").chmod(0o755)

    client_ctx, _ = resolve_jira_client()
    assert client_ctx.__class__.__name__ == "TrinityJiraClient"


def test_resolver_can_be_disabled_via_env(monkeypatch):
    """RANCH_USE_TRINITY=0 forces the legacy path."""
    from ranch.jira_backend import resolve_jira_client

    monkeypatch.setenv("RANCH_USE_TRINITY", "0")
    # Stub the legacy path so this test doesn't need real Jira creds
    class FakeCfg:
        url = "x"; email = "e"; api_token = "t"; hand_account = "e"
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr("ranch.triage.JiraConfig.load", classmethod(lambda cls: FakeCfg()))
    monkeypatch.setattr("ranch.triage.JiraClient", lambda c: FakeClient())

    client_ctx, account = resolve_jira_client()
    assert client_ctx.__class__.__name__ == "FakeClient"
    assert account == "e"
