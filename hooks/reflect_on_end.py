#!/usr/bin/env python3
"""
Claude Code SessionEnd hook.
Triggers reflection on whatever ticket was active for this session.
"""
import json
import sys
import subprocess
from pathlib import Path

RANCH_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RANCH_ROOT))

try:
    from ranch.config import RANCH_HOME
except Exception:
    sys.exit(0)

STATE_FILE = RANCH_HOME / "active_tickets.json"
VENV_PYTHON = RANCH_ROOT / ".venv" / "bin" / "python"


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    if not STATE_FILE.exists():
        sys.exit(0)

    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        sys.exit(0)

    session_id = payload.get("session_id", "")
    target_key = None
    for key in list(state.keys()):
        if key.endswith(f":{session_id}"):
            target_key = key
            break

    if not target_key:
        sys.exit(0)

    ticket_id = state.pop(target_key, None)
    STATE_FILE.write_text(json.dumps(state))

    if ticket_id:
        subprocess.Popen(
            [str(VENV_PYTHON), "-m", "ranch.reflect_cli", ticket_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(RANCH_ROOT),
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
