"""Tests for Plan D — PR feedback loop.

Covers:
- PR backend parsing for bb and gh (mocked subprocess)
- `ranch poll-pr` — auto-discovery, dedupe, loop-friendly output
- `ranch respond-pr` — error paths (no PR, no session)
- `ranch resolve-comment`
- triage checkpoint added to APPROVAL_REQUIRED
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ranch.cli import cli
from ranch.db import db_session, init_db
from ranch.models import ReviewComment, Run
from ranch.runner.checkpoints import APPROVAL_REQUIRED
from ranch.runner.pr_backend import BBBackend, GHBackend, PRBackendError, detect_platform


def _make_subprocess_result(stdout: str = "", returncode: int = 0, stderr: str = ""):
    mock = MagicMock()
    mock.stdout = stdout
    mock.stderr = stderr
    mock.returncode = returncode
    return mock


def _make_run(**kwargs) -> int:
    defaults = dict(
        agent="max", ticket="PR-TEST", cwd="/tmp", initial_prompt="b",
        state="completed",
    )
    defaults.update(kwargs)
    init_db()
    with db_session() as db:
        run = Run(**defaults)
        db.add(run)
        db.flush()
        return run.id


# ─── APPROVAL_REQUIRED ────────────────────────────────────────────


def test_triage_is_approval_required():
    """triage must be an approval checkpoint — the whole point is the human gate."""
    assert "triage" in APPROVAL_REQUIRED
    assert "plan_ready" in APPROVAL_REQUIRED
    assert "pre_push" in APPROVAL_REQUIRED


# ─── BBBackend parsing ────────────────────────────────────────────


def test_bb_discover_pr_matches_source_branch():
    fake_prs = json.dumps([
        {"id": 101, "source": {"branch": {"name": "other-branch"}},
         "links": {"html": {"href": "https://bb/.../101"}}},
        {"id": 367, "source": {"branch": {"name": "pr/ECD-1602-x"}},
         "links": {"html": {"href": "https://bb/.../367"}}},
    ])
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(stdout=fake_prs)):
        result = BBBackend().discover_pr_by_branch("pr/ECD-1602-x", Path("/tmp"))
    assert result == ("367", "https://bb/.../367")


def test_bb_discover_pr_returns_none_when_no_match():
    fake_prs = json.dumps([
        {"id": 101, "source": {"branch": {"name": "other"}}, "links": {}},
    ])
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(stdout=fake_prs)):
        result = BBBackend().discover_pr_by_branch("missing", Path("/tmp"))
    assert result is None


def test_bb_fetch_comments_normalizes_structure():
    fake_view = json.dumps({
        "comments": [
            {
                "id": 781577556,
                "content": {"raw": "Consider fixing X"},
                "user": {"display_name": "reviewer1"},
                "created_on": "2026-04-12T21:30:49.624912+00:00",
                "inline": {"path": "src/foo.py", "to": 42},
                "deleted": False,
                "pending": False,
            },
            # deleted comment — should be filtered
            {"id": 999, "deleted": True, "content": {"raw": "x"}, "user": {"display_name": "r"}, "pending": False},
            # pending comment — filtered
            {"id": 1000, "pending": True, "content": {"raw": "x"}, "user": {"display_name": "r"}, "deleted": False},
            # top-level (no inline)
            {
                "id": 781577557,
                "content": {"raw": "Also nit"},
                "user": {"display_name": "reviewer2"},
                "created_on": "2026-04-13T00:00:00+00:00",
                "deleted": False,
                "pending": False,
            },
        ]
    })
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(stdout=fake_view)):
        comments = BBBackend().fetch_comments("367", Path("/tmp"))

    assert len(comments) == 2
    assert comments[0].platform_comment_id == "781577556"
    assert comments[0].author == "reviewer1"
    assert comments[0].file_path == "src/foo.py"
    assert comments[0].line_number == 42
    assert comments[0].body == "Consider fixing X"
    assert comments[0].created_at_remote is not None
    assert comments[1].file_path is None
    assert comments[1].line_number is None


def test_bb_post_reply_passes_reply_to():
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result()) as mock_run:
        BBBackend().post_reply("367", "sounds good", Path("/tmp"), reply_to="781577556")
    argv = mock_run.call_args[0][0]
    assert argv[:4] == ["bb", "pr", "comment", "367"]
    assert "--reply-to" in argv
    assert "781577556" in argv


def test_bb_backend_raises_on_non_json():
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(stdout="not-json")):
        with pytest.raises(PRBackendError):
            BBBackend().discover_pr_by_branch("any", Path("/tmp"))


def test_bb_backend_raises_on_nonzero_exit():
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(returncode=1, stderr="auth failed")):
        with pytest.raises(PRBackendError, match="auth failed"):
            BBBackend().discover_pr_by_branch("any", Path("/tmp"))


# ─── GHBackend parsing ────────────────────────────────────────────


def test_gh_discover_pr_returns_first_match():
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(
                   stdout=json.dumps([{"number": 123, "url": "https://github.com/x/y/pull/123"}]))):
        result = GHBackend().discover_pr_by_branch("feature-x", Path("/tmp"))
    assert result == ("123", "https://github.com/x/y/pull/123")


def test_gh_fetch_comments_merges_review_and_issue():
    review_body = json.dumps([
        {"id": 111, "user": {"login": "alice"}, "body": "nit", "path": "a.py",
         "line": 7, "created_at": "2026-04-10T00:00:00Z"},
    ])
    issue_body = json.dumps([
        {"id": 222, "user": {"login": "bob"}, "body": "general comment",
         "created_at": "2026-04-11T00:00:00Z"},
    ])
    # subprocess.run is called twice — once for each API endpoint
    with patch("ranch.runner.pr_backend.subprocess.run",
               side_effect=[
                   _make_subprocess_result(stdout=review_body),
                   _make_subprocess_result(stdout=issue_body),
               ]):
        comments = GHBackend().fetch_comments("123", Path("/tmp"))

    assert len(comments) == 2
    ids = {c.platform_comment_id for c in comments}
    assert "review:111" in ids
    assert "issue:222" in ids


# ─── detect_platform ──────────────────────────────────────────────


def test_detect_platform_bitbucket():
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(
                   stdout="git@bitbucket.org:org/repo.git")):
        assert detect_platform(Path("/tmp")) == "bb"


def test_detect_platform_github():
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(
                   stdout="git@github.com:org/repo.git")):
        assert detect_platform(Path("/tmp")) == "gh"


def test_detect_platform_unknown_returns_none():
    with patch("ranch.runner.pr_backend.subprocess.run",
               return_value=_make_subprocess_result(stdout="git@gitlab.com:a/b.git")):
        assert detect_platform(Path("/tmp")) is None


# ─── ranch poll-pr ────────────────────────────────────────────────


def test_poll_pr_unknown_run():
    init_db()
    result = CliRunner().invoke(cli, ["poll-pr", "99999"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_poll_pr_auto_discovers_and_fetches(tmp_path):
    run_id = _make_run(branch_name="pr/ECD-1602-x", pr_platform="bb",
                       cwd=str(tmp_path))

    from ranch.runner.pr_backend import FetchedComment
    canned = [
        FetchedComment("c1", "alice", "a.py", 1, "first", None),
        FetchedComment("c2", "bob", None, None, "second", None),
    ]

    with patch("ranch.runner.pr_backend.BBBackend.discover_pr_by_branch",
               return_value=("367", "https://bb/.../367")), \
         patch("ranch.runner.pr_backend.BBBackend.fetch_comments",
               return_value=canned):
        result = CliRunner().invoke(cli, ["poll-pr", str(run_id)])

    assert result.exit_code == 0, result.output
    assert "2 new comment" in result.output
    assert "ranch respond-pr" in result.output

    with db_session() as db:
        r = db.query(Run).filter_by(id=run_id).one()
        assert r.pr_id == "367"
        assert r.pr_url == "https://bb/.../367"
        rows = db.query(ReviewComment).filter_by(run_id=run_id).all()
        assert {row.platform_comment_id for row in rows} == {"c1", "c2"}


def test_poll_pr_dedupes_on_second_call(tmp_path):
    run_id = _make_run(branch_name="b", pr_id="367", pr_platform="bb",
                       pr_url="https://bb/.../367", cwd=str(tmp_path))
    # Seed an existing comment
    with db_session() as db:
        db.add(ReviewComment(run_id=run_id, platform_comment_id="c1",
                             author="alice", body="first"))

    from ranch.runner.pr_backend import FetchedComment
    canned = [
        FetchedComment("c1", "alice", None, None, "first", None),  # dup
        FetchedComment("c3", "carol", None, None, "third", None),  # new
    ]

    with patch("ranch.runner.pr_backend.BBBackend.fetch_comments",
               return_value=canned):
        result = CliRunner().invoke(cli, ["poll-pr", str(run_id)])

    assert result.exit_code == 0
    assert "1 new comment" in result.output

    with db_session() as db:
        ids = {r.platform_comment_id for r in
               db.query(ReviewComment).filter_by(run_id=run_id).all()}
        assert ids == {"c1", "c3"}


def test_poll_pr_quiet_when_no_new_comments(tmp_path):
    """Loop-friendly output contract: no new comments → single-line quiet output."""
    run_id = _make_run(branch_name="b", pr_id="367", pr_platform="bb",
                       cwd=str(tmp_path))
    with patch("ranch.runner.pr_backend.BBBackend.fetch_comments",
               return_value=[]):
        result = CliRunner().invoke(cli, ["poll-pr", str(run_id)])

    assert result.exit_code == 0
    assert "no new comments" in result.output.lower()
    assert "new comment(s)" not in result.output


def test_poll_pr_no_pr_discovered_exits_silently(tmp_path):
    """If no PR is open yet for the branch, exit 0 silently (loop-friendly)."""
    run_id = _make_run(branch_name="feature-x", pr_platform="bb", cwd=str(tmp_path))
    with patch("ranch.runner.pr_backend.BBBackend.discover_pr_by_branch",
               return_value=None):
        result = CliRunner().invoke(cli, ["poll-pr", str(run_id)])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_poll_pr_missing_branch_aborts(tmp_path):
    """Without branch_name and without --pr, we can't discover anything."""
    run_id = _make_run(branch_name=None, pr_platform="bb", cwd=str(tmp_path))
    result = CliRunner().invoke(cli, ["poll-pr", str(run_id)])
    assert result.exit_code != 0
    assert "branch_name" in result.output.lower()


def test_poll_pr_manual_pr_override(tmp_path):
    """--pr <id> bypasses discovery even when branch_name is missing."""
    run_id = _make_run(branch_name=None, pr_platform="bb", cwd=str(tmp_path))
    with patch("ranch.runner.pr_backend.BBBackend.fetch_comments",
               return_value=[]):
        result = CliRunner().invoke(cli, ["poll-pr", str(run_id), "--pr", "999"])
    assert result.exit_code == 0
    assert "no new comments on PR #999" in result.output


# ─── ranch respond-pr error paths ────────────────────────────────


def test_respond_pr_no_run():
    init_db()
    result = CliRunner().invoke(cli, ["respond-pr", "99999"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_respond_pr_no_pr_attached():
    run_id = _make_run(pr_id=None)
    result = CliRunner().invoke(cli, ["respond-pr", str(run_id)])
    assert result.exit_code != 0
    assert "no pr" in result.output.lower() or "poll-pr" in result.output.lower()


def test_respond_pr_no_sdk_session():
    run_id = _make_run(pr_id="367", sdk_session_id=None)
    result = CliRunner().invoke(cli, ["respond-pr", str(run_id)])
    assert result.exit_code != 0
    assert "sdk session" in result.output.lower()


def test_respond_pr_no_unresolved_comments_is_noop():
    """If every comment is already resolved, respond-pr exits cleanly."""
    run_id = _make_run(pr_id="367", sdk_session_id="sess-xyz")
    with db_session() as db:
        db.add(ReviewComment(run_id=run_id, platform_comment_id="c1",
                             author="a", body="x", resolved=1))
    result = CliRunner().invoke(cli, ["respond-pr", str(run_id)])
    assert result.exit_code == 0
    assert "no unresolved" in result.output.lower()


# ─── ranch resolve-comment ───────────────────────────────────────


def test_resolve_comment_marks_resolved_with_sha():
    run_id = _make_run()
    with db_session() as db:
        db.add(ReviewComment(run_id=run_id, platform_comment_id="c1",
                             author="a", body="x"))
    result = CliRunner().invoke(cli, [
        "resolve-comment", str(run_id), "c1", "--sha", "abc123",
    ])
    assert result.exit_code == 0
    with db_session() as db:
        row = db.query(ReviewComment).filter_by(run_id=run_id, platform_comment_id="c1").one()
        assert row.resolved == 1
        assert row.resolved_commit_sha == "abc123"


def test_resolve_comment_unknown_comment():
    run_id = _make_run()
    result = CliRunner().invoke(cli, ["resolve-comment", str(run_id), "nonexistent"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ═══════════════════════════════════════════════════════════════════
#  H20 Phase 1 — tests for the reusable ranch/pr_loop.py functions.
#  The above tests cover the CLI surface; these cover the pure
#  helpers that the ranch hand calls into.
# ═══════════════════════════════════════════════════════════════════

from datetime import timedelta as _td

from ranch.models import Dossier as _Dossier
from ranch.pr_loop import (
    PRPollCandidate,
    filter_by_poll_cadence,
    poll_pr_for_run,
    runs_pending_pr_review,
)
from ranch.runner.pr_backend import FetchedComment as _FC


def _make_run_h20(
    *, agent: str = "max", ticket: str = "ECD-1",
    state: str = "completed",
    pr_id: str | None = None,
    branch: str | None = "feature/ECD-1",
    cwd: str = "/tmp",
    ended_at: datetime | None = None,
    last_pr_check_at: datetime | None = None,
) -> int:
    init_db()
    with db_session() as db:
        run = Run(
            agent=agent, ticket=ticket, cwd=cwd, initial_prompt="x",
            state=state, branch_name=branch, pr_id=pr_id,
            ended_at=ended_at or datetime.now(timezone.utc),
            last_pr_check_at=last_pr_check_at,
        )
        if pr_id:
            run.pr_platform = "bb"
        db.add(run)
        db.flush()
        return run.id


def _add_dossier_h20(run_id: int, state: str = "parked"):
    with db_session() as db:
        payload = {"state": state, "just_did": "x", "plan": [], "blocker": "x"}
        db.add(_Dossier(run_id=run_id, state=state, payload_json=json.dumps(payload)))


def _fc(id_: str, body: str = "lgtm", author: str = "ethan") -> _FC:
    return _FC(
        platform_comment_id=id_, author=author,
        file_path=None, line_number=None, body=body,
        created_at_remote=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


# ─── poll_pr_for_run ──────────────────────────────────────────────


def test_h20_poll_returns_error_for_missing_run():
    init_db()
    r = poll_pr_for_run(99_999)
    assert r.ok is False
    assert "not found" in r.reason


def test_h20_poll_handles_no_branch_and_no_pr():
    rid = _make_run_h20(branch=None, pr_id=None)
    r = poll_pr_for_run(rid)
    assert r.ok is False
    assert "no branch_name" in r.reason


def test_h20_poll_loop_friendly_when_pr_not_yet_open():
    """Discovery returns None → not an error, just nothing to do yet."""
    rid = _make_run_h20(branch="feature/ECD-1", pr_id=None)
    backend = MagicMock()
    backend.discover_pr_by_branch = MagicMock(return_value=None)
    with patch("ranch.pr_loop.get_backend", return_value=backend), \
         patch("ranch.pr_loop.detect_platform", return_value="bb"):
        r = poll_pr_for_run(rid)
    assert r.ok is True
    assert r.pr_id is None
    assert r.new_comment_count == 0


def test_h20_poll_discovery_persists_pr_id():
    rid = _make_run_h20(branch="feature/ECD-1", pr_id=None)
    backend = MagicMock()
    backend.discover_pr_by_branch = MagicMock(return_value=("42", "https://bb/pr/42"))
    backend.fetch_comments = MagicMock(return_value=[])
    with patch("ranch.pr_loop.get_backend", return_value=backend), \
         patch("ranch.pr_loop.detect_platform", return_value="bb"):
        r = poll_pr_for_run(rid)
    assert r.ok and r.pr_id == "42"
    with db_session() as db:
        run = db.query(Run).filter_by(id=rid).one()
        assert run.pr_id == "42"
        assert run.pr_url == "https://bb/pr/42"


def test_h20_poll_persists_new_comments_and_is_idempotent():
    rid = _make_run_h20(pr_id="42")
    backend = MagicMock()
    backend.fetch_comments = MagicMock(return_value=[_fc("c1"), _fc("c2")])
    with patch("ranch.pr_loop.get_backend", return_value=backend), \
         patch("ranch.pr_loop.detect_platform", return_value="bb"):
        r1 = poll_pr_for_run(rid)
        assert r1.ok and r1.new_comment_count == 2
        r2 = poll_pr_for_run(rid)
    assert r2.new_comment_count == 0
    with db_session() as db:
        rows = db.query(ReviewComment).filter_by(run_id=rid).all()
    assert {r.platform_comment_id for r in rows} == {"c1", "c2"}


def test_h20_poll_surfaces_backend_error_cleanly():
    rid = _make_run_h20(pr_id="42")
    backend = MagicMock()
    backend.fetch_comments = MagicMock(side_effect=PRBackendError("bb auth failed"))
    with patch("ranch.pr_loop.get_backend", return_value=backend), \
         patch("ranch.pr_loop.detect_platform", return_value="bb"):
        r = poll_pr_for_run(rid)
    assert r.ok is False
    assert "comment fetch failed" in r.reason
    assert "bb auth failed" in r.reason


def test_h20_poll_touches_last_check_at_even_when_no_comments():
    rid = _make_run_h20(pr_id="42")
    backend = MagicMock()
    backend.fetch_comments = MagicMock(return_value=[])
    with patch("ranch.pr_loop.get_backend", return_value=backend), \
         patch("ranch.pr_loop.detect_platform", return_value="bb"):
        poll_pr_for_run(rid)
    with db_session() as db:
        run = db.query(Run).filter_by(id=rid).one()
        assert run.last_pr_check_at is not None
        # SQLite returns naive datetimes; compare with naive utcnow.
        delta = datetime.utcnow() - run.last_pr_check_at
        assert delta.total_seconds() < 5


# ─── runs_pending_pr_review ───────────────────────────────────────


def test_h20_pending_excludes_non_terminal_runs():
    rid = _make_run_h20(state="planning", pr_id="42")
    _add_dossier_h20(rid, "parked")
    assert runs_pending_pr_review("max") == []


def test_h20_pending_excludes_runs_without_pr_or_branch():
    rid = _make_run_h20(pr_id=None, branch=None)
    _add_dossier_h20(rid, "parked")
    assert runs_pending_pr_review("max") == []


def test_h20_pending_excludes_when_dossier_not_parked():
    rid = _make_run_h20(pr_id="42")
    _add_dossier_h20(rid, "coding")
    assert runs_pending_pr_review("max") == []


def test_h20_pending_includes_when_all_conditions_met():
    rid = _make_run_h20(pr_id="42")
    _add_dossier_h20(rid, "parked")
    out = runs_pending_pr_review("max")
    assert len(out) == 1
    assert out[0].run_id == rid
    assert out[0].pr_id == "42"


def test_h20_pending_scoped_by_agent():
    rid_jeffy = _make_run_h20(agent="jeffy", pr_id="50")
    _add_dossier_h20(rid_jeffy, "parked")
    rid_max = _make_run_h20(agent="max", pr_id="51")
    _add_dossier_h20(rid_max, "parked")
    assert [c.run_id for c in runs_pending_pr_review("max")] == [rid_max]


def test_h20_pending_no_agent_filter_returns_all():
    rid_a = _make_run_h20(agent="max", pr_id="60")
    _add_dossier_h20(rid_a, "parked")
    rid_b = _make_run_h20(agent="jeffy", pr_id="61")
    _add_dossier_h20(rid_b, "parked")
    ids = {c.run_id for c in runs_pending_pr_review()}
    assert ids == {rid_a, rid_b}


def test_h20_pending_can_skip_parked_filter():
    rid = _make_run_h20(pr_id="42")
    _add_dossier_h20(rid, "coding")
    out = runs_pending_pr_review("max", require_parked_dossier=False)
    assert len(out) == 1


# ─── filter_by_poll_cadence ───────────────────────────────────────


def _cand(run_id: int = 1, last: datetime | None = None) -> PRPollCandidate:
    return PRPollCandidate(
        run_id=run_id, agent="max", ticket="ECD-1",
        pr_id="42", pr_platform="bb", branch_name="b",
        last_check_at=last,
    )


def test_h20_cadence_passes_never_polled():
    out = filter_by_poll_cadence([_cand(last=None)], interval_seconds=60)
    assert len(out) == 1


def test_h20_cadence_drops_recently_polled():
    recent = datetime.now(timezone.utc) - _td(seconds=10)
    out = filter_by_poll_cadence([_cand(last=recent)], interval_seconds=60)
    assert out == []


def test_h20_cadence_passes_old_enough():
    old = datetime.now(timezone.utc) - _td(seconds=120)
    out = filter_by_poll_cadence([_cand(last=old)], interval_seconds=60)
    assert len(out) == 1


def test_h20_cadence_independent_per_candidate():
    recent = datetime.now(timezone.utc) - _td(seconds=10)
    old = datetime.now(timezone.utc) - _td(seconds=120)
    out = filter_by_poll_cadence(
        [_cand(run_id=1, last=recent), _cand(run_id=2, last=old)],
        interval_seconds=60,
    )
    assert [c.run_id for c in out] == [2]
