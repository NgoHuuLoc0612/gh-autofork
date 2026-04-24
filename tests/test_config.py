"""Tests for config loading, env overrides, and validation."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gh_autofork.config import (
    AppConfig,
    FilterConfig,
    GithubConfig,
    _env_overrides,
    load_config,
    save_config,
    _deep_merge,
)
from gh_autofork.exceptions import ConfigError


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------

class TestDeepMerge:
    def test_simple(self):
        base = {"a": 1, "b": 2}
        override = {"b": 99, "c": 3}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 99, "c": 3}

    def test_nested(self):
        base     = {"github": {"token": "old", "per_page": 100}}
        override = {"github": {"token": "new"}}
        result = _deep_merge(base, override)
        assert result["github"]["token"]    == "new"
        assert result["github"]["per_page"] == 100

    def test_non_destructive(self):
        base     = {"x": {"y": 1}}
        override = {"x": {"z": 2}}
        _deep_merge(base, override)
        assert "z" not in base["x"]  # original unchanged


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------

class TestEnvOverrides:
    def test_token_override(self, monkeypatch):
        monkeypatch.setenv("GH_AUTOFORK_GITHUB__TOKEN", "ghp_env_token")
        overrides = _env_overrides()
        assert overrides["github"]["token"] == "ghp_env_token"

    def test_integer_coercion(self, monkeypatch):
        monkeypatch.setenv("GH_AUTOFORK_BATCH__CONCURRENCY", "8")
        overrides = _env_overrides()
        assert overrides["batch"]["concurrency"] == 8

    def test_bool_coercion_true(self, monkeypatch):
        monkeypatch.setenv("GH_AUTOFORK_DRY_RUN", "true")
        overrides = _env_overrides()
        assert overrides["dry_run"] is True

    def test_bool_coercion_false(self, monkeypatch):
        monkeypatch.setenv("GH_AUTOFORK_DRY_RUN", "false")
        overrides = _env_overrides()
        assert overrides["dry_run"] is False

    def test_float_coercion(self, monkeypatch):
        monkeypatch.setenv("GH_AUTOFORK_BATCH__FORK_DELAY", "1.5")
        overrides = _env_overrides()
        assert overrides["batch"]["fork_delay"] == 1.5

    def test_non_prefixed_vars_ignored(self, monkeypatch):
        monkeypatch.setenv("SOME_OTHER_VAR", "value")
        overrides = _env_overrides()
        assert "some_other_var" not in overrides

    def test_github_token_fallback(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("github:\n  token: ''\n")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")
        monkeypatch.delenv("GH_AUTOFORK_GITHUB__TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        cfg = load_config(cfg_file)
        assert cfg.github.token == "ghp_fallback"


# ---------------------------------------------------------------------------
# Config file loading
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_load_defaults_with_env_token(self, monkeypatch, tmp_path):
        non_existent = tmp_path / "missing.yaml"
        monkeypatch.setenv("GH_AUTOFORK_GITHUB__TOKEN", "ghp_test")
        cfg = load_config(non_existent)
        assert cfg.github.token == "ghp_test"
        assert cfg.batch.concurrency == 4  # default

    def test_load_yaml(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GH_AUTOFORK_GITHUB__TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
github:
  token: ghp_yaml_token
  per_page: 50
batch:
  concurrency: 8
  fork_delay: 2.0
dry_run: true
startup_repos:
  - alice/repo1
  - bob/repo2
""")
        cfg = load_config(cfg_file)
        assert cfg.github.token       == "ghp_yaml_token"
        assert cfg.github.per_page    == 50
        assert cfg.batch.concurrency  == 8
        assert cfg.batch.fork_delay   == 2.0
        assert cfg.dry_run            is True
        assert "alice/repo1"          in cfg.startup_repos

    def test_env_overrides_yaml(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("github:\n  token: ghp_yaml\n  per_page: 50\n")
        monkeypatch.setenv("GH_AUTOFORK_GITHUB__TOKEN", "ghp_env")
        cfg = load_config(cfg_file)
        assert cfg.github.token    == "ghp_env"
        assert cfg.github.per_page == 50   # from YAML, not overridden

    def test_invalid_yaml_raises(self, tmp_path):
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("github: [\ninvalid")
        with pytest.raises(ConfigError, match="Cannot parse"):
            load_config(cfg_file)

    def test_missing_token_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GH_AUTOFORK_GITHUB__TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("github:\n  token: ''\n")
        with pytest.raises(ConfigError, match="GitHub token"):
            load_config(cfg_file)

    def test_filter_languages_comma_string(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("""
github:
  token: ghp_test
filters:
  languages: "python, go, rust"
""")
        cfg = load_config(cfg_file)
        assert cfg.filters.languages == ["python", "go", "rust"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def _base(self) -> AppConfig:
        cfg = AppConfig()
        cfg.github = GithubConfig(token="ghp_valid")
        return cfg

    def test_invalid_concurrency(self):
        from gh_autofork.config import _validate
        cfg = self._base()
        cfg.batch.concurrency = 0
        with pytest.raises(ConfigError, match="concurrency"):
            _validate(cfg)

    def test_invalid_sync_strategy(self):
        from gh_autofork.config import _validate
        cfg = self._base()
        cfg.sync.strategy = "cherry-pick"
        with pytest.raises(ConfigError, match="sync.strategy"):
            _validate(cfg)

    def test_invalid_log_level(self):
        from gh_autofork.config import _validate
        cfg = self._base()
        cfg.logging.level = "VERBOSE"
        with pytest.raises(ConfigError, match="logging.level"):
            _validate(cfg)

    def test_invalid_description_regex(self):
        from gh_autofork.config import _validate
        cfg = self._base()
        cfg.filters.description_pattern = r"[invalid(regex"
        with pytest.raises(ConfigError, match="description_pattern"):
            _validate(cfg)

    def test_invalid_name_regex(self):
        from gh_autofork.config import _validate
        cfg = self._base()
        cfg.filters.name_pattern = r"("
        with pytest.raises(ConfigError, match="name_pattern"):
            _validate(cfg)


# ---------------------------------------------------------------------------
# save_config round-trip
# ---------------------------------------------------------------------------

class TestSaveConfig:
    def test_roundtrip(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg = AppConfig()
        cfg.github = GithubConfig(token="ghp_roundtrip", per_page=75)
        cfg.batch.concurrency = 6
        cfg.dry_run = True

        save_config(cfg, cfg_file)

        assert cfg_file.exists()
        loaded = yaml.safe_load(cfg_file.read_text())
        assert loaded["github"]["token"]   == "ghp_roundtrip"
        assert loaded["batch"]["concurrency"] == 6
        assert loaded["dry_run"] is True

    def test_creates_parent_dirs(self, tmp_path):
        cfg_file = tmp_path / "subdir" / "config.yaml"
        cfg = AppConfig()
        cfg.github = GithubConfig(token="ghp_x")
        save_config(cfg, cfg_file)
        assert cfg_file.exists()
