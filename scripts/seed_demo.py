"""Seed the dev ranch.db with a representative dataset.

Idempotent — clears the dev tables we own (initiatives, hand_initiatives,
blocks, hand_events) and writes a fresh consistent snapshot. Existing Run
/ Dossier / Checkpoint rows from earlier dev sessions are also wiped so
the kanban renders only the seeded fixtures.

Run via:
    RANCH_DATABASE_URL="sqlite:///.ranch-dev/ranch.db" \
        ./.venv/bin/python scripts/seed_demo.py
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ranch.db import db_session, init_db
from ranch.events import emit_event
from ranch.models import (
    Block,
    Checkpoint,
    Dossier,
    HandEvent,
    HandInitiative,
    Initiative,
    ReviewComment,
    Run,
)


def utcago(*, seconds: int = 0, minutes: int = 0, hours: int = 0, days: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)


def wipe() -> None:
    with db_session() as s:
        s.query(Block).delete()
        s.query(ReviewComment).delete()
        s.query(Dossier).delete()
        s.query(Checkpoint).delete()
        s.query(HandEvent).delete()
        s.query(Run).delete()
        s.query(HandInitiative).delete()
        s.query(Initiative).delete()


def seed() -> None:
    init_db()
    wipe()

    with db_session() as s:
        # Initiatives — global
        for k, label in [
            ("ref-mgmt", "Reference Management"),
            ("scrapers", "Scrapers infra"),
            ("perf", "Performance"),
            ("misc", "Misc & one-offs"),
        ]:
            s.add(Initiative(key=k, label=label))
        s.flush()

        # Hand <-> initiative mapping
        for hand, watches, default in [
            ("max", ["ref-mgmt", "misc"], "ref-mgmt"),
            ("jeffy", ["ref-mgmt", "scrapers", "misc"], "ref-mgmt"),
            ("arnold", ["scrapers", "perf", "misc"], "scrapers"),
            ("kesha", ["scrapers", "misc"], "scrapers"),
        ]:
            for order, init_key in enumerate(watches):
                s.add(HandInitiative(
                    hand_name=hand,
                    initiative_key=init_key,
                    is_default=int(init_key == default),
                    sort_order=order,
                ))
        s.flush()

    # ─── max — fully populated ──────────────────────────────────────
    with db_session() as s:
        # ECD-2105 — triage, queued
        s.add(Run(
            agent="max", ticket="ECD-2105", state="queued", cwd="/tmp",
            initial_prompt="Improve search hit caching — reduce repeat search-hit DB hits on the lit-review index page.",
            initiative_key="ref-mgmt",
            started_at=utcago(minutes=8),
        ))

        # ECD-2078 — blocked
        s.add(Run(
            agent="max", ticket="ECD-2078", state="queued", cwd="/tmp",
            initial_prompt="Migrate old search endpoint to v3 — sunset /api/search/v1.",
            initiative_key="ref-mgmt",
            started_at=utcago(minutes=15),
        ))

        # ECD-2073 — plan_ready, parked
        r2073 = Run(
            agent="max", ticket="ECD-2073", state="parked", cwd="/tmp",
            initial_prompt="Performance deploy — Celery worker scaling. Improve throughput on hot Celery queues.",
            initiative_key="ref-mgmt",
            started_at=utcago(minutes=30),
        )
        s.add(r2073); s.flush()
        s.add(Checkpoint(
            run_id=r2073.id, kind="plan_ready", summary="5-step plan drafted",
            created_at=utcago(minutes=12),
        ))
        s.add(Dossier(
            run_id=r2073.id, state="parked",
            payload_json=json.dumps({
                "plan": [
                    {"step": "Read epic + sister tickets", "status": "done"},
                    {"step": "Pull prod metrics for queue depth", "status": "done"},
                    {"step": "Draft 5-step plan (complexity M)", "status": "done"},
                ],
                "just_did": "Parked at plan_ready awaiting operator review of the proposed plan.",
                "state": "parked",
                "details": "Drafted the worker scaling plan. Key decision: split scrape-dispatch off the default queue onto a dedicated worker pool. Expected to cut p95 dispatch latency by ~40% per prod metrics from last 7 days.",
                "recommended_action": "deploy",
                "recommendation_reason": "needs release-phase validation in realistic Procfile",
            }),
            created_at=utcago(minutes=12),
        ))

        # ECD-1880 — code, in flight
        r1880 = Run(
            agent="max", ticket="ECD-1880", state="in_development", cwd="/tmp",
            initial_prompt="Add audit log for migration runs. Every migration writes a row to migration_audit.",
            initiative_key="ref-mgmt",
            started_at=utcago(minutes=42),
        )
        s.add(r1880); s.flush()
        s.add(Dossier(
            run_id=r1880.id, state="coding",
            payload_json=json.dumps({
                "plan": [
                    {"step": "Inspect existing migration hook", "status": "done"},
                    {"step": "Write failing test for AuditLogWriter", "status": "done"},
                    {"step": "Implement AuditLogWriter", "status": "in_progress"},
                    {"step": "Wire into runner.apply_migrations", "status": "pending"},
                    {"step": "Acceptance + commit", "status": "pending"},
                ],
                "just_did": "Implementing AuditLogWriter. Just wrote the failing test; about to implement the writer.",
                "state": "coding",
                "details": "Migration hook lives in core/migrations/runner.py — single entry point apply_migrations(env). Decided to hook at the runner level, not per-migration, so the audit guarantee is centralized.",
            }),
            created_at=utcago(minutes=1),
        ))

        # ECD-2087 — pre_push, parked, needs approval
        r2087 = Run(
            agent="max", ticket="ECD-2087", state="parked", cwd="/tmp",
            initial_prompt="Restore post-scrape dedup dispatch step that was dropped in the 0317 migration.",
            initiative_key="ref-mgmt",
            started_at=utcago(minutes=25),
        )
        s.add(r2087); s.flush()
        s.add(Checkpoint(
            run_id=r2087.id, kind="pre_push", summary="Diff +88/-12 across 3 files. 4/4 acceptance green.",
            payload_json=json.dumps({"diff_stats": "+88/-12"}),
            created_at=utcago(minutes=2),
        ))
        for delta_s, plan, details in [
            (1200, [{"step": "Read scrape dispatch flow", "status": "done"}], "Walked the dispatch chain end-to-end. Found the bug: 0317 commit removed the post-dedup hook but the consumer kept calling it as a no-op stub."),
            (900,  [{"step": "Read scrape dispatch flow", "status": "done"},
                    {"step": "Added post-dedup dispatch step", "status": "done"}],
                   "Restored dispatch_post_dedup() in lit_reviews/dedup/dispatch.py. Sync (not async) because the consumer is already async and there's no benefit to async-ifying the inner step."),
            (600,  [{"step": "Read scrape dispatch flow", "status": "done"},
                    {"step": "Added post-dedup dispatch step", "status": "done"},
                    {"step": "Wired onComplete callback", "status": "done"}],
                   "Threaded run_id through the chord signature. Verified the callback fires before the task is marked complete (otherwise we'd race the next batch)."),
            (120,  [{"step": "Read scrape dispatch flow", "status": "done"},
                    {"step": "Added post-dedup dispatch step", "status": "done"},
                    {"step": "Wired onComplete callback", "status": "done"},
                    {"step": "Acceptance 4/4 green", "status": "done"}],
                   "Acceptance: pytest test_dedup_dispatch.py (3 cases) + scraper integration check. 4/4 green in 22s. Integration check is why deploy is recommended — needs the remote scraper fleet."),
        ]:
            s.add(Dossier(
                run_id=r2087.id,
                state="parked" if delta_s == 120 else "coding",
                payload_json=json.dumps({
                    "plan": plan,
                    "just_did": "Working through the plan.",
                    "state": "parked" if delta_s == 120 else "coding",
                    "details": details,
                    "recommended_action": "deploy" if delta_s == 120 else None,
                    "recommendation_reason": (
                        "acceptance has a real scraper integration check that needs the remote scraper fleet to validate end-to-end"
                        if delta_s == 120 else None
                    ),
                }),
                created_at=utcago(seconds=delta_s),
            ))

        # ECD-1762 — review with PR + unresolved comments + CI failed
        r1762 = Run(
            agent="max", ticket="ECD-1762", state="in_qa", cwd="/tmp",
            initial_prompt="Citation export — DOI formatting consistency across the export pipeline.",
            initiative_key="ref-mgmt",
            pr_id="1834", pr_platform="bb", pr_url="https://bitbucket.org/citemed/citemed_web/pull-requests/1834",
            started_at=utcago(hours=2),
        )
        s.add(r1762); s.flush()
        s.add(Checkpoint(
            run_id=r1762.id, kind="pre_push", summary="diff ready", decision="approved",
            decided_at=utcago(hours=1, minutes=30),
            created_at=utcago(hours=1, minutes=35),
        ))
        s.add(ReviewComment(
            run_id=r1762.id, platform_comment_id="bb-1834-1",
            author="vinod",
            body="Should this be Title Case before stripping? We had a bug where 'DOI: 10.x' became 'doi: 10.x'.",
            file_path="citations/export.py", line_number=88,
            resolved=0, fetched_at=utcago(minutes=4),
        ))
        s.add(Dossier(
            run_id=r1762.id, state="judging",
            payload_json=json.dumps({
                "plan": [
                    {"step": "Parsed reviewer expectations", "status": "done"},
                    {"step": "Triaged 2 comments — both AGREE", "status": "done"},
                    {"step": "Applied vinod's regex nit", "status": "done"},
                ],
                "just_did": "Just pushed fix for vinod's comments. CI was running, turned red 30s ago.",
                "state": "judging",
            }),
            created_at=utcago(seconds=30),
        ))

        # ECD-2055 + ECD-2032 — merged (drives the pill progress fill)
        for key, days_ago in [("ECD-2055", 2), ("ECD-2032", 4)]:
            s.add(Run(
                agent="max", ticket=key, state="merged", cwd="/tmp",
                initial_prompt=f"{key} — merged work for ref-mgmt initiative.",
                initiative_key="ref-mgmt",
                started_at=utcago(days=days_ago + 1),
                ended_at=utcago(days=days_ago),
            ))

        # ECD-2099 — misc orphan
        s.add(Run(
            agent="max", ticket="ECD-2099", state="queued", cwd="/tmp",
            initial_prompt="Add pagination headers to /api/articles — expose Link headers.",
            initiative_key="misc",
            started_at=utcago(minutes=4),
        ))

    # ─── Block: ECD-2078 blocked by ECD-2073 ───────────────────────
    from ranch.blocks import record_block as _record_block
    with db_session() as s:
        r2078 = s.query(Run).filter_by(ticket="ECD-2078").one()
        run_id = r2078.id
    _record_block(
        blocked_run_id=run_id,
        blocker_ticket="ECD-2073",
        reason="Migration shape depends on the worker-scaling decision pending in ECD-2073's plan_ready review.",
        source="agent",
    )

    # ─── arnold ────────────────────────────────────────────────────
    with db_session() as s:
        r1644 = Run(
            agent="arnold", ticket="ECD-1644", state="parked", cwd="/tmp",
            initial_prompt="MAUDE adverse-event ingester rate limit — add exponential backoff.",
            initiative_key="scrapers",
            started_at=utcago(minutes=24),
        )
        s.add(r1644); s.flush()
        s.add(Checkpoint(
            run_id=r1644.id, kind="pre_push", summary="Diff +36/-4. Acceptance 3/3 green.",
            created_at=utcago(minutes=22),
        ))
        s.add(Dossier(
            run_id=r1644.id, state="parked",
            payload_json=json.dumps({
                "plan": [
                    {"step": "Reproduce the rate-limit hit", "status": "done"},
                    {"step": "Add exponential backoff (max 30s, 5 retries)", "status": "done"},
                    {"step": "Acceptance: pytest + curl smoke", "status": "done"},
                ],
                "just_did": "Acceptance 3/3 green. Small contained logic diff.",
                "state": "parked",
                "details": "Backoff: 1s → 2s → 4s → 8s → 16s capped at 30s. Honors Retry-After when present, falls back to the curve when absent (about 1 in 8 requests return raw 429 with no header).",
                "recommended_action": "no_deploy",
                "recommendation_reason": "all acceptance is unit_test + script (localhost) — change is contained logic, no UI / no public URL",
            }),
            created_at=utcago(minutes=22),
        ))

        # ECD-1410 — review with reviewer push-back
        r1410 = Run(
            agent="arnold", ticket="ECD-1410", state="in_qa", cwd="/tmp",
            initial_prompt="Worker spatial pre-parse — index rebuild optimization.",
            initiative_key="perf",
            pr_id="1620", pr_platform="bb",
            started_at=utcago(hours=3),
        )
        s.add(r1410); s.flush()
        s.add(Checkpoint(
            run_id=r1410.id, kind="pre_push", summary="ready", decision="approved",
            decided_at=utcago(hours=2),
            created_at=utcago(hours=2, minutes=5),
        ))
        s.add(ReviewComment(
            run_id=r1410.id, platform_comment_id="bb-1620-1",
            author="mohamed",
            body="Why rebuild on every dispatch? The whole point of the cached parse was to avoid this. Push back unless I'm missing something.",
            file_path="workers/spatial.py", line_number=142,
            resolved=0, fetched_at=utcago(minutes=6),
        ))

    # ─── jeffy ─────────────────────────────────────────────────────
    with db_session() as s:
        r1853 = Run(
            agent="jeffy", ticket="ECD-1853", state="in_qa", cwd="/tmp",
            initial_prompt="Lit-reviews migration merge fix.",
            initiative_key="ref-mgmt",
            started_at=utcago(minutes=10),
        )
        s.add(r1853); s.flush()
        s.add(Dossier(
            run_id=r1853.id, state="testing",
            payload_json=json.dumps({
                "plan": [
                    {"step": "Reproduce merge conflict", "status": "done"},
                    {"step": "Patch migration tree", "status": "done"},
                    {"step": "Running pytest + migration smoke", "status": "in_progress"},
                ],
                "just_did": "Running pytest + migration smoke check now.",
                "state": "testing",
            }),
            created_at=utcago(seconds=45),
        ))

        s.add(Run(
            agent="jeffy", ticket="ECD-2071", state="queued", cwd="/tmp",
            initial_prompt="PubMed search — handle 429 backoff cleanly.",
            initiative_key="scrapers",
            started_at=utcago(minutes=3),
        ))

    # ─── kesha ─────────────────────────────────────────────────────
    with db_session() as s:
        r1580 = Run(
            agent="kesha", ticket="ECD-1410-k", state="in_qa", cwd="/tmp",
            initial_prompt="PubMed callback signing v2 — HMAC instead of static token.",
            initiative_key="scrapers",
            pr_id="1580", pr_platform="bb",
            started_at=utcago(days=2),
        )
        s.add(r1580); s.flush()
        s.add(Checkpoint(
            run_id=r1580.id, kind="pre_push", summary="ready", decision="approved",
            decided_at=utcago(days=2),
            created_at=utcago(days=2),
        ))
        s.add(Dossier(
            run_id=r1580.id, state="judging",
            payload_json=json.dumps({
                "plan": [
                    {"step": "Implementation", "status": "done"},
                    {"step": "Acceptance 5/5 green", "status": "done"},
                    {"step": "PR opened", "status": "done"},
                ],
                "just_did": "PR open for 2 days with no review activity. Branch up to date with develop.",
                "state": "judging",
            }),
            created_at=utcago(days=2),
        ))

    # ─── Events log ────────────────────────────────────────────────
    emit_event(hand_name="max", kind="ci_flip", title="CI failed on PR #1834 (ECD-1762)",
               detail="build went running → failed", severity="bad", ticket="ECD-1762")
    emit_event(hand_name="max", kind="review_comment", title="2 new review comments on ECD-1762",
               detail="from vinod; auto-triage queued", severity="info", ticket="ECD-1762")
    emit_event(hand_name="max", kind="triage", title="Triaged ECD-2073 → propose",
               detail="score 78, top of queue", severity="info", ticket="ECD-2073")
    emit_event(hand_name="max", kind="approval", title="Approved propose ECD-2087 → exec",
               detail="operator approval received; execute started", severity="good", ticket="ECD-2087")
    emit_event(hand_name="arnold", kind="review_comment", title="Review push-back from mohamed",
               detail="on ECD-1410 — re scope of index rebuild", severity="warn", ticket="ECD-1410")
    emit_event(hand_name="arnold", kind="state_transition", title="run_acceptance · 3/3 green on ECD-1644",
               detail="MAUDE rate-limit fix", severity="good", ticket="ECD-1644")
    emit_event(hand_name="jeffy", kind="state_transition", title="run_acceptance · 4/4 green",
               detail="tests + smoke + integration on ECD-1853", severity="good", ticket="ECD-1853")
    emit_event(hand_name="kesha", kind="state_transition", title="PR #1580 idle 2 days",
               detail="no review activity; nothing to do", severity="info", ticket="ECD-1410-k")


if __name__ == "__main__":
    seed()
    print("✓ dev DB seeded — 4 hands, 4 initiatives, 12 runs")
    print("  ./venv/bin/ranch view-hand max --json | head -30  # smoke")
