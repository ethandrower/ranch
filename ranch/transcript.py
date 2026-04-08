"""Parse Claude Code session transcript files."""
import json
from pathlib import Path


def read_transcript(transcript_path: str | Path) -> list[dict]:
    """Read a CC session transcript JSONL file."""
    p = Path(transcript_path)
    if not p.exists():
        return []
    entries = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def get_last_assistant_turn(entries: list[dict]) -> dict | None:
    """Find the most recent assistant turn in the transcript."""
    for entry in reversed(entries):
        if entry.get("type") == "assistant" or entry.get("role") == "assistant":
            return entry
    return None


def extract_text_and_tools(assistant_entry: dict) -> tuple[str, list[dict]]:
    """Pull the text content and tool uses from an assistant turn."""
    if not assistant_entry:
        return "", []

    content = (
        assistant_entry.get("message", {}).get("content")
        or assistant_entry.get("content", [])
    )
    if isinstance(content, str):
        return content, []

    text_parts = []
    tool_uses = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_uses.append({
                "name": block.get("name"),
                "input": block.get("input", {}),
            })
    return "\n".join(text_parts), tool_uses


def get_recent_file_paths(entries: list[dict], n: int = 10) -> list[str]:
    """Pull recently-touched file paths from the transcript for context."""
    files = []
    for entry in reversed(entries):
        content = (
            entry.get("message", {}).get("content")
            or entry.get("content", [])
        )
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                inp = block.get("input", {})
                fp = inp.get("file_path") or inp.get("path")
                if fp and fp not in files:
                    files.append(fp)
                    if len(files) >= n:
                        return files
    return files
