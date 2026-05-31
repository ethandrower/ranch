"""H8 — self-judge: run the acceptance checks emitted by `ranch propose`.

The judge is invoked by the agent via the `run_acceptance` MCP tool during
execute runs. Each check is independently runnable; results come back as a
structured list the agent can read, react to, and iterate on.

v1 supports three check kinds:
  - unit_test: shell command + pass_pattern (substring or regex in stdout)
  - script:    same shape as unit_test; semantic naming for the operator
  - http:      httpx GET + status code / body-substring assertions

Browser + figma_diff are reserved for v2 (need playwright + screenshot-diff
infra) — they're already in the AcceptanceCheck enum's `kind`, but the
runner rejects them with a clear error so misuse is loud, not silent.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import httpx

from .runner.messages import AcceptanceCheck


@dataclass
class AcceptanceResult:
    """One acceptance check's outcome — what the agent reads to decide next step."""

    name: str
    kind: str
    passed: bool
    duration_ms: int
    output: str = ""  # for unit_test/script: stdout+stderr tail; for http: status+body sample
    error: str = ""  # populated when the check couldn't run (config error, timeout, etc.)

    def summary_line(self) -> str:
        mark = "✓" if self.passed else "✗"
        return f"  {mark} [{self.kind}] {self.name}  ({self.duration_ms}ms)"


@dataclass
class JudgeRun:
    """Aggregate result for one `run_acceptance` invocation."""

    results: list[AcceptanceResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def num_failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def to_dict(self) -> dict:
        return {
            "all_passed": self.all_passed,
            "num_failed": self.num_failed,
            "results": [
                {
                    "name": r.name, "kind": r.kind, "passed": r.passed,
                    "duration_ms": r.duration_ms,
                    "output": r.output[:2000] if r.output else "",
                    "error": r.error,
                }
                for r in self.results
            ],
        }


# ─── Per-kind runners ──────────────────────────────────────────────


def _trim_for_report(text: str, limit: int = 3000) -> str:
    """Keep the tail when output is huge — failures usually surface there."""
    if len(text) <= limit:
        return text
    return f"...[truncated {len(text) - limit} chars]...\n{text[-limit:]}"


def _matches(pattern: str, text: str) -> bool:
    r"""Match either as a regex if it compiles, else as a substring.

    Agents naturally write things like 'passed' (substring) and '\d+ passed'
    (regex). Tolerate both without forcing them to escape.
    """
    if not pattern:
        return True
    try:
        return re.search(pattern, text, re.MULTILINE) is not None
    except re.error:
        return pattern in text


def _run_subprocess_check(check: AcceptanceCheck, cwd: Path) -> AcceptanceResult:
    if not check.cmd:
        return AcceptanceResult(
            name=check.name, kind=check.kind, passed=False, duration_ms=0,
            error=f"{check.kind} check missing `cmd`",
        )
    if not check.pass_pattern:
        return AcceptanceResult(
            name=check.name, kind=check.kind, passed=False, duration_ms=0,
            error=f"{check.kind} check missing `pass_pattern`",
        )

    start = time.time()
    try:
        proc = subprocess.run(
            check.cmd, cwd=str(cwd), shell=True,
            capture_output=True, text=True, timeout=check.timeout_seconds,
        )
        elapsed_ms = int((time.time() - start) * 1000)
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        passed = (proc.returncode == 0) and _matches(check.pass_pattern, combined)
        return AcceptanceResult(
            name=check.name, kind=check.kind, passed=passed,
            duration_ms=elapsed_ms, output=_trim_for_report(combined),
        )
    except subprocess.TimeoutExpired as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return AcceptanceResult(
            name=check.name, kind=check.kind, passed=False,
            duration_ms=elapsed_ms,
            error=f"timed out after {check.timeout_seconds}s",
            output=_trim_for_report((e.stdout or "") + (e.stderr or "")),
        )
    except (OSError, ValueError) as e:
        return AcceptanceResult(
            name=check.name, kind=check.kind, passed=False, duration_ms=0,
            error=f"subprocess failed: {e}",
        )


def _run_http_check(check: AcceptanceCheck) -> AcceptanceResult:
    if not check.url:
        return AcceptanceResult(
            name=check.name, kind="http", passed=False, duration_ms=0,
            error="http check missing `url`",
        )
    expected_status = check.expected_status if check.expected_status is not None else 200

    start = time.time()
    try:
        with httpx.Client(timeout=check.timeout_seconds) as client:
            resp = client.get(check.url)
        elapsed_ms = int((time.time() - start) * 1000)
        body = resp.text
        status_ok = (resp.status_code == expected_status)
        body_ok = (
            check.expected_body_contains is None
            or check.expected_body_contains in body
        )
        passed = status_ok and body_ok
        report = f"HTTP {resp.status_code}\nbody[:500]: {body[:500]}"
        if not status_ok:
            report += f"\n!! expected status {expected_status}"
        if not body_ok:
            report += f"\n!! body did not contain {check.expected_body_contains!r}"
        return AcceptanceResult(
            name=check.name, kind="http", passed=passed,
            duration_ms=elapsed_ms, output=report,
        )
    except httpx.RequestError as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return AcceptanceResult(
            name=check.name, kind="http", passed=False,
            duration_ms=elapsed_ms, error=f"request failed: {e}",
        )


# ─── Public entry point ────────────────────────────────────────────


def run_acceptance(checks: Iterable[AcceptanceCheck], cwd: Path) -> JudgeRun:
    """Run every check in order. Always returns a complete JudgeRun, even if
    individual checks crash — the agent reads `error` per result to triage."""
    run = JudgeRun()
    for c in checks:
        if c.kind in ("unit_test", "script"):
            run.results.append(_run_subprocess_check(c, cwd))
        elif c.kind == "http":
            run.results.append(_run_http_check(c))
        else:  # pragma: no cover — Pydantic guards the enum, but be loud if it slips
            run.results.append(AcceptanceResult(
                name=c.name, kind=c.kind, passed=False, duration_ms=0,
                error=f"check kind {c.kind!r} not yet supported (browser/figma_diff are v2)",
            ))
    return run
