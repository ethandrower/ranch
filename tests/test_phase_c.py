"""Phase C tests — config respects RANCH_HOME / RANCH_DATABASE_URL for
isolation. Without these env vars, dev runs would clobber the live
~/.ranch state.
"""
from __future__ import annotations

import importlib

import pytest


def test_ranch_home_respects_env_var(tmp_path, monkeypatch):
    """When RANCH_HOME is set, all derived paths should rebase under it."""
    monkeypatch.setenv("RANCH_HOME", str(tmp_path))
    # Re-import the module so the module-level constants pick up the env.
    import ranch.config as _config
    importlib.reload(_config)
    try:
        assert _config.RANCH_HOME == tmp_path
        assert _config.DB_PATH == tmp_path / "ranch.db"
        assert _config.CONFIG_FILE == tmp_path / "config.toml"
        assert _config.LOG_DIR == tmp_path / "logs"
    finally:
        # Restore default so subsequent tests aren't polluted.
        monkeypatch.delenv("RANCH_HOME", raising=False)
        importlib.reload(_config)


def test_database_url_overrides_path(tmp_path, monkeypatch):
    """RANCH_DATABASE_URL fully overrides RANCH_HOME's DB path."""
    monkeypatch.setenv("RANCH_HOME", str(tmp_path))
    monkeypatch.setenv("RANCH_DATABASE_URL", "sqlite:///somewhere/else.db")
    import ranch.config as _config
    importlib.reload(_config)
    try:
        assert _config.DATABASE_URL == "sqlite:///somewhere/else.db"
    finally:
        monkeypatch.delenv("RANCH_HOME", raising=False)
        monkeypatch.delenv("RANCH_DATABASE_URL", raising=False)
        importlib.reload(_config)


def test_default_ranch_home_is_dot_ranch(monkeypatch, tmp_path):
    """Without RANCH_HOME set, falls back to ~/.ranch. We don't actually
    poke ~ here — we monkeypatch Path.home to keep the test sandboxed."""
    monkeypatch.delenv("RANCH_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    import ranch.config as _config
    importlib.reload(_config)
    try:
        assert _config.RANCH_HOME == tmp_path / ".ranch"
    finally:
        importlib.reload(_config)
