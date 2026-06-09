"""Tests for the triage scorer and the Jira normalization helpers.

The live JiraClient is exercised via mocked HTTP responses so tests stay
hermetic — no network needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ranch.db import db_session, init_db
from ranch.models import Run
from ranch.triage import (
    JiraConfig,
    JiraClient,
    JiraTicket,
    _adf_to_text,
    _normalize_ticket,
    in_flight_ticket_keys_for_agent,
    score_ticket,
    triage,
)


def _ticket(**overrides) -> JiraTicket:
    base = dict(
        key="ECD-100",
        summary="Add /healthz endpoint",
        status="In Progress",
        status_category="indeterminate",
        priority="Medium",
        created=datetime.now(timezone.utc) - timedelta(days=3),
        updated=datetime.now(timezone.utc),
        description="Add a healthz endpoint per the design.",
        comments=[],
        labels=[],
        has_figma_link=False,
    )
    base.update(overrides)
    return JiraTicket(**base)


# ─── score_ticket ──────────────────────────────────────────────────


def test_status_in_progress_scores_30():
    t = _ticket(status="In Progress", status_category="indeterminate")
    s = score_ticket(t, set())
    assert s.status == 30


def test_status_todo_scores_20():
    t = _ticket(status="To Do", status_category="new")
    s = score_ticket(t, set())
    assert s.status == 20


def test_status_blocked_keyword_zeros_status():
    t = _ticket(status="Blocked", status_category="indeterminate")
    s = score_ticket(t, set())
    assert s.status == 0


def test_status_waiting_for_design_zeros_status():
    t = _ticket(status="Needs Design", status_category="new")
    s = score_ticket(t, set())
    assert s.status == 0


def test_figma_link_present_adds_20():
    t = _ticket(has_figma_link=True)
    s = score_ticket(t, set())
    assert s.design_present == 20


def test_figma_link_absent_zeroes_design():
    t = _ticket(has_figma_link=False)
    s = score_ticket(t, set())
    assert s.design_present == 0


def test_ac_explicit_header_recognized():
    t = _ticket(description="## Acceptance Criteria\n- foo\n- bar")
    s = score_ticket(t, set())
    assert s.ac_clarity == 15


def test_ac_numbered_should_recognized():
    t = _ticket(description="1. User should be able to click submit.\n2. The toast should fire.")
    s = score_ticket(t, set())
    assert s.ac_clarity == 15


def test_ac_missing_scores_zero():
    t = _ticket(description="Just a vague description.")
    s = score_ticket(t, set())
    assert s.ac_clarity == 0


@pytest.mark.parametrize("priority,expected", [
    ("Highest", 15),
    ("High", 10),
    ("Medium", 5),
    ("Low", 0),
    ("Lowest", -5),
    (None, 0),
    ("Unknown", 0),
])
def test_priority_ladder(priority, expected):
    t = _ticket(priority=priority)
    s = score_ticket(t, set())
    assert s.priority == expected


def test_age_grows_with_time_but_bounded():
    one_day = _ticket(created=datetime.now(timezone.utc) - timedelta(days=1))
    week = _ticket(created=datetime.now(timezone.utc) - timedelta(days=7))
    month = _ticket(created=datetime.now(timezone.utc) - timedelta(days=30))
    year = _ticket(created=datetime.now(timezone.utc) - timedelta(days=365))

    s1 = score_ticket(one_day, set()).age
    s7 = score_ticket(week, set()).age
    s30 = score_ticket(month, set()).age
    s365 = score_ticket(year, set()).age

    # Monotonically non-decreasing
    assert s1 <= s7 <= s30 <= s365
    # Bounded at 10
    assert s365 <= 10.0


def test_in_flight_drops_to_negative_thousand():
    t = _ticket(key="ECD-999")
    s = score_ticket(t, {"ECD-999"})
    assert s.dropped is True
    assert s.total == -1000


def test_total_is_sum_of_axes():
    t = _ticket(
        priority="High",
        has_figma_link=True,
        description="## Acceptance Criteria\n1. x",
        status="In Progress",
        status_category="indeterminate",
        created=datetime.now(timezone.utc) - timedelta(days=2),
    )
    s = score_ticket(t, set())
    expected = s.status + s.design_present + s.ac_clarity + s.priority + s.age
    assert s.total == pytest.approx(expected)


# ─── triage (ranking) ──────────────────────────────────────────────


def test_triage_excludes_in_flight_and_ranks_descending():
    tickets = [
        _ticket(key="ECD-1", priority="Low", has_figma_link=False, description="vague"),
        _ticket(key="ECD-2", priority="Highest", has_figma_link=True,
                description="## Acceptance Criteria\n1. should"),
        _ticket(key="ECD-3", priority="Medium", has_figma_link=True,
                description="## Acceptance Criteria\n1. should"),
        _ticket(key="ECD-DROP", priority="Highest", has_figma_link=True,
                description="## Acceptance Criteria\n1. should"),
    ]
    ranked = triage(tickets, {"ECD-DROP"})
    keys = [t.key for t, _ in ranked]
    assert "ECD-DROP" not in keys
    assert keys == ["ECD-2", "ECD-3", "ECD-1"]


def test_triage_empty_input_returns_empty():
    assert triage([], set()) == []


# ─── ADF extraction ────────────────────────────────────────────────


def test_adf_to_text_walks_paragraph_tree():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Line 2"}]},
        ],
    }
    assert "Hello " in _adf_to_text(doc)
    assert "world" in _adf_to_text(doc)
    assert "Line 2" in _adf_to_text(doc)


def test_adf_to_text_handles_none():
    assert _adf_to_text(None) == ""


def test_adf_to_text_handles_string():
    assert _adf_to_text("plain") == "plain"


# ─── Jira normalization ────────────────────────────────────────────


def test_normalize_ticket_extracts_all_fields():
    issue = {
        "key": "ECD-1234",
        "fields": {
            "summary": "Add /foo endpoint",
            "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
            "priority": {"name": "High"},
            "created": "2026-04-01T10:00:00.000+0000",
            "updated": "2026-04-15T10:00:00.000+0000",
            "labels": ["backend", "api"],
            "assignee": {"emailAddress": "max@example.com"},
            "description": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "See https://figma.com/file/abc"}]}],
            },
            "comment": {
                "comments": [
                    {"body": {"type": "doc", "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Looks good"}]},
                    ]}},
                ],
            },
        },
    }
    t = _normalize_ticket(issue)
    assert t.key == "ECD-1234"
    assert t.summary == "Add /foo endpoint"
    assert t.status == "In Progress"
    assert t.status_category == "indeterminate"
    assert t.priority == "High"
    assert "figma.com/file/abc" in t.description
    assert t.has_figma_link is True
    assert t.labels == ["backend", "api"]
    assert t.assignee_email == "max@example.com"
    assert t.created.year == 2026


def test_normalize_ticket_handles_missing_fields():
    issue = {"key": "ECD-1", "fields": {}}
    t = _normalize_ticket(issue)
    assert t.key == "ECD-1"
    assert t.summary == ""
    assert t.priority is None
    assert t.has_figma_link is False


def test_figma_link_detected_only_in_comments():
    """A figma link in a comment (not description) still counts."""
    issue = {
        "key": "ECD-X",
        "fields": {
            "description": {"type": "doc", "content": []},
            "comment": {"comments": [{"body": {"type": "doc", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Design at https://figma.com/file/xyz"},
                ]},
            ]}}]},
        },
    }
    t = _normalize_ticket(issue)
    assert t.has_figma_link is True


# ─── In-flight detection ───────────────────────────────────────────


def test_in_flight_ticket_keys_extracts_jira_keys():
    init_db()
    with db_session() as db:
        # Various ticket formats and states
        db.add_all([
            Run(agent="max", ticket="ECD-100", cwd="/tmp", initial_prompt="x", state="planning"),
            Run(agent="max", ticket="feature/ECD-200-foo", cwd="/tmp", initial_prompt="x", state="in_development"),
            Run(agent="max", ticket="ECD-300", cwd="/tmp", initial_prompt="x", state="completed"),
            Run(agent="jeffy", ticket="ECD-400", cwd="/tmp", initial_prompt="x", state="planning"),
        ])

    keys = in_flight_ticket_keys_for_agent("max")
    assert keys == {"ECD-100", "ECD-200"}  # 300 is completed (terminal), 400 is jeffy's


def test_in_flight_ticket_keys_no_agent_filter_returns_all():
    init_db()
    with db_session() as db:
        db.add_all([
            Run(agent="max", ticket="ECD-1", cwd="/tmp", initial_prompt="x", state="planning"),
            Run(agent="jeffy", ticket="ECD-2", cwd="/tmp", initial_prompt="x", state="planning"),
        ])
    keys = in_flight_ticket_keys_for_agent(None)
    assert keys == {"ECD-1", "ECD-2"}


# ─── JiraClient with mocked transport ──────────────────────────────


def _mock_transport(responder):
    """Build an httpx MockTransport that calls `responder(request)` for every request."""
    return httpx.MockTransport(responder)


def test_jira_client_builds_jql_and_normalizes_response():
    captured = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["jql"] = request.url.params.get("jql")
        return httpx.Response(200, json={
            "issues": [
                {"key": "ECD-1", "fields": {"summary": "First", "status": {"name": "To Do", "statusCategory": {"key": "new"}}, "priority": None, "created": "2026-04-01T00:00:00.000+0000", "updated": "2026-04-01T00:00:00.000+0000", "description": {"type": "doc", "content": []}, "comment": {"comments": []}}},
                {"key": "ECD-2", "fields": {"summary": "Second", "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}, "priority": {"name": "High"}, "created": "2026-04-02T00:00:00.000+0000", "updated": "2026-04-02T00:00:00.000+0000", "description": {"type": "doc", "content": []}, "comment": {"comments": []}}},
            ]
        })

    cfg = JiraConfig(url="https://example.atlassian.net", email="me@example.com", api_token="tok")
    client = JiraClient(cfg)
    client._client = httpx.Client(
        base_url=cfg.url, auth=(cfg.email, cfg.api_token), transport=_mock_transport(responder),
    )
    try:
        tickets = client.list_assigned_to_me(project="ECD")
    finally:
        client.close()

    assert len(tickets) == 2
    assert tickets[0].key == "ECD-1"
    assert tickets[1].priority == "High"

    assert captured["url"].startswith("https://example.atlassian.net/rest/api/3/search")
    assert "assignee = currentUser()" in captured["jql"]
    assert "statusCategory != Done" in captured["jql"]
    assert "project = ECD" in captured["jql"]


def test_jira_client_raises_on_4xx():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessages": ["unauth"]})

    cfg = JiraConfig(url="https://example.atlassian.net", email="me@example.com", api_token="bad")
    client = JiraClient(cfg)
    client._client = httpx.Client(
        base_url=cfg.url, auth=(cfg.email, cfg.api_token), transport=_mock_transport(responder),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.list_assigned_to_me()
    finally:
        client.close()
