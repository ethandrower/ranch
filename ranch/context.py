"""Build a markdown context block of relevant lessons to inject into a new CC session."""
from fnmatch import fnmatch
from .db import db_session
from .models import Lesson


def get_relevant_lessons(
    *,
    file_paths: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
) -> list[Lesson]:
    with db_session() as db:
        all_lessons = db.query(Lesson).filter(Lesson.is_active == 1).all()

    matched = []
    for l in all_lessons:
        if l.applies_always:
            matched.append((l, 100))
            continue
        score = 0
        if file_paths and l.applies_to_files:
            for pattern in l.applies_to_files:
                if any(fnmatch(fp, pattern) for fp in file_paths):
                    score += 10
        if tags and l.applies_to_tags:
            overlap = set(tags) & set(l.applies_to_tags)
            score += 10 * len(overlap)
        if l.confidence >= 3:
            score += l.confidence
        if score > 0:
            matched.append((l, score))

    matched.sort(key=lambda x: (x[1], x[0].confidence), reverse=True)
    return [l for l, _ in matched[:limit]]


def format_context_markdown(lessons: list[Lesson]) -> str:
    if not lessons:
        return "## Lessons from previous tickets\n\n(none yet)"
    lines = ["## Lessons from previous tickets", ""]
    by_cat: dict[str, list[Lesson]] = {}
    for l in lessons:
        by_cat.setdefault(l.category, []).append(l)
    for cat, items in sorted(by_cat.items()):
        lines.append(f"### {cat.replace('_', ' ').title()}")
        for l in items:
            stars = "★" * l.confidence
            lines.append(f"- {stars} {l.content} _(reinforced {l.times_reinforced}x)_")
        lines.append("")
    return "\n".join(lines)
