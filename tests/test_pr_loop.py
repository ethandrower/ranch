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
