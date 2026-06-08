"""H9 Phase 1 — labs deploy primitives.

Deploys an agent's run to its Dokku app (per the citemed
`infra/dokku/CHEATSHEET.md` conventions), polls the public URL until
the app responds, and persists the URL + deployed_at timestamp on the
Run row.

Conventions inherited verbatim from the cheatsheet:
  - Dokku always deploys whatever is pushed to `main`, regardless of
    source branch (`git push <remote> <source>:main`).
  - The remote alias per hand is `dokku-<agent>`.
  - The app on the host is `dev-<agent>`.
  - Public URL is `<agent>.staging.citemed.com` (dev-ethan's
    `labs.staging.citemed.com` is special; not relevant to ranch hands).
  - Health check: `curl -I` on root — 2xx OR 3xx counts as alive.

Phase 1 (this module) ships the primitive. Phase 2 wires it into the
hand's main loop so it auto-fires after pre_push approval. Phase 3
extends the H10 PR draft body with the resulting `labs_url`.
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
class LabsConfig:
    """Resolved labs config for one agent — fleet defaults + per-agent overrides."""

    agent: str
    host: str  # e.g. "dokku@178.105.80.165"
    app: str   # e.g. "dev-max"
    remote: str  # e.g. "dokku-max"
    url: str  # e.g. "https://max.staging.citemed.com"
    health_path: str  # e.g. "/"
    deploy_timeout_seconds: int


class LabsConfigError(RuntimeError):
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


def load_labs_config(agent: str) -> LabsConfig:
    """Resolve LabsConfig for one agent. Raises LabsConfigError on misconfig."""
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
        return LabsConfig(**merged)

    return _from_templates(agent, fleet)


def _from_templates(agent: str, fleet: dict) -> LabsConfig:
    return LabsConfig(
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
    """End-to-end deploy result returned by deploy_run_to_labs."""

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


# ─── Deploy + health check ─────────────────────────────────────────


def deploy_run_to_labs(
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
      1. Load Run + resolve LabsConfig
      2. Ensure the git remote exists with the right URL
      3. git push <remote> <source>:main  (Dokku always deploys main)
      4. Poll <url><health_path> via HEAD until 2xx/3xx OR timeout
      5. On success: write labs_url + labs_deployed_at to Run row

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
        cfg = load_labs_config(agent)
    except LabsConfigError as e:
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
                "labs_url": cfg.url,
                "labs_deployed_at": now,
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
