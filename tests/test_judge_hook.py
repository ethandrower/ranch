"""Tests for the H8 PostToolUse hook + budget tracker.

The hook intercepts mcp__ranch__run_acceptance, resolves the checks (inline
or from the dossier), runs them, and returns formatted results as
additionalContext. We mock _run_acceptance so these tests don't exec real
subprocesses (the runner itself is tested in test_judge.py).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ranch.db import db_session, init_db
from ranch.judge import AcceptanceResult, JudgeRun
from ranch.models import Dossier, Run
from ranch.runner.judge_hook import (
    RUN_ACCEPTANCE_TOOL,
    _format_results_for_agent,
    _latest_acceptance_from_dossier,
    make_judge_hook,
)
from ranch.runner.tools import (
    DEFAULT_JUDGE_BUDGET,
    _judge_budget_remaining,
    reset_judge_budget,
)


# ─── Budget tracking ─────────────────────────────────────────────


def test_reset_judge_budget_starts_fresh():
    reset_judge_budget()
    assert _judge_budget_remaining() == DEFAULT_JUDGE_BUDGET


def test_reset_judge_budget_clears_counter_after_calls():
    """Budget state is module-level. Increment via the same path the tool does."""
    import ranch.runner.tools as _tools
    reset_judge_budget()
    _tools._judge_call_count = 3
    assert _judge_budget_remaining() == DEFAULT_JUDGE_BUDGET - 3
    reset_judge_budget()
    assert _judge_budget_remaining() == DEFAULT_JUDGE_BUDGET


def test_budget_remaining_goes_negative_after_overrun():
    """The tool body uses `remaining < 0` as the exhausted signal."""
    import ranch.runner.tools as _tools
    reset_judge_budget()
    _tools._judge_call_count = DEFAULT_JUDGE_BUDGET + 1
    assert _judge_budget_remaining() < 0
    reset_judge_budget()


# ─── _latest_acceptance_from_dossier ────────────────────────────


def _seed_dossier(run_id, payload):
    with db_session() as db:
        db.add(Dossier(run_id=run_id, state=payload.get("state", "planning"),
                        payload_json=json.dumps(payload)))


def test_latest_acceptance_returns_empty_when_no_run_id():
    assert _latest_acceptance_from_dossier(None) == []


def test_latest_acceptance_returns_empty_when_no_dossier():
    init_db()
    with db_session() as db:
        run = Run(agent="x", ticket="T-1", cwd="/tmp", initial_prompt="x", state="planning")
        db.add(run); db.flush()
        rid = run.id
    assert _latest_acceptance_from_dossier(rid) == []


def test_latest_acceptance_skips_dossiers_without_acceptance():
    init_db()
    with db_session() as db:
        run = Run(agent="x", ticket="T-1", cwd="/tmp", initial_prompt="x", state="planning")
        db.add(run); db.flush()
        rid = run.id
    _seed_dossier(rid, {"plan": [], "just_did": "x", "state": "coding"})
    assert _latest_acceptance_from_dossier(rid) == []


def test_latest_acceptance_returns_validated_checks():
    init_db()
    with db_session() as db:
        run = Run(agent="x", ticket="T-1", cwd="/tmp", initial_prompt="x", state="planning")
        db.add(run); db.flush()
        rid = run.id
    _seed_dossier(rid, {
        "plan": [], "just_did": "x", "state": "parked",
        "acceptance": [
            {"kind": "unit_test", "name": "pytest", "cmd": "pytest", "pass_pattern": "passed"},
        ],
    })
    checks = _latest_acceptance_from_dossier(rid)
    assert len(checks) == 1
    assert checks[0].kind == "unit_test"
    assert checks[0].cmd == "pytest"


def test_latest_acceptance_prefers_most_recent():
    """Walks dossier rows newest first; returns the first non-empty list."""
    import time
    init_db()
    with db_session() as db:
        run = Run(agent="x", ticket="T-1", cwd="/tmp", initial_prompt="x", state="planning")
        db.add(run); db.flush()
        rid = run.id
    # Older row with one check
    _seed_dossier(rid, {
        "plan": [], "just_did": "old", "state": "planning",
        "acceptance": [{"kind": "script", "name": "old", "cmd": "true", "pass_pattern": "."}],
    })
    time.sleep(0.01)
    # Newer row with two checks — this is what should be returned
    _seed_dossier(rid, {
        "plan": [], "just_did": "new", "state": "parked",
        "acceptance": [
            {"kind": "unit_test", "name": "a", "cmd": "echo a", "pass_pattern": "a"},
            {"kind": "unit_test", "name": "b", "cmd": "echo b", "pass_pattern": "b"},
        ],
    })
    checks = _latest_acceptance_from_dossier(rid)
    assert [c.name for c in checks] == ["a", "b"]


# ─── _format_results_for_agent ──────────────────────────────────


def test_format_empty_run_explains_misconfig():
    text = _format_results_for_agent(JudgeRun(), source="dossier (from propose)")
    assert "no checks executed" in text.lower()


def test_format_all_pass_says_proceed():
    run = JudgeRun(results=[
        AcceptanceResult(name="a", kind="unit_test", passed=True, duration_ms=10),
        AcceptanceResult(name="b", kind="http", passed=True, duration_ms=20),
    ])
    text = _format_results_for_agent(run, source="dossier (from propose)")
    assert "PASS" in text
    assert "2/2 passed" in text
    assert "proceed" in text.lower() or "pre_push" in text


def test_format_failure_includes_output_and_next_steps():
    run = JudgeRun(results=[
        AcceptanceResult(name="pytest", kind="unit_test", passed=False,
                         duration_ms=100, output="FAILED: assertion error"),
    ])
    text = _format_results_for_agent(run, source="dossier (from propose)")
    assert "FAIL" in text
    assert "FAILED: assertion error" in text
    assert "fix" in text.lower()
    assert "run_acceptance again" in text


# ─── Hook integration ───────────────────────────────────────────


def _make_orch(run_id):
    orch = MagicMock()
    orch.cwd = Path("/tmp")
    orch.run_id = run_id
    return orch


@pytest.mark.asyncio
async def test_hook_ignores_other_tools():
    hook_fn = make_judge_hook(_make_orch(None)).hooks[0]
    res = await hook_fn({"tool_name": "Bash", "tool_input": {}}, "tid", MagicMock())
    assert res == {}


@pytest.mark.asyncio
async def test_hook_runs_inline_checks_and_returns_report():
    """Inline checks bypass dossier lookup; results land in additionalContext."""
    init_db()
    orch = _make_orch(None)

    fake_run = JudgeRun(results=[
        AcceptanceResult(name="echo", kind="script", passed=True, duration_ms=5),
    ])
    with patch("ranch.runner.judge_hook._run_acceptance", return_value=fake_run):
        hook_fn = make_judge_hook(orch).hooks[0]
        res = await hook_fn(
            {
                "tool_name": RUN_ACCEPTANCE_TOOL,
                "tool_input": {"checks": [
                    {"kind": "script", "name": "echo", "cmd": "echo x", "pass_pattern": "x"},
                ]},
            },
            "tid", MagicMock(),
        )
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "inline (1 check)" in ctx
    assert "PASS" in ctx


@pytest.mark.asyncio
async def test_hook_falls_back_to_dossier_when_no_inline_checks():
    init_db()
    with db_session() as db:
        run = Run(agent="x", ticket="T-1", cwd="/tmp", initial_prompt="x", state="planning")
        db.add(run); db.flush()
        rid = run.id
    _seed_dossier(rid, {
        "plan": [], "just_did": "x", "state": "parked",
        "acceptance": [
            {"kind": "unit_test", "name": "p", "cmd": "echo passed", "pass_pattern": "passed"},
        ],
    })

    orch = _make_orch(rid)
    fake_run = JudgeRun(results=[
        AcceptanceResult(name="p", kind="unit_test", passed=True, duration_ms=5),
    ])
    captured = {}
    def fake_runner(checks, cwd):
        captured["count"] = len(list(checks))
        captured["cwd"] = cwd
        return fake_run
    with patch("ranch.runner.judge_hook._run_acceptance", side_effect=fake_runner):
        hook_fn = make_judge_hook(orch).hooks[0]
        res = await hook_fn(
            {"tool_name": RUN_ACCEPTANCE_TOOL, "tool_input": {}},
            "tid", MagicMock(),
        )

    assert captured["count"] == 1
    assert "dossier" in res["hookSpecificOutput"]["additionalContext"]


@pytest.mark.asyncio
async def test_hook_validates_inline_checks_and_surfaces_errors():
    orch = _make_orch(None)
    hook_fn = make_judge_hook(orch).hooks[0]
    res = await hook_fn(
        {
            "tool_name": RUN_ACCEPTANCE_TOOL,
            "tool_input": {"checks": [{"kind": "nonsense", "name": "x"}]},
        },
        "tid", MagicMock(),
    )
    ctx = res["hookSpecificOutput"]["additionalContext"]
    assert "validation" in ctx.lower()
