"""Tests for hand_events table + emit_event/list_events_for_hand helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from ranch.cli import cli
from ranch.db import db_session
from ranch.events import emit_event, extract_ticket, list_events_for_hand
from ranch.models import HandEvent, Run


def _add_run(s, **kw) -> Run:
    defaults = dict(agent="max", state="parked", cwd="/tmp", initial_prompt="x")
    defaults.update(kw)
    r = Run(**defaults)
    s.add(r); s.flush()
    return r


def test_emit_event_writes_row():
    ev = emit_event(
        hand_name="max", kind="state_transition", title="moved to verify",
        detail="acceptance starting", severity="info",
    )
    assert ev.id is not None
    with db_session() as s:
        rows = s.query(HandEvent).all()
        assert len(rows) == 1
        assert rows[0].hand_name == "max"
        assert rows[0].kind == "state_transition"


def test_emit_event_picks_default_icon_by_kind():
    ev = emit_event(hand_name="max", kind="ci_flip", title="CI red")
    assert ev.icon == "⚡"


def test_list_events_for_hand_returns_newest_first():
    emit_event(hand_name="max", kind="triage", title="first", severity="info")
    emit_event(hand_name="max", kind="triage", title="second", severity="info")
    emit_event(hand_name="max", kind="triage", title="third", severity="info")
    rows = list_events_for_hand("max")
    assert [r["title"] for r in rows[:3]] == ["third", "second", "first"]


def test_list_events_filters_by_hand():
    emit_event(hand_name="max", kind="triage", title="mx")
    emit_event(hand_name="arnold", kind="triage", title="ar")
    assert [r["title"] for r in list_events_for_hand("max")] == ["mx"]
    assert [r["title"] for r in list_events_for_hand("arnold")] == ["ar"]


def test_list_events_limit():
    for i in range(5):
        emit_event(hand_name="max", kind="triage", title=f"e{i}")
    rows = list_events_for_hand("max", limit=2)
    assert len(rows) == 2


def test_extract_ticket_finds_keys():
    assert extract_ticket("Approved ECD-2087 → exec") == "ECD-2087"
    assert extract_ticket("PR open · ECD-1234") == "ECD-1234"
    assert extract_ticket("no ticket here") is None
    assert extract_ticket(None) is None


def test_humanize_ago_buckets():
    """Make sure ago text falls into the expected buckets."""
    rows = []
    with db_session() as s:
        for delta_sec in [10, 90, 7200, 3 * 86400]:
            row = HandEvent(
                hand_name="max", kind="t", title="x",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=delta_sec),
            )
            s.add(row); s.flush()
            rows.append(row)
    out = list_events_for_hand("max", limit=10)
    agos = [r["ago"] for r in out]
    # newest first ordering
    assert agos[0].endswith("s")  # 10s
    assert agos[1].endswith("m")  # ~1m
    assert agos[2].endswith("h")  # ~2h
    assert agos[3].endswith("d")  # 3d


# ─── CLI integration: approve emits event ───────────────────────────


def test_cli_approve_emits_event():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-500", state="parked")
    runner = CliRunner()
    result = runner.invoke(cli, ["approve", str(r.id), "--note", "lgtm"])
    assert result.exit_code == 0
    with db_session() as s:
        events = s.query(HandEvent).filter_by(hand_name="max", kind="approval").all()
        assert len(events) == 1
        assert "ECD-500" in events[0].title


def test_cli_approve_emits_unblock_event_when_cascading():
    from ranch.blocks import record_block
    with db_session() as s:
        blocker = _add_run(s, ticket="ECD-600", state="parked")
        dep = _add_run(s, ticket="ECD-601", state="queued")
    record_block(blocked_run_id=dep.id, blocker_ticket="ECD-600", reason="r")

    runner = CliRunner()
    result = runner.invoke(cli, ["approve", str(blocker.id)])
    assert result.exit_code == 0

    with db_session() as s:
        kinds = {e.kind for e in s.query(HandEvent).all()}
        assert "approval" in kinds
        assert "block_resolved" in kinds


# ─── View-model integration: events_log populates from DB ──────────


def test_view_hand_includes_events_log():
    from ranch.view.hands import build_hand_view
    emit_event(hand_name="max", kind="ci_flip", title="CI passed on PR #1834")
    view = build_hand_view("max")
    assert len(view["events_log"]) >= 1
    assert view["events_log"][0]["title"].startswith("CI passed")
