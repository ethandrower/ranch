"""Tests for ranch/labs.py — H9 Phase 1.

Covers:
  - load_labs_config: pure defaults, fleet override, per-agent override, mix
  - ensure_dokku_remote: missing / matching / divergent
  - wait_for_health: 2xx / 3xx / 4xx / 5xx / connection error / timeout
  - deploy_run_to_labs: missing run, no cwd, no branch, git push fail,
    health pass, health fail
  - CLI integration via CliRunner
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from ranch.cli import cli
from ranch.db import db_session, init_db
from ranch.labs import (
    DeployResult,
    HealthResult,
    LabsConfig,
    _from_templates,
    deploy_run_to_labs,
    ensure_dokku_remote,
    load_labs_config,
    wait_for_health,
)
from ranch.models import Run


# ─── Helpers ──────────────────────────────────────────────────────


def _make_run(
    *, agent: str = "max", branch: str | None = "feature/ECD-1",
    cwd: str = "/tmp", state: str = "completed",
) -> int:
    init_db()
    with db_session() as db:
        run = Run(
            agent=agent, ticket="ECD-1", cwd=cwd, initial_prompt="x",
            state=state, branch_name=branch,
        )
        db.add(run)
        db.flush()
        return run.id


# ─── load_labs_config ────────────────────────────────────────────


def test_config_pure_defaults_when_no_file(tmp_path, monkeypatch):
    """No config.toml → pure citemed defaults."""
    monkeypatch.setattr("ranch.labs.CONFIG_FILE", tmp_path / "nope.toml")
    cfg = load_labs_config("max")
    assert cfg.agent == "max"
    assert cfg.host == "dokku@178.105.80.165"
    assert cfg.app == "dev-max"
    assert cfg.remote == "dokku-max"
    assert cfg.url == "https://max.staging.citemed.com"
    assert cfg.health_path == "/"


def test_config_fleet_overrides_apply(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[dokku]\n'
        'host = "dokku@example.com"\n'
        'url_template = "https://{agent}.example.com"\n'
        'app_template = "{agent}-app"\n'
        'remote_template = "x-{agent}"\n'
    )
    monkeypatch.setattr("ranch.labs.CONFIG_FILE", cfg_path)
    cfg = load_labs_config("jeffy")
    assert cfg.host == "dokku@example.com"
    assert cfg.app == "jeffy-app"
    assert cfg.remote == "x-jeffy"
    assert cfg.url == "https://jeffy.example.com"


def test_config_per_agent_overrides_win(tmp_path, monkeypatch):
    """Per-agent values override fleet templates."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[dokku]\n'
        'url_template = "https://{agent}.staging.citemed.com"\n'
        '\n'
        '[agents.arnold.dokku]\n'
        'url = "https://arnold-special.citemed.com"\n'
        'app = "dev-arnold-test"\n'
    )
    monkeypatch.setattr("ranch.labs.CONFIG_FILE", cfg_path)
    cfg = load_labs_config("arnold")
    assert cfg.url == "https://arnold-special.citemed.com"
    assert cfg.app == "dev-arnold-test"
    # remote falls back to template
    assert cfg.remote == "dokku-arnold"


def test_config_per_agent_inherits_unspecified_values(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[dokku]\n'
        'health_path = "/healthz"\n'
        '\n'
        '[agents.max.dokku]\n'
        'app = "dev-max-override"\n'
    )
    monkeypatch.setattr("ranch.labs.CONFIG_FILE", cfg_path)
    cfg = load_labs_config("max")
    assert cfg.health_path == "/healthz"  # inherited
    assert cfg.app == "dev-max-override"  # overridden


def test_from_templates_substitutes_agent_token():
    cfg = _from_templates("kesha", {
        "host": "h",
        "url_template": "u-{agent}",
        "app_template": "a-{agent}",
        "remote_template": "r-{agent}",
        "health_path": "/",
        "deploy_timeout_seconds": 1,
    })
    assert cfg.app == "a-kesha"
    assert cfg.remote == "r-kesha"
    assert cfg.url == "u-kesha"


# ─── ensure_dokku_remote ─────────────────────────────────────────


def test_ensure_remote_adds_when_missing(tmp_path):
    # Init a real bare repo so git -C works
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    ok, msg = ensure_dokku_remote(tmp_path, "dokku-max", "dokku@host:dev-max")
    assert ok is True
    assert "added remote" in msg
    # Verify it really got added
    out = subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "get-url", "dokku-max"],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == "dokku@host:dev-max"


def test_ensure_remote_noop_when_correct(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "dokku-max", "dokku@host:dev-max"],
        check=True,
    )
    ok, msg = ensure_dokku_remote(tmp_path, "dokku-max", "dokku@host:dev-max")
    assert ok is True
    assert "already configured" in msg


def test_ensure_remote_refuses_to_silently_repoint(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "dokku-max", "dokku@host:OLD"],
        check=True,
    )
    ok, msg = ensure_dokku_remote(tmp_path, "dokku-max", "dokku@host:dev-max")
    assert ok is False
    assert "OLD" in msg
    assert "dev-max" in msg


# ─── wait_for_health ──────────────────────────────────────────────


def _patch_httpx_to_return(status_code: int | None = None, raise_error: bool = False):
    """Build a context manager that swaps httpx.Client for one returning a
    fixed response (or raising)."""
    fake_client = MagicMock()
    if raise_error:
        fake_client.head = MagicMock(side_effect=httpx.ConnectError("refused"))
    else:
        resp = MagicMock()
        resp.status_code = status_code
        fake_client.head = MagicMock(return_value=resp)
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    return patch("ranch.labs.httpx.Client", return_value=fake_client)


def test_health_ok_on_200():
    with _patch_httpx_to_return(200):
        r = wait_for_health("https://x", timeout_seconds=1, interval_seconds=0.01)
    assert r.ok is True
    assert r.status_code == 200


def test_health_ok_on_redirect():
    """A 302 to /login is a perfectly valid 'app is up' signal."""
    with _patch_httpx_to_return(302):
        r = wait_for_health("https://x", timeout_seconds=1, interval_seconds=0.01)
    assert r.ok is True
    assert r.status_code == 302


def test_health_fail_on_5xx():
    with _patch_httpx_to_return(503):
        r = wait_for_health("https://x", timeout_seconds=0.05, interval_seconds=0.01)
    assert r.ok is False
    assert r.status_code == 503


def test_health_fail_on_4xx():
    """A 404 means the URL/path is wrong, not 'app is up'."""
    with _patch_httpx_to_return(404):
        r = wait_for_health("https://x", timeout_seconds=0.05, interval_seconds=0.01)
    assert r.ok is False


def test_health_fail_on_connection_error():
    with _patch_httpx_to_return(raise_error=True):
        r = wait_for_health("https://x", timeout_seconds=0.05, interval_seconds=0.01)
    assert r.ok is False
    assert "error" in (r.error or "")


def test_health_builds_url_with_health_path():
    captured = {}
    fake_client = MagicMock()
    def fake_head(url, *args, **kwargs):
        captured["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        return resp
    fake_client.head = fake_head
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    with patch("ranch.labs.httpx.Client", return_value=fake_client):
        wait_for_health("https://x.com/", "/healthz",
                         timeout_seconds=1, interval_seconds=0.01)
    assert captured["url"] == "https://x.com/healthz"


# ─── deploy_run_to_labs ───────────────────────────────────────────


def test_deploy_missing_run():
    init_db()
    r = deploy_run_to_labs(99_999)
    assert r.ok is False
    assert "not found" in r.reason


def test_deploy_missing_branch(tmp_path):
    rid = _make_run(branch=None, cwd=str(tmp_path))
    r = deploy_run_to_labs(rid)
    assert r.ok is False
    assert "branch_name" in r.reason


def test_deploy_git_push_failure_surfaces_output(tmp_path):
    rid = _make_run(cwd=str(tmp_path))
    # Init repo so remote management works
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    with patch("ranch.labs._git") as fake_git:
        # First call: remote get-url returns nonzero (missing)
        # Second call: remote add → ok
        # Third call: push → fail
        fake_git.side_effect = [
            (1, "", "no such remote"),
            (0, "", ""),
            (128, "", "fatal: deploy denied"),
        ]
        r = deploy_run_to_labs(rid)
    assert r.ok is False
    assert "push failed" in r.reason
    assert "deploy denied" in r.push_output


def test_deploy_happy_path(tmp_path):
    """Push succeeds, health responds 200 → labs_url + labs_deployed_at set."""
    rid = _make_run(cwd=str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    with patch("ranch.labs._git") as fake_git, \
         _patch_httpx_to_return(200):
        # remote missing → add → push success
        fake_git.side_effect = [
            (1, "", "no remote"),
            (0, "", ""),
            (0, "successful deploy log", ""),
        ]
        r = deploy_run_to_labs(rid, health_timeout_seconds=1)

    assert r.ok is True
    assert r.url == "https://max.staging.citemed.com"
    with db_session() as db:
        run = db.query(Run).filter_by(id=rid).one()
        assert run.labs_url == "https://max.staging.citemed.com"
        assert run.labs_deployed_at is not None


def test_deploy_push_ok_but_health_fails(tmp_path):
    rid = _make_run(cwd=str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    with patch("ranch.labs._git") as fake_git, \
         _patch_httpx_to_return(503):
        fake_git.side_effect = [
            (1, "", "no remote"),
            (0, "", ""),
            (0, "deploy ok", ""),
        ]
        r = deploy_run_to_labs(rid, health_timeout_seconds=0.05, health_poll_interval_seconds=0.01)

    assert r.ok is False
    assert "health check failed" in r.reason
    # URL should still be reported (deploy succeeded; only health failed)
    assert r.url == "https://max.staging.citemed.com"
    # labs_url NOT set (we only set on full success)
    with db_session() as db:
        run = db.query(Run).filter_by(id=rid).one()
        assert run.labs_url is None


def test_deploy_passes_force_flag_when_requested(tmp_path):
    rid = _make_run(cwd=str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    captured_pushes = []
    with patch("ranch.labs._git") as fake_git, \
         _patch_httpx_to_return(200):
        def side(args, cwd, timeout=None):
            if "push" in args:
                captured_pushes.append(args)
            return (0, "", "") if "push" not in args[0] else (0, "ok", "")
        fake_git.side_effect = [
            (1, "", "no remote"),
            (0, "", ""),
            (0, "deploy ok", ""),
        ]
        # Need to allow side_effect to capture args; use a list of returns
        # The push is the 3rd call; build a wrapper that captures args
        def capturing(args, cwd, timeout=None):
            captured_pushes.append(args)
            n = len(captured_pushes)
            return [(1, "", "no remote"), (0, "", ""), (0, "deploy ok", "")][n - 1]
        fake_git.side_effect = capturing
        deploy_run_to_labs(rid, force=True, health_timeout_seconds=1)

    push_call = captured_pushes[-1]
    assert "push" in push_call
    assert "--force" in push_call


# ─── CLI integration ─────────────────────────────────────────────


def test_cli_labs_deploy_happy_path(tmp_path):
    rid = _make_run(cwd=str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    with patch("ranch.labs._git") as fake_git, \
         _patch_httpx_to_return(200):
        fake_git.side_effect = [
            (1, "", "no remote"),
            (0, "", ""),
            (0, "deploy ok\nline 2", ""),
        ]
        result = CliRunner().invoke(cli, ["labs", "deploy", str(rid), "--health-timeout", "1"])

    assert result.exit_code == 0
    assert "Deploy live" in result.output
    assert "max.staging.citemed.com" in result.output


def test_cli_labs_deploy_failure_aborts(tmp_path):
    rid = _make_run(cwd=str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    with patch("ranch.labs._git") as fake_git:
        fake_git.side_effect = [
            (1, "", "no remote"),
            (0, "", ""),
            (128, "", "fatal: nope"),
        ]
        result = CliRunner().invoke(cli, ["labs", "deploy", str(rid)])
    assert result.exit_code != 0
    assert "Deploy failed" in result.output
