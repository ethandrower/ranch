"""Smoke tests for reflect helpers — no real API calls."""
from ranch.reflect import _parse_json_block
from ranch.reflect import format_feedback_for_prompt, format_existing_lessons


def test_parse_json_block_plain():
    raw = '{"new_lessons": [], "reinforced_lessons": [], "skipped_feedback_ids": [1], "summary": "ok"}'
    result = _parse_json_block(raw)
    assert result is not None
    assert result["summary"] == "ok"


def test_parse_json_block_fenced():
    raw = '```json\n{"new_lessons": [], "summary": "good"}\n```'
    result = _parse_json_block(raw)
    assert result is not None
    assert result["summary"] == "good"


def test_parse_json_block_invalid():
    result = _parse_json_block("not json at all")
    assert result is None


def test_format_feedback_empty():
    result = format_feedback_for_prompt([])
    assert result == ""


def test_format_existing_lessons_empty():
    result = format_existing_lessons([])
    assert result == "(none yet)"
