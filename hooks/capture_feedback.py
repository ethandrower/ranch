#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook.
Logs the user message as feedback for the active ticket (detected from branch name).
Detects ticket switches and triggers async reflection on the previous ticket.
"""
import json
import sys
import os
import subprocess
from pathlib import Path

# Make sure ranch is importable
RANCH_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RANCH_ROOT))

try:
    from ranch.config import agent_for_cwd, RANCH_HOME
    from ranch.feedback import log_feedback, detect_ticket_from_branch
    from ranch.models import FeedbackSource
except Exception as e:
    err_log = Path.home() / ".ranch" / "hook_errors.log"
    err_log.parent.mkdir(exist_ok=True)
    err_log.open("a").write(f"import error: {e}\n")
    sys.exit(0)

STATE_FILE = RANCH_HOME / "active_tickets.json"
VENV_PYTHON = RANCH_ROOT / ".venv" / "bin" / "python"


def get_active_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_active_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    cwd = Path(payload.get("cwd") or os.getcwd())
    agent = agent_for_cwd(cwd)
    agent_name = agent.name if agent else None

    # Detect git branch + ticket
    branch = None
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=str(cwd),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        pass

    ticket_id = detect_ticket_from_branch(branch)

    # Detect ticket switch — fire async reflection on the old ticket
    state = get_active_state()
    state_key = f"{agent_name or 'unknown'}:{payload.get('session_id', '')}"
    last_ticket = state.get(state_key)

    if last_ticket and ticket_id and last_ticket != ticket_id:
        subprocess.Popen(
            [str(VENV_PYTHON), "-m", "ranch.reflect_cli", last_ticket],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(RANCH_ROOT),
        )

    if ticket_id:
        state[state_key] = ticket_id
        save_active_state(state)

    user_message = payload.get("prompt") or payload.get("user_message") or ""
    if user_message and ticket_id:
        try:
            log_feedback(
                user_message=user_message,
                session_id=payload.get("session_id", ""),
                transcript_path=payload.get("transcript_path"),
                cwd=str(cwd),
                agent_name=agent_name,
                branch=branch,
                source=FeedbackSource.USER_CORRECTION,
            )
        except Exception as e:
            err_log = Path.home() / ".ranch" / "hook_errors.log"
            err_log.open("a").write(f"log_feedback error: {e}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
