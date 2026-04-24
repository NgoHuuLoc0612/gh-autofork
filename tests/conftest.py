"""Shared pytest fixtures for gh-autofork tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gh_autofork.config import AppConfig, BatchConfig, FilterConfig, GithubConfig, SyncConfig
from gh_autofork.database import Database
from gh_autofork.github_client import GitHubClient, RepoInfo


# ---------------------------------------------------------------------------
# Config / DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.github = GithubConfig(token="ghp_test_token_fixture")
    cfg.db_path = str(tmp_path / "test.db")
    cfg.logging.file = str(tmp_path / "test.log")
    cfg.logging.stderr = False
    cfg.dry_run = False
    cfg.batch.concurrency = 2
    cfg.batch.fork_delay  = 0.0
    return cfg


@pytest.fixture
def db(app_config: AppConfig) -> Database:
    return Database(app_config.db_path)


# ---------------------------------------------------------------------------
# Mock GitHub API responses
# ---------------------------------------------------------------------------

def make_repo_raw(
    full_name: str = "owner/repo",
    stars: int = 100,
    language: str = "Python",
    topics: list | None = None,
    is_fork: bool = False,
    is_archived: bool = False,
    is_private: bool = False,
    pushed_at: str = "2024-06-01T00:00:00Z",
    parent_full_name: str = "",
) -> dict:
    owner, name = full_name.split("/", 1)
    # Use hash of full_name to ensure unique github_id per repo
    github_id = abs(hash(full_name)) % (10 ** 9)
    raw = {
        "id":                 github_id,
        "full_name":          full_name,
        "owner":              {"login": owner},
        "name":               name,
        "description":        f"Test repo {name}",
        "stargazers_count":   stars,
        "forks_count":        10,
        "language":           language,
        "topics":             topics or [],
        "private":            is_private,
        "archived":           is_archived,
        "fork":               is_fork,
        "size":               1024,
        "license":            {"spdx_id": "MIT"},
        "homepage":           "",
        "default_branch":     "main",
        "open_issues_count":  5,
        "clone_url":          f"https://github.com/{full_name}.git",
        "ssh_url":            f"git@github.com:{full_name}.git",
        "created_at":         "2020-01-01T00:00:00Z",
        "updated_at":         "2024-01-01T00:00:00Z",
        "pushed_at":          pushed_at,
    }
    if parent_full_name:
        raw["parent"] = {"full_name": parent_full_name}
    return raw


@pytest.fixture
def mock_repo_info() -> RepoInfo:
    return RepoInfo(make_repo_raw())


@pytest.fixture
def mock_client(app_config: AppConfig) -> MagicMock:
    """Return a MagicMock GitHubClient with sensible defaults."""
    client = MagicMock(spec=GitHubClient)
    client.auth_login = "testuser"

    # get_repo returns a RepoInfo by default - use side_effect callable
    def _get_repo(full_name: str):
        return RepoInfo(make_repo_raw(full_name=full_name))
    client.get_repo.side_effect = _get_repo

    # fork_repo returns a fork RepoInfo
    def _fork_repo(full_name: str, organization: str = "", **kwargs):
        owner = organization or "testuser"
        name  = full_name.split("/")[-1]
        fork_name = f"{owner}/{name}"
        raw = make_repo_raw(full_name=fork_name, is_fork=True, parent_full_name=full_name)
        return RepoInfo(raw)
    client.fork_repo.side_effect = _fork_repo

    client.get_rate_limit.return_value = MagicMock(
        remaining=4999, limit=5000, used=1,
        seconds_until_reset=3600,
        reset_dt=MagicMock(strftime=lambda _: "2025-01-01 00:00 UTC"),
    )
    return client
