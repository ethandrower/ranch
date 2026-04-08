"""Tests for Pydantic message models in ranch/runner/messages.py."""
import pytest
from pydantic import ValidationError

from ranch.runner.messages import (
    CheckpointInput,
    DecisionLogInput,
    HumanDecision,
    HumanNote,
)


# ─── CheckpointInput ─────────────────────────────────────────────


def test_checkpoint_input_valid():
    cp = CheckpointInput(kind="plan_ready", summary="Here is the plan.")
    assert cp.kind == "plan_ready"
    assert cp.payload is None


def test_checkpoint_input_with_payload():
    cp = CheckpointInput(kind="pre_push", summary="Ready.", payload={"files": ["a.py"]})
    assert cp.payload == {"files": ["a.py"]}


def test_checkpoint_input_invalid_kind():
    with pytest.raises(ValidationError):
        CheckpointInput(kind="unknown_kind", summary="Bad kind")


def test_checkpoint_input_empty_summary_rejected():
    with pytest.raises(ValidationError, match="summary must not be empty"):
        CheckpointInput(kind="plan_ready", summary="   ")


def test_checkpoint_input_model_validate_from_dict():
    data = {"kind": "tests_green", "summary": "All pass", "payload": None}
    cp = CheckpointInput.model_validate(data)
    assert cp.kind == "tests_green"


def test_checkpoint_input_model_validate_rejects_bad_kind():
    with pytest.raises(ValidationError):
        CheckpointInput.model_validate({"kind": "oops", "summary": "fine"})


# ─── DecisionLogInput ────────────────────────────────────────────


def test_decision_log_valid():
    d = DecisionLogInput(decision="Use FileResponse", rationale="Streams large files.")
    assert d.decision == "Use FileResponse"


def test_decision_log_missing_field():
    with pytest.raises(ValidationError):
        DecisionLogInput(decision="Use FileResponse")  # missing rationale


# ─── HumanDecision.to_prompt — approval paths ────────────────────


def test_human_decision_plan_ready_approved():
    msg = HumanDecision(checkpoint_kind="plan_ready", decision="approved").to_prompt()
    assert "APPROVED" in msg
    assert "plan_ready" in msg
    assert "DEVELOP" in msg
    assert "failing tests" in msg


def test_human_decision_tests_green_approved():
    msg = HumanDecision(checkpoint_kind="tests_green", decision="approved").to_prompt()
    assert "APPROVED" in msg
    assert "QA" in msg


def test_human_decision_pre_push_approved_includes_branch_hint():
    msg = HumanDecision(
        checkpoint_kind="pre_push", decision="approved", ticket="ECD-1589"
    ).to_prompt()
    assert "APPROVED" in msg
    assert "ecd-1589" in msg.lower()
    assert "branch" in msg.lower()
    assert "push" in msg.lower()
    assert "commit" in msg.lower()


def test_human_decision_pre_push_approved_no_ticket():
    msg = HumanDecision(checkpoint_kind="pre_push", decision="approved").to_prompt()
    assert "APPROVED" in msg
    assert "branch" in msg.lower()


def test_human_decision_custom_approved():
    msg = HumanDecision(checkpoint_kind="custom", decision="approved").to_prompt()
    assert "APPROVED" in msg
    assert "Continue" in msg


# ─── HumanDecision.to_prompt — rejection paths ───────────────────


def test_human_decision_rejected_includes_reason():
    msg = HumanDecision(
        checkpoint_kind="plan_ready",
        decision="rejected",
        reason="scope is too wide",
    ).to_prompt()
    assert "REJECTED" in msg
    assert "scope is too wide" in msg
    assert "revise" in msg.lower()


def test_human_decision_rejected_no_reason_uses_fallback():
    msg = HumanDecision(checkpoint_kind="pre_push", decision="rejected").to_prompt()
    assert "REJECTED" in msg
    assert "no reason given" in msg


def test_human_decision_rejected_does_not_include_push_steps():
    """Rejected pre_push must NOT include branch/commit/push instructions."""
    msg = HumanDecision(
        checkpoint_kind="pre_push", decision="rejected", reason="tests failing"
    ).to_prompt()
    # "pre_push" may appear in the header — that's fine.
    # What must NOT appear is the actual push instructions.
    assert "push to origin" not in msg.lower()
    assert "create branch" not in msg.lower()


# ─── HumanNote ───────────────────────────────────────────────────


def test_human_note_to_prompt():
    note = HumanNote(content="also handle the 429 case")
    prompt = note.to_prompt()
    assert "also handle the 429 case" in prompt
    assert "Human note" in prompt


def test_human_note_empty_content_allowed():
    # Empty notes are allowed — agent sees an empty note is fine
    note = HumanNote(content="")
    assert note.content == ""
