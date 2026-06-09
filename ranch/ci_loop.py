"""H20 Phase 2 — CI status polling for in-flight PRs.

The literal "20-min build, I switch tasks, I forget" closer. The hand
polls each in-flight PR's CI status on a cadence; when status flips
(running → green / red), it persists the new state on a `PRCIStatus`
row and emits a dossier update so the operator sees "build green on
PR #123" without scrolling logs.

This is a SIGNAL detector — it watches CI but doesn't itself resume the
agent's SDK session. On red, the operator decides whether to fire
`ranch respond-pr` (the agent triages the build failure same way it
triages review comments). On green, the operator decides whether to
merge.

Scope of this module:
  - poll_ci_for_run(run_id) → CIPollResult (new state, flipped, error)
  - runs_pending_ci_check(agent) → candidates the hand should poll
  - persist last-seen status on the PRCIStatus table (one row per
    (run_id, commit_sha) — append-only audit trail)

The hand integration (cadence + wake-on-flip) lives in hand.py.

Phase 3 (deferred) — auto-respond to red builds via the existing
respond-pr machinery. For now we surface the signal and let the
operator act.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import db_session
from .models import Dossier, PRCIStatus, Run


TERMINAL_RUN_STATES = {"completed", "stopped", "error"}

# Normalized status set the hand reasons about. Underlying CI systems
# emit a wider vocabulary (bb: SUCCESSFUL/FAILED/IN_PROGRESS/STOPPED;
# gh: completed/in_progress/queued × conclusion=success/failure/...).
# We collapse to this set so the hand's logic is uniform.
CIStatus = str  # one of: "queued", "running", "passed", "failed", "stopped", "unknown"


# ─── Result types ─────────────────────────────────────────────────


@dataclass
class CIPollResult:
    """Outcome of one `poll_ci_for_run` call."""

    ok: bool
    pr_id: Optional[str] = None
    commit_sha: Optional[str] = None
    status: Optional[CIStatus] = None  # latest normalized status
    flipped: bool = False              # True when status changed from last seen
    previous_status: Optional[CIStatus] = None
    reason: Optional[str] = None       # populated when ok=False


@dataclass
class PRCICandidate:
    """A Run the hand should consider polling for CI signal."""

    run_id: int
    agent: str
    ticket: Optional[str]
    pr_id: str
    pr_platform: str
    cwd: str


# ─── Backends ─────────────────────────────────────────────────────


def _normalize_bb_status(raw: dict) -> tuple[CIStatus, str | None]:
    """Map a bb pipeline entry → (normalized status, commit_sha)."""
    state = (raw.get("state") or {}).get("name", "")
    result = ((raw.get("state") or {}).get("result") or {}).get("name", "")
    sha = (raw.get("target") or {}).get("commit", {}).get("hash")

    # Bitbucket pipeline states: PENDING | IN_PROGRESS | COMPLETED | STOPPED
    # When COMPLETED, look at result: SUCCESSFUL | FAILED | ERROR | STOPPED
    if state == "PENDING":
        return ("queued", sha)
    if state == "IN_PROGRESS":
        return ("running", sha)
    if state == "STOPPED":
        return ("stopped", sha)
    if state == "COMPLETED":
        if result == "SUCCESSFUL":
            return ("passed", sha)
        if result in ("FAILED", "ERROR"):
            return ("failed", sha)
        if result == "STOPPED":
            return ("stopped", sha)
    return ("unknown", sha)


def _normalize_gh_status(raw: dict) -> tuple[CIStatus, str | None]:
    """Map a gh workflow run → (normalized status, commit_sha)."""
    status = raw.get("status", "")
    conclusion = raw.get("conclusion") or ""
    sha = raw.get("headSha")
    if status == "queued":
        return ("queued", sha)
    if status == "in_progress":
        return ("running", sha)
    if status == "completed":
        if conclusion == "success":
            return ("passed", sha)
        if conclusion in ("failure", "timed_out", "cancelled"):
            return ("failed", sha)
    return ("unknown", sha)


def _bb_pipelines_for_pr(pr_id: str, cwd: Path) -> tuple[CIStatus, str | None] | None:
    """Fetch the latest pipeline run for a Bitbucket PR. None if bb can't run
    or no pipelines exist."""
    try:
        proc = subprocess.run(
            ["bb", "--json", "pipeline", "list", "--limit", "5"],
            cwd=str(cwd), capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    # bb pipelines aren't filtered by PR in CLI; we take the most recent
    # entry. Refinement (filter by source branch) is a follow-up.
    if not isinstance(rows, list) or not rows:
        return None
    return _normalize_bb_status(rows[0])


def _gh_runs_for_pr(pr_id: str, cwd: Path) -> tuple[CIStatus, str | None] | None:
    """Fetch the most recent workflow run for the head commit of a GH PR.
    Returns None if gh can't run or no runs match."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "checks", str(pr_id), "--json", "name,state,bucket,workflow"],
            cwd=str(cwd), capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    # Aggregate: if ANY failed → failed; elif ANY running → running; else passed.
    # `bucket` is one of: pass / fail / pending / skipping / cancel.
    buckets = {r.get("bucket") for r in rows}
    if "fail" in buckets or "cancel" in buckets:
        status: CIStatus = "failed"
    elif "pending" in buckets:
        status = "running"
    else:
        status = "passed"
    # gh pr checks doesn't expose the SHA per row; the consumer can read it
    # from the Run row if needed. None here is acceptable.
    return (status, None)


# ─── Public polling entry point ───────────────────────────────────


def poll_ci_for_run(run_id: int) -> CIPollResult:
    """Fetch the latest CI status for the run's PR; persist a PRCIStatus row
    when the status differs from the last seen value (flip detection).
    """
    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            return CIPollResult(ok=False, reason=f"run #{run_id} not found")
        if not run.pr_id:
            return CIPollResult(ok=False, reason=f"run #{run_id} has no pr_id")
        if not run.cwd:
            return CIPollResult(ok=False, reason=f"run #{run_id} has no cwd")
        agent = run.agent
        pr_id = run.pr_id
        platform = run.pr_platform or "bb"
        cwd = Path(run.cwd)

        previous = (
            db.query(PRCIStatus)
            .filter_by(run_id=run_id)
            .order_by(PRCIStatus.fetched_at.desc())
            .first()
        )
        previous_status: str | None = previous.status if previous else None

    if platform == "bb":
        fetched = _bb_pipelines_for_pr(pr_id, cwd)
    elif platform == "gh":
        fetched = _gh_runs_for_pr(pr_id, cwd)
    else:
        return CIPollResult(ok=False, pr_id=pr_id,
                            reason=f"unknown platform {platform!r}")

    if fetched is None:
        return CIPollResult(
            ok=True, pr_id=pr_id, status=previous_status,
            reason="CI backend returned no usable result (no pipelines yet?)",
        )

    new_status, sha = fetched

    flipped = previous_status is not None and new_status != previous_status
    # Persist a row only when something changed — keeps the audit trail signal-dense
    if previous_status != new_status:
        with db_session() as db:
            db.add(PRCIStatus(
                run_id=run_id,
                pr_id=pr_id,
                commit_sha=sha,
                status=new_status,
                fetched_at=datetime.now(timezone.utc),
            ))

    return CIPollResult(
        ok=True,
        pr_id=pr_id,
        commit_sha=sha,
        status=new_status,
        flipped=flipped,
        previous_status=previous_status,
    )


# ─── Finding runs to poll ─────────────────────────────────────────


def runs_pending_ci_check(agent: Optional[str] = None) -> list[PRCICandidate]:
    """Runs the hand should consider polling for CI signal: terminal-state
    runs with a pr_id set (i.e., the PR has been opened). Agent-scoped
    when `agent` is provided."""
    with db_session() as db:
        q = db.query(Run).filter(Run.state.in_(TERMINAL_RUN_STATES))
        q = q.filter(Run.pr_id.isnot(None))
        if agent:
            q = q.filter(Run.agent == agent)
        runs = q.order_by(Run.ended_at.desc()).all()
        return [
            PRCICandidate(
                run_id=r.id, agent=r.agent, ticket=r.ticket,
                pr_id=r.pr_id, pr_platform=r.pr_platform or "bb",
                cwd=r.cwd,
            )
            for r in runs
        ]


# ─── Dossier event for the operator ───────────────────────────────


def emit_ci_flip_dossier(run_id: int, result: CIPollResult) -> None:
    """Write a Dossier row narrating the CI flip — what the operator sees
    in `ranch fleet --watch` / dossier views without having to read logs."""
    if not result.flipped or not result.status:
        return
    label = {
        "passed": "CI passed",
        "failed": "CI failed",
        "running": "CI running",
        "queued": "CI queued",
        "stopped": "CI stopped",
    }.get(result.status, f"CI {result.status}")
    just_did = f"{label} on PR #{result.pr_id}"
    payload = {
        "plan": [],
        "just_did": just_did,
        "state": "researching",
        "details": (
            f"CI status flipped: {result.previous_status or 'unknown'} → {result.status}.\n"
            f"Commit: {result.commit_sha or '?'}"
        ),
    }
    with db_session() as db:
        db.add(Dossier(
            run_id=run_id,
            state="researching",
            payload_json=json.dumps(payload),
        ))
