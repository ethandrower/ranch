"""Hermetic tests for the browser-verification stage (ranch verify)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ranch.runner.messages import CriterionVerdict, VerdictInput
from ranch.verify import VERDICT_TOOL, make_verdict_hook


def _verdict(overall=False):
    return VerdictInput(
        overall_pass=overall,
        criteria=[
            CriterionVerdict(criterion="counter starts at 0", passed=True,
                             evidence="loaded page, #count read 0", screenshot="crit1-pass.png"),
            CriterionVerdict(criterion="reset returns to 0", passed=overall,
                             evidence="clicked reset at 5, saw 1" if not overall else "clicked reset at 5, saw 0",
                             screenshot="crit2-fail.png"),
        ],
        summary="Reset sets counter to 1, not 0. Fix the #reset handler." if not overall else "All pass.",
    )


# ─── VerdictInput contract ───────────────────────────────────────


def test_verdict_requires_nonempty_evidence():
    with pytest.raises(Exception):
        CriterionVerdict(criterion="x", passed=True, evidence="   ")


def test_to_fix_brief_contains_failures_and_summary():
    brief = _verdict(overall=False).to_fix_brief("http://localhost:8907", "/tmp/arts")
    assert "FAILED CRITERIA" in brief
    assert "reset returns to 0" in brief
    assert "clicked reset at 5, saw 1" in brief
    assert "/tmp/arts/crit2-fail.png" in brief
    assert "Fix the #reset handler." in brief
    assert "counter starts at 0" not in brief.split("FAILED CRITERIA")[1].split("SUMMARY")[0]


def test_to_fix_brief_omits_screenshots_without_artifacts_dir():
    brief = _verdict(overall=False).to_fix_brief("http://x", None)
    assert "crit2-fail.png" not in brief


# ─── the verdict hook ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hook_captures_valid_verdict_into_sink():
    sink: dict = {}
    hook_fn = make_verdict_hook(sink).hooks[0]
    payload = _verdict(overall=True).model_dump()

    result = await hook_fn({"tool_name": VERDICT_TOOL, "tool_input": payload}, "tid", MagicMock())

    assert result == {}
    assert sink["verdict"].overall_pass is True
    assert len(sink["verdict"].criteria) == 2


@pytest.mark.asyncio
async def test_hook_rejects_invalid_payload_with_retry_context():
    sink: dict = {}
    hook_fn = make_verdict_hook(sink).hooks[0]

    result = await hook_fn({"tool_name": VERDICT_TOOL, "tool_input": {"overall_pass": True}}, "tid", MagicMock())

    assert "verdict" not in sink
    assert "validation error" in result["hookSpecificOutput"]["additionalContext"]


@pytest.mark.asyncio
async def test_hook_ignores_other_tools():
    sink: dict = {}
    hook_fn = make_verdict_hook(sink).hooks[0]
    result = await hook_fn({"tool_name": "Bash", "tool_input": {}}, "tid", MagicMock())
    assert result == {} and not sink


# ─── CLI registration ────────────────────────────────────────────


def test_verify_command_registered():
    from ranch.cli import cli
    assert "verify" in cli.commands
