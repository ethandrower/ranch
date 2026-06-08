"""H9 Phase 1 — per-hand staging deploy primitives.

Each ranch hand deploys to its OWN per-hand Dokku staging app — there
is no shared "labs" catch-all in this model. dev-ethan at
labs.staging.citemed.com is a notional 5th hand handled via the same
per-agent config override mechanism, not a special target.

Deploys an agent's run to its hand's Dokku app (per the citemed
`infra/dokku/CHEATSHEET.md` conventions), polls the public URL until
the app responds, and persists the URL + deployed_at timestamp on the
Run row.

Conventions inherited verbatim from the cheatsheet:
  - Dokku always deploys whatever is pushed to `main`, regardless of
    source branch (`git push <remote> <source>:main`).
  - The remote alias per hand is `dokku-<agent>`.
  - The app on the host is `dev-<agent>`.
  - Public URL is `<agent>.staging.citemed.com`.
  - Health check: `curl -I` on root — 2xx OR 3xx counts as alive.

Auto-fire from the hand's poll loop is deliberately NOT part of this
design: the staging box is memory-tight (4 hands * 1500MB + pre-prod +
remington) and most ticket work doesn't actually need a deploy to
validate. Deploys are operator-driven via `ranch deploy <run_id>`,
with the agent putting a "deploy recommended"/"deploy not needed" hint
on the pre_push parked dossier based on its acceptance contract shape.

Phase 1 (this module): the deploy primitive + state inspection.
Phase 2 (follow-up): the agent's recommendation field on the dossier.
Phase 3 (follow-up): H10 PR draft body gets the deploy_url section.
"""
from __future__ import annotations

import subprocess
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .config import CONFIG_FILE
from .db import db_session
from .models import Run


# ─── Config ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeployConfig:
    """Resolved labs config for one agent — fleet defaults + per-agent overrides."""

    agent: str
    host: str  # e.g. "dokku@178.105.80.165"
    app: str   # e.g. "dev-max"
    remote: str  # e.g. "dokku-max"
    url: str  # e.g. "https://max.staging.citemed.com"
    health_path: str  # e.g. "/"
    deploy_timeout_seconds: int


class DeployConfigError(RuntimeError):
    """Raised when the labs config can't be loaded or is incomplete."""


# Sensible defaults that match the citemed staging convention exactly.
# Override-points live in ~/.ranch/config.toml under [dokku] and
# [agents.<name>.dokku]; nothing here needs to change for a green-path setup.
_DEFAULT_FLEET = {
    "host": "dokku@178.105.80.165",
    "url_template": "https://{agent}.staging.citemed.com",
    "app_template": "dev-{agent}",
    "remote_template": "dokku-{agent}",
    "health_path": "/",
    "deploy_timeout_seconds": 600,
}


def load_deploy_config(agent: str) -> DeployConfig:
    """Resolve DeployConfig for one agent. Raises DeployConfigError on misconfig."""
    if not CONFIG_FILE.exists():
        # Use pure defaults — gives a working config in dev-from-scratch.
        return _from_templates(agent, _DEFAULT_FLEET)

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    fleet = {**_DEFAULT_FLEET, **(data.get("dokku") or {})}
    per_agent = ((data.get("agents") or {}).get(agent) or {}).get("dokku") or {}

    # Per-agent explicit values take precedence over template-rendered defaults.
    if per_agent:
        rendered = _from_templates(agent, fleet)
        merged = {
            "agent": agent,
            "host": per_agent.get("host", rendered.host),
            "app": per_agent.get("app", rendered.app),
            "remote": per_agent.get("remote", rendered.remote),
            "url": per_agent.get("url", rendered.url),
            "health_path": per_agent.get("health_path", rendered.health_path),
            "deploy_timeout_seconds": int(
                per_agent.get("deploy_timeout_seconds", rendered.deploy_timeout_seconds)
            ),
        }
        return DeployConfig(**merged)

    return _from_templates(agent, fleet)


def _from_templates(agent: str, fleet: dict) -> DeployConfig:
    return DeployConfig(
        agent=agent,
        host=str(fleet["host"]),
        app=str(fleet["app_template"]).format(agent=agent),
        remote=str(fleet["remote_template"]).format(agent=agent),
        url=str(fleet["url_template"]).format(agent=agent),
        health_path=str(fleet["health_path"]),
        deploy_timeout_seconds=int(fleet.get("deploy_timeout_seconds", 600)),
    )


# ─── Result types ──────────────────────────────────────────────────


@dataclass
class HealthResult:
    """Outcome of polling the public URL after a deploy."""

    ok: bool
    status_code: int | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0


@dataclass
class DeployResult:
    """End-to-end deploy result returned by deploy_run."""

    ok: bool
    url: str | None = None
    push_output: str = ""  # tail of git push stdout/stderr — useful when ok=False
    health: HealthResult | None = None
    elapsed_seconds: float = 0.0
    reason: str | None = None  # populated when ok=False AND nothing more specific applies


# ─── Git remote management ─────────────────────────────────────────


def _git(args: list[str], cwd: Path, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a git command in a worktree. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return -1, "", f"git invocation failed: {e}"
    return proc.returncode, proc.stdout, proc.stderr


def ensure_dokku_remote(cwd: Path, remote: str, expected_url: str) -> tuple[bool, str]:
    """Make sure the named git remote exists and points at `expected_url`.

    If missing → add it.
    If present with the right URL → no-op.
    If present with a different URL → return error (don't silently re-point;
    that's an operator decision).
    """
    rc, out, err = _git(["remote", "get-url", remote], cwd)
    if rc != 0:
        # Doesn't exist — add it
        rc2, _, err2 = _git(["remote", "add", remote, expected_url], cwd)
        if rc2 != 0:
            return False, f"could not add remote '{remote}': {err2.strip() or err.strip()}"
        return True, f"added remote '{remote}' → {expected_url}"
    existing = out.strip()
    if existing != expected_url:
        return False, (
            f"remote '{remote}' exists but points at {existing!r}, expected {expected_url!r} "
            f"— refusing to silently re-point; update or remove it first"
        )
    return True, f"remote '{remote}' already configured"


# ─── Pre-deploy state inspection ──────────────────────────────────


@dataclass(frozen=True)
class CurrentDeployState:
    """What's currently sitting on a hand's Dokku app (best-effort lookup)."""

    deployed_sha: str | None  # SHA of `main` on the remote, if discoverable
    local_head_sha: str | None  # SHA of the source branch HEAD locally
    commits_ahead: int | None  # how many commits local is ahead by, if computable
    error: str | None = None  # populated when introspection failed


def inspect_current_deploy(
    cwd: Path, remote: str, source_branch: str,
) -> CurrentDeployState:
    """Look up what's currently deployed on the remote vs what we'd push.

    Operator-facing — surfaces "you are about to overwrite X" before the
    push so we don't silently blow away yesterday's manual testing on the
    same env. Best-effort: a failure to query just means we proceed
    without the safety net, never blocks the deploy.
    """
    # 1. SHA on the remote's main
    rc, out, err = _git(["ls-remote", remote, "main"], cwd, timeout=15.0)
    deployed_sha: str | None = None
    if rc == 0 and out.strip():
        # Format: "<sha>\trefs/heads/main"
        deployed_sha = out.split()[0]

    # 2. Local HEAD of source branch
    rc2, out2, _ = _git(["rev-parse", source_branch], cwd, timeout=10.0)
    local_head_sha = out2.strip() if rc2 == 0 and out2.strip() else None

    # 3. Commits between (only computable if BOTH SHAs are known AND the
    # remote SHA is reachable from local history — usually true after a
    # fetch, but not guaranteed)
    commits_ahead: int | None = None
    if deployed_sha and local_head_sha and deployed_sha != local_head_sha:
        rc3, out3, _ = _git(
            ["rev-list", "--count", f"{deployed_sha}..{local_head_sha}"],
            cwd, timeout=10.0,
        )
        if rc3 == 0 and out3.strip().isdigit():
            commits_ahead = int(out3.strip())

    error: str | None = None
    if deployed_sha is None:
        # Soft warning, not a hard error — first deploy to a fresh app
        # legitimately won't have a `main` ref yet.
        error = f"could not read main from {remote}: {err.strip() or 'no output'}"

    return CurrentDeployState(
        deployed_sha=deployed_sha,
        local_head_sha=local_head_sha,
        commits_ahead=commits_ahead,
        error=error,
    )


# ─── Deploy + health check ─────────────────────────────────────────


def deploy_run(
    run_id: int,
    *,
    source_branch: Optional[str] = None,
    force: bool = False,
    deploy_timeout_seconds: Optional[float] = None,
    health_timeout_seconds: float = 180.0,
    health_poll_interval_seconds: float = 5.0,
) -> DeployResult:
    """Deploy a run's branch to its agent's Dokku app, then health-check.

    Steps:
      1. Load Run + resolve DeployConfig
      2. Ensure the git remote exists with the right URL
      3. git push <remote> <source>:main  (Dokku always deploys main)
      4. Poll <url><health_path> via HEAD until 2xx/3xx OR timeout
      5. On success: write deploy_url + deployed_at to Run row

    `source_branch` defaults to Run.branch_name. `force=True` adds --force
    to the push (fine on dev envs per the cheatsheet).
    """
    started = time.time()

    with db_session() as db:
        run = db.query(Run).filter_by(id=run_id).one_or_none()
        if not run:
            return DeployResult(ok=False, reason=f"run #{run_id} not found",
                                 elapsed_seconds=time.time() - started)
        agent = run.agent
        cwd = Path(run.cwd) if run.cwd else None
        branch = source_branch or run.branch_name

    if not cwd:
        return DeployResult(ok=False, reason=f"run #{run_id} has no cwd",
                             elapsed_seconds=time.time() - started)
    if not branch:
        return DeployResult(ok=False, reason=f"run #{run_id} has no branch_name (must push first?)",
                             elapsed_seconds=time.time() - started)

    try:
        cfg = load_deploy_config(agent)
    except DeployConfigError as e:
        return DeployResult(ok=False, reason=str(e),
                             elapsed_seconds=time.time() - started)

    # Ensure remote
    expected_remote_url = f"{cfg.host}:{cfg.app}"
    ok, msg = ensure_dokku_remote(cwd, cfg.remote, expected_remote_url)
    if not ok:
        return DeployResult(ok=False, reason=msg,
                             elapsed_seconds=time.time() - started)

    # Push (Dokku always deploys main — `<source>:main`)
    push_args = ["push", cfg.remote, f"{branch}:main"]
    if force:
        push_args.append("--force")
    timeout = deploy_timeout_seconds or cfg.deploy_timeout_seconds
    rc, out, err = _git(push_args, cwd, timeout=timeout)
    push_output = (out + ("\n" + err if err else "")).strip()
    if rc != 0:
        return DeployResult(
            ok=False,
            url=cfg.url,
            push_output=push_output,
            reason=f"git push failed (exit {rc})",
            elapsed_seconds=time.time() - started,
        )

    # Health-check
    health = wait_for_health(
        cfg.url, cfg.health_path,
        timeout_seconds=health_timeout_seconds,
        interval_seconds=health_poll_interval_seconds,
    )

    if health.ok:
        now = datetime.now(timezone.utc)
        with db_session() as db:
            db.query(Run).filter_by(id=run_id).update({
                "deploy_url": cfg.url,
                "deployed_at": now,
            })

    return DeployResult(
        ok=health.ok,
        url=cfg.url,
        push_output=push_output,
        health=health,
        elapsed_seconds=time.time() - started,
        reason=None if health.ok else "health check failed after push succeeded",
    )


def wait_for_health(
    base_url: str,
    health_path: str = "/",
    *,
    timeout_seconds: float = 180.0,
    interval_seconds: float = 5.0,
    head_timeout_seconds: float = 10.0,
) -> HealthResult:
    """Poll base_url+health_path via HEAD until it responds 2xx/3xx OR timeout.

    Why HEAD: lighter weight than GET, and the citemed cheatsheet verifies
    deploys with `curl -I` — same idea.
    Why 2xx/3xx: a redirect to /login is a perfectly valid signal that the
    app is up. Anything else (4xx/5xx, connection refused, timeout) is not.
    """
    started = time.time()
    deadline = started + timeout_seconds
    url = base_url.rstrip("/") + (health_path if health_path.startswith("/") else "/" + health_path)
    last_error: str | None = None
    last_status: int | None = None

    while time.time() < deadline:
        try:
            with httpx.Client(
                timeout=head_timeout_seconds,
                follow_redirects=False,
                verify=True,
            ) as client:
                r = client.head(url)
            last_status = r.status_code
            if 200 <= r.status_code < 400:
                return HealthResult(
                    ok=True,
                    status_code=r.status_code,
                    elapsed_seconds=time.time() - started,
                )
            last_error = f"HEAD {url} → {r.status_code}"
        except httpx.RequestError as e:
            last_error = f"HEAD {url} error: {e}"

        time.sleep(interval_seconds)

    return HealthResult(
        ok=False,
        status_code=last_status,
        error=last_error or f"health check timed out after {timeout_seconds}s",
        elapsed_seconds=time.time() - started,
    )
