"""Tests for Pydantic message models in ranch/runner/messages.py."""
import pytest
from pydantic import ValidationError

from ranch.runner.messages import (
    CheckpointInput,
    DecisionLogInput,
    DossierOption,
    HumanDecision,
    HumanNote,
    PlanStep,
    RecordStateInput,
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


# ─── RecordStateInput / dossier (Phase H1) ───────────────────────


def _minimal_state(**overrides):
    base = {
        "plan": [{"step": "Read the ticket", "status": "done"}],
        "just_did": "Finished reading the ticket and the linked epic.",
        "state": "planning",
    }
    base.update(overrides)
    return base


def test_record_state_minimal_valid():
    payload = RecordStateInput.model_validate(_minimal_state())
    assert payload.state == "planning"
    assert payload.plan[0].status == "done"
    assert payload.blocker is None
    assert payload.options is None
    assert payload.files_touched == []


def test_record_state_full_parked_with_options():
    data = _minimal_state(
        state="parked",
        blocker="Plan approval needed — see options.",
        options=[
            {"label": "approve", "description": "Proceed with the proposed plan."},
            {"label": "split", "description": "Split into ECD-1234a and ECD-1234b."},
        ],
        files_touched=["foo.py", "bar.py"],
        ticket="ECD-1234",
    )
    payload = RecordStateInput.model_validate(data)
    assert payload.state == "parked"
    assert payload.options is not None
    assert len(payload.options) == 2
    assert payload.options[0].label == "approve"
    assert payload.ticket == "ECD-1234"


def test_record_state_rejects_bad_state():
    with pytest.raises(ValidationError):
        RecordStateInput.model_validate(_minimal_state(state="not_a_real_state"))


def test_record_state_rejects_bad_plan_status():
    with pytest.raises(ValidationError):
        RecordStateInput.model_validate(
            _minimal_state(plan=[{"step": "Do thing", "status": "halfway"}])
        )


def test_record_state_rejects_empty_just_did():
    with pytest.raises(ValidationError, match="just_did must not be empty"):
        RecordStateInput.model_validate(_minimal_state(just_did="   "))


def test_record_state_requires_required_fields():
    with pytest.raises(ValidationError):
        RecordStateInput.model_validate({"plan": [], "just_did": "x"})  # missing state


def test_plan_step_notes_optional():
    step = PlanStep(step="Wire MCP tool", status="in_progress")
    assert step.notes is None
    step2 = PlanStep(step="Wire MCP tool", status="in_progress", notes="schema first")
    assert step2.notes == "schema first"


def test_dossier_option_requires_both_fields():
    DossierOption(label="approve", description="Go ahead.")
    with pytest.raises(ValidationError):
        DossierOption(label="approve")  # missing description
