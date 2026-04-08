"""Reflection runner — distills unprocessed feedback into lessons."""
import asyncio
import json
import time
from datetime import datetime, timezone
from claude_code_sdk import (
    query,
    ClaudeCodeOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)
from .db import db_session
from .models import Ticket, Feedback, Lesson, ReflectionRun, LessonCategory
from .feedback import unprocessed_for_ticket, mark_processed
from .lessons import create_lesson, reinforce_lesson, list_active

REFLECTION_PROMPT = """You are a reflection agent. Your job is to read all feedback (corrections, comments, signals) received during a single development ticket and extract REUSABLE LESSONS for future tickets.

# Current ticket
{ticket_id} — {ticket_title}
Agent: {agent_name}

# Existing lessons (do NOT duplicate; reinforce instead)
{existing_lessons}

# Feedback received during this ticket ({feedback_count} items)
{feedback_dump}

# Your task

For each piece of feedback, decide:

1. **Is this a one-off** (specific to this ticket, not generalizable)? → SKIP it.
2. **Is this a NEW general pattern**? → Create a new lesson.
3. **Does this REINFORCE an existing lesson**? → Reinforce it.

Output ONLY a JSON object with this exact shape:

```json
{{
  "new_lessons": [
    {{
      "content": "Clear, actionable lesson statement",
      "category": "code_style|architecture|testing|tooling|reviewer_preference|error_handling|security|performance|django_specific|repo_convention|other",
      "applies_to_files": ["**/serializers.py"],
      "applies_to_tags": ["django", "api"],
      "applies_always": false,
      "confidence": 1,
      "source_feedback_ids": [12, 34]
    }}
  ],
  "reinforced_lessons": [
    {{ "lesson_id": 5, "source_feedback_ids": [56] }}
  ],
  "skipped_feedback_ids": [78, 90],
  "summary": "Created 2 new lessons, reinforced 1, skipped 3 one-offs"
}}
```

Be SELECTIVE. A reviewer saying "add a blank line here" is NOT a lesson. A reviewer saying "we always use factory_boy for fixtures in this repo" IS a lesson. Quality over quantity. Aim for 0-3 new lessons per ticket on average.

Output the JSON object and nothing else."""


def format_feedback_for_prompt(feedback_rows: list[Feedback]) -> str:
    lines = []
    for f in feedback_rows:
        lines.append(f"--- Feedback #{f.id} ({f.timestamp.isoformat()}) ---")
        if f.prior_assistant_text:
            preview = f.prior_assistant_text[:500].replace("\n", " ")
            lines.append(f"Assistant just said: {preview}")
        if f.prior_tool_uses:
            tool_names = [t.get("name", "?") for t in f.prior_tool_uses[:5]]
            lines.append(f"Assistant just used tools: {', '.join(tool_names)}")
        if f.file_context:
            lines.append(f"Recent files: {', '.join(f.file_context[:5])}")
        lines.append(f"USER said: {f.user_message}")
        lines.append("")
    return "\n".join(lines)


def format_existing_lessons(lessons: list[Lesson]) -> str:
    if not lessons:
        return "(none yet)"
    lines = []
    for l in lessons[:30]:
        lines.append(f"- [id={l.id}] [{l.category}] (conf {l.confidence}) {l.content}")
    return "\n".join(lines)


async def run_reflection(ticket_id: str) -> dict:
    """Run reflection for a single ticket. Returns a summary dict."""
    started = time.time()

    with db_session() as db:
        ticket = db.query(Ticket).filter_by(ticket_id=ticket_id).first()
        if not ticket:
            return {"error": f"Ticket {ticket_id} not found"}
        ticket_title = ticket.title or "(no title)"
        ticket_agent = ticket.agent_name or "?"

    feedback_rows = unprocessed_for_ticket(ticket_id)
    if not feedback_rows:
        return {"summary": "No unprocessed feedback", "feedback_count": 0}

    existing = list_active()
    prompt = REFLECTION_PROMPT.format(
        ticket_id=ticket_id,
        ticket_title=ticket_title,
        agent_name=ticket_agent,
        existing_lessons=format_existing_lessons(existing),
        feedback_count=len(feedback_rows),
        feedback_dump=format_feedback_for_prompt(feedback_rows),
    )

    full_text = ""
    cost_cents = 0
    async for message in query(
        prompt=prompt,
        options=ClaudeCodeOptions(
            allowed_tools=[],
            permission_mode="bypassPermissions",
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    full_text += block.text
        elif isinstance(message, ResultMessage):
            cost_cents = int((message.total_cost_usd or 0) * 100)

    parsed = _parse_json_block(full_text)
    if not parsed:
        with db_session() as db:
            db.add(ReflectionRun(
                ticket_id=ticket_id,
                agent_name=ticket_agent,
                feedback_count=len(feedback_rows),
                duration_seconds=int(time.time() - started),
                cost_cents=cost_cents,
                error="Failed to parse JSON from reflection response",
                summary=full_text[:500],
            ))
        return {"error": "Failed to parse reflection output", "raw": full_text[:500]}

    new_count = 0
    reinforced_count = 0
    all_processed_ids: set[int] = set()

    for new in parsed.get("new_lessons", []):
        create_lesson(
            content=new["content"],
            category=new.get("category", LessonCategory.OTHER.value),
            source_ticket_ids=[ticket_id],
            source_feedback_ids=new.get("source_feedback_ids", []),
            applies_to_files=new.get("applies_to_files"),
            applies_to_tags=new.get("applies_to_tags"),
            applies_always=new.get("applies_always", False),
            confidence=new.get("confidence", 1),
        )
        new_count += 1
        all_processed_ids.update(new.get("source_feedback_ids", []))

    for r in parsed.get("reinforced_lessons", []):
        reinforce_lesson(
            lesson_id=r["lesson_id"],
            ticket_id=ticket_id,
            feedback_ids=r.get("source_feedback_ids", []),
        )
        reinforced_count += 1
        all_processed_ids.update(r.get("source_feedback_ids", []))

    all_processed_ids.update(parsed.get("skipped_feedback_ids", []))

    if all_processed_ids:
        mark_processed(list(all_processed_ids))

    with db_session() as db:
        t = db.query(Ticket).filter_by(ticket_id=ticket_id).one()
        t.reflected_at = datetime.now(timezone.utc)
        db.add(ReflectionRun(
            ticket_id=ticket_id,
            agent_name=t.agent_name,
            feedback_count=len(feedback_rows),
            lessons_created=new_count,
            lessons_reinforced=reinforced_count,
            duration_seconds=int(time.time() - started),
            cost_cents=cost_cents,
            summary=parsed.get("summary", ""),
        ))

    return {
        "ticket_id": ticket_id,
        "feedback_count": len(feedback_rows),
        "new_lessons": new_count,
        "reinforced": reinforced_count,
        "summary": parsed.get("summary", ""),
        "cost_cents": cost_cents,
    }


def _parse_json_block(text: str) -> dict | None:
    """Pull a JSON object out of the response, even if wrapped in ```json fences."""
    text = text.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None


def reflect_sync(ticket_id: str) -> dict:
    return asyncio.run(run_reflection(ticket_id))
