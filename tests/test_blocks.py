"""Tests for the Block construct — cross-ticket cascading dependencies."""
from __future__ import annotations

from click.testing import CliRunner

from ranch.blocks import (
    is_run_blocked,
    open_blocks_for_ticket,
    record_block,
    resolve_blocks_for_run,
    resolve_blocks_for_ticket,
    resolve_blocks_on_approve,
)
from ranch.cli import cli
from ranch.db import db_session
from ranch.models import Block, Run


def _add_run(session, **kwargs) -> Run:
    defaults = dict(agent="max", state="queued", cwd="/tmp", initial_prompt="x")
    defaults.update(kwargs)
    r = Run(**defaults)
    session.add(r)
    session.flush()
    return r


# ─── record_block ──────────────────────────────────────────────────


def test_record_block_writes_row():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    block = record_block(
        blocked_run_id=r.id, blocker_ticket="ECD-73", reason="depends on plan",
    )
    assert block.id is not None
    assert block.blocker_ticket == "ECD-73"
    assert block.resolved_at is None
    assert block.source == "agent"


def test_record_block_links_blocker_run_id_when_blocker_run_exists():
    with db_session() as s:
        _add_run(s, ticket="ECD-73", agent="max")
        r = _add_run(s, ticket="ECD-78", agent="max")
    block = record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="x")
    assert block.blocker_run_id is not None


def test_record_block_is_idempotent_on_same_pair():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    b1 = record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="r1")
    b2 = record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="r1")
    assert b1.id == b2.id  # same row reused
    with db_session() as s:
        assert s.query(Block).count() == 1


def test_record_block_updates_reason_when_called_again():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="original")
    b = record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="updated")
    assert b.reason == "updated"


# ─── is_run_blocked ────────────────────────────────────────────────


def test_is_run_blocked_true_when_unresolved_block_exists():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="r")
    assert is_run_blocked(r.id) is True


def test_is_run_blocked_false_when_no_block():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-99")
    assert is_run_blocked(r.id) is False


def test_is_run_blocked_false_when_block_resolved():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="r")
    resolve_blocks_for_run(r.id)
    assert is_run_blocked(r.id) is False


# ─── Resolution ────────────────────────────────────────────────────


def test_resolve_blocks_for_ticket_marks_all_matching_resolved():
    with db_session() as s:
        a = _add_run(s, ticket="ECD-78")
        b = _add_run(s, ticket="ECD-79")
    record_block(blocked_run_id=a.id, blocker_ticket="ECD-73", reason="r")
    record_block(blocked_run_id=b.id, blocker_ticket="ECD-73", reason="r")
    n = resolve_blocks_for_ticket("ECD-73")
    assert n == 2
    assert open_blocks_for_ticket("ECD-73") == []


def test_resolve_blocks_for_ticket_ignores_already_resolved():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="r")
    resolve_blocks_for_ticket("ECD-73")
    n2 = resolve_blocks_for_ticket("ECD-73")
    assert n2 == 0


def test_resolve_blocks_for_ticket_empty_string_noop():
    assert resolve_blocks_for_ticket("") == 0


def test_resolve_blocks_for_run_clears_targeted_run_only():
    with db_session() as s:
        a = _add_run(s, ticket="ECD-78")
        b = _add_run(s, ticket="ECD-79")
    record_block(blocked_run_id=a.id, blocker_ticket="ECD-73", reason="r")
    record_block(blocked_run_id=b.id, blocker_ticket="ECD-73", reason="r")
    resolve_blocks_for_run(a.id)
    assert is_run_blocked(a.id) is False
    assert is_run_blocked(b.id) is True


# ─── Auto-resolve on approve ───────────────────────────────────────


def test_resolve_blocks_on_approve_unblocks_dependent_tickets():
    with db_session() as s:
        blocker = _add_run(s, ticket="ECD-73", state="parked")
        dep = _add_run(s, ticket="ECD-78")
    record_block(blocked_run_id=dep.id, blocker_ticket="ECD-73", reason="r")
    assert is_run_blocked(dep.id) is True
    # Operator approves the blocker's checkpoint → CLI calls this
    n = resolve_blocks_on_approve(blocker.id)
    assert n == 1
    assert is_run_blocked(dep.id) is False


def test_resolve_blocks_on_approve_noop_for_adhoc_run():
    with db_session() as s:
        adhoc = _add_run(s, ticket=None, state="parked")
    assert resolve_blocks_on_approve(adhoc.id) == 0


# ─── CLI: block/unblock ────────────────────────────────────────────


def test_cli_block_creates_operator_block():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    runner = CliRunner()
    result = runner.invoke(cli, [
        "block", str(r.id),
        "--blocker", "ECD-73",
        "--reason", "depends on plan-call",
    ])
    assert result.exit_code == 0, result.output
    assert "blocked by" in result.output

    with db_session() as s:
        rows = s.query(Block).all()
        assert len(rows) == 1
        assert rows[0].source == "operator"
        assert rows[0].blocker_ticket == "ECD-73"


def test_cli_unblock_clears():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    record_block(blocked_run_id=r.id, blocker_ticket="ECD-73", reason="r")
    runner = CliRunner()
    result = runner.invoke(cli, ["unblock", str(r.id)])
    assert result.exit_code == 0
    assert "Cleared 1" in result.output
    assert is_run_blocked(r.id) is False


def test_cli_unblock_noop_when_no_blocks():
    with db_session() as s:
        r = _add_run(s, ticket="ECD-78")
    runner = CliRunner()
    result = runner.invoke(cli, ["unblock", str(r.id)])
    assert result.exit_code == 0
    assert "no open blocks" in result.output


# ─── Approve CLI propagation ───────────────────────────────────────


def test_cli_approve_propagates_unblock(monkeypatch):
    """`ranch approve <blocker_id>` should queue the interjection AND clear
    blocks where blocker_ticket == approved run's ticket."""
    with db_session() as s:
        blocker = _add_run(s, ticket="ECD-73", state="parked")
        dep = _add_run(s, ticket="ECD-78")
    record_block(blocked_run_id=dep.id, blocker_ticket="ECD-73", reason="r")

    runner = CliRunner()
    result = runner.invoke(cli, ["approve", str(blocker.id)])
    assert result.exit_code == 0
    assert "unblocked 1 dependent ticket" in result.output
    assert is_run_blocked(dep.id) is False
