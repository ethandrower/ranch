"""Tests for the H8 self-judge runner.

Subprocess checks are exercised with real `bash -c` invocations — they're
hermetic enough and prove the actual subprocess path. HTTP checks use
httpx.MockTransport so we don't hit the network.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ranch.judge import (
    AcceptanceResult,
    JudgeRun,
    _matches,
    _trim_for_report,
    run_acceptance,
)
from ranch.runner.messages import AcceptanceCheck


# ─── _matches ─────────────────────────────────────────────────────


def test_matches_substring():
    assert _matches("passed", "1 test passed") is True
    assert _matches("failed", "1 test passed") is False


def test_matches_regex():
    assert _matches(r"\d+ passed", "5 passed") is True
    assert _matches(r"\d+ passed", "all good") is False


def test_matches_invalid_regex_falls_back_to_substring():
    """A bare '*' isn't a valid regex — should be treated as substring."""
    assert _matches("*", "before * after") is True


def test_matches_empty_pattern_is_pass():
    """No pattern = no constraint."""
    assert _matches("", "anything") is True


# ─── _trim_for_report ─────────────────────────────────────────────


def test_trim_keeps_tail():
    body = "a" * 5000
    trimmed = _trim_for_report(body, limit=100)
    assert "[truncated 4900 chars]" in trimmed
    assert trimmed.endswith("a" * 100)


def test_trim_passes_short_unchanged():
    assert _trim_for_report("short") == "short"


# ─── subprocess checks ────────────────────────────────────────────


def test_unit_test_passes_when_cmd_zero_and_pattern_matches(tmp_path):
    check = AcceptanceCheck(
        kind="unit_test", name="echo-pass",
        cmd="echo '3 passed'", pass_pattern=r"\d+ passed",
    )
    run = run_acceptance([check], tmp_path)
    assert run.all_passed
    assert run.num_failed == 0
    assert run.results[0].duration_ms >= 0


def test_unit_test_fails_when_pattern_missing(tmp_path):
    check = AcceptanceCheck(
        kind="unit_test", name="echo-fail",
        cmd="echo 'nothing here'", pass_pattern="passed",
    )
    run = run_acceptance([check], tmp_path)
    assert not run.all_passed
    assert run.num_failed == 1
    assert "nothing here" in run.results[0].output


def test_unit_test_fails_on_nonzero_exit(tmp_path):
    """Pattern present but exit code nonzero → still a fail."""
    check = AcceptanceCheck(
        kind="unit_test", name="false-with-pattern",
        cmd="echo passed && exit 1", pass_pattern="passed",
    )
    run = run_acceptance([check], tmp_path)
    assert run.results[0].passed is False


def test_unit_test_missing_cmd_reports_error(tmp_path):
    check = AcceptanceCheck(kind="unit_test", name="bad", pass_pattern="x")
    run = run_acceptance([check], tmp_path)
    assert run.results[0].passed is False
    assert "missing `cmd`" in run.results[0].error


def test_unit_test_missing_pattern_reports_error(tmp_path):
    check = AcceptanceCheck(kind="unit_test", name="bad", cmd="echo x")
    run = run_acceptance([check], tmp_path)
    assert run.results[0].passed is False
    assert "missing `pass_pattern`" in run.results[0].error


def test_unit_test_timeout_reports_clean_failure(tmp_path):
    check = AcceptanceCheck(
        kind="unit_test", name="hang",
        cmd="sleep 5", pass_pattern="never",
        timeout_seconds=0.2,
    )
    run = run_acceptance([check], tmp_path)
    assert run.results[0].passed is False
    assert "timed out" in run.results[0].error


def test_script_kind_uses_same_runner_as_unit_test(tmp_path):
    """Semantic alias for the operator — agent picks `script` for integration
    checks vs `unit_test` for actual test invocations."""
    check = AcceptanceCheck(
        kind="script", name="smoke",
        cmd="echo READY", pass_pattern="READY",
    )
    run = run_acceptance([check], tmp_path)
    assert run.all_passed


def test_runs_in_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("present")
    check = AcceptanceCheck(
        kind="script", name="cwd-check",
        cmd="ls marker.txt", pass_pattern="marker.txt",
    )
    run = run_acceptance([check], tmp_path)
    assert run.all_passed


# ─── http checks (mock transport) ─────────────────────────────────


def _client_with_responder(responder, monkeypatch):
    """Patch httpx.Client to use a MockTransport that calls `responder`."""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(responder)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("ranch.judge.httpx.Client", factory)


def test_http_pass_on_status_and_body(tmp_path, monkeypatch):
    def responder(request):
        assert request.url.path == "/healthz"
        return httpx.Response(200, text='{"status":"ok"}')
    _client_with_responder(responder, monkeypatch)

    check = AcceptanceCheck(
        kind="http", name="healthz",
        url="http://example/healthz",
        expected_status=200, expected_body_contains='"status":"ok"',
    )
    run = run_acceptance([check], tmp_path)
    assert run.all_passed


def test_http_fails_on_unexpected_status(tmp_path, monkeypatch):
    def responder(_req):
        return httpx.Response(500, text="boom")
    _client_with_responder(responder, monkeypatch)

    check = AcceptanceCheck(
        kind="http", name="healthz",
        url="http://example/healthz", expected_status=200,
    )
    run = run_acceptance([check], tmp_path)
    assert run.results[0].passed is False
    assert "expected status 200" in run.results[0].output


def test_http_fails_on_body_not_containing_substring(tmp_path, monkeypatch):
    def responder(_req):
        return httpx.Response(200, text="hello world")
    _client_with_responder(responder, monkeypatch)

    check = AcceptanceCheck(
        kind="http", name="x",
        url="http://example/x", expected_body_contains="goodbye",
    )
    run = run_acceptance([check], tmp_path)
    assert run.results[0].passed is False
    assert "goodbye" in run.results[0].output


def test_http_default_expected_status_is_200(tmp_path, monkeypatch):
    """If expected_status is omitted, 200 is the assumed contract."""
    def responder(_req):
        return httpx.Response(200, text="ok")
    _client_with_responder(responder, monkeypatch)

    check = AcceptanceCheck(kind="http", name="x", url="http://example/x")
    run = run_acceptance([check], tmp_path)
    assert run.all_passed


def test_http_missing_url_reports_error(tmp_path):
    check = AcceptanceCheck(kind="http", name="no-url")
    run = run_acceptance([check], tmp_path)
    assert run.results[0].passed is False
    assert "missing `url`" in run.results[0].error


# ─── JudgeRun aggregation ─────────────────────────────────────────


def test_judge_run_all_passed_requires_at_least_one_check():
    """An empty run shouldn't report all_passed — caller should treat empty
    as a misconfiguration, not as success."""
    assert JudgeRun().all_passed is False


def test_judge_run_to_dict_truncates_long_output():
    r = AcceptanceResult(name="x", kind="script", passed=True,
                          duration_ms=1, output="z" * 5000)
    d = JudgeRun(results=[r]).to_dict()
    assert len(d["results"][0]["output"]) <= 2000


def test_judge_run_mixed_results_reports_num_failed(tmp_path):
    checks = [
        AcceptanceCheck(kind="script", name="a", cmd="echo ok", pass_pattern="ok"),
        AcceptanceCheck(kind="script", name="b", cmd="echo nope", pass_pattern="missing"),
        AcceptanceCheck(kind="script", name="c", cmd="echo also-ok", pass_pattern="also"),
    ]
    run = run_acceptance(checks, tmp_path)
    assert run.all_passed is False
    assert run.num_failed == 1
    assert [r.passed for r in run.results] == [True, False, True]
