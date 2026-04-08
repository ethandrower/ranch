"""Subprocess entry point for hook-triggered reflection. Logs to a file."""
import sys
from pathlib import Path
from datetime import datetime
from .reflect import reflect_sync

LOG_FILE = Path.home() / ".ranch" / "reflection.log"


def main():
    if len(sys.argv) < 2:
        return
    ticket_id = sys.argv[1]
    LOG_FILE.parent.mkdir(exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"\n=== {datetime.now().isoformat()} reflecting on {ticket_id} ===\n")
        try:
            result = reflect_sync(ticket_id)
            f.write(f"{result}\n")
        except Exception as e:
            f.write(f"ERROR: {e}\n")


if __name__ == "__main__":
    main()
