"""Per-run state machine."""
from __future__ import annotations

RUN_STATES = {
    "queued", "planning", "in_development", "in_qa",
    "needs_approval", "completed", "stopped", "error",
}

VALID_TRANSITIONS: dict[str, set[str]] = {
    "queued":         {"planning", "stopped"},
    "planning":       {"in_development", "needs_approval", "stopped", "error"},
    "in_development": {"in_qa", "needs_approval", "stopped", "error"},
    "in_qa":          {"completed", "in_development", "needs_approval", "stopped", "error"},
    "needs_approval": RUN_STATES - {"queued"},
}


def transition(run, new_state: str, *, session) -> None:
    allowed = VALID_TRANSITIONS.get(run.state, set())
    if new_state not in allowed:
        raise ValueError(f"Illegal state transition: {run.state!r} → {new_state!r}")
    if new_state == "needs_approval":
        run.state_before_pause = run.state
    run.state = new_state
    session.flush()
