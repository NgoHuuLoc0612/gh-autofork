"""Tests for the SyncEngine."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gh_autofork.github_client import RepoInfo
from gh_autofork.sync import SyncEngine
from tests.conftest import make_repo_raw


@pytest.fixture
def engine(mock_client, db, app_config):
    return SyncEngine(mock_client, db, app_config)


class TestSyncEngine:
    def test_run_empty_returns_empty(self, engine, db):
        results = engine.run()
        assert results == []

    def test_run_syncs_successful_forks(self, engine, db, mock_client):
        # Set up: one successful fork
        fid = db.enqueue_fork("upstream/repo")
        db.mark_fork_success(fid, "me/repo")

        mock_client.compare_commits.return_value = {
            "status": "behind",
            "behind_by": 3,
            "ahead_by":  0,
            "base_commit": "abc",
            "head_commit": "def",
        }
        mock_client.sync_fork.return_value = (True, "synced (merge)")

        results = engine.run(force=True)

        assert len(results) == 1
        assert results[0].status == "synced"
        assert results[0].commits_behind == 3

    def test_run_skips_up_to_date(self, engine, db, mock_client):
        fid = db.enqueue_fork("upstream/repo")
        db.mark_fork_success(fid, "me/repo")

        mock_client.compare_commits.return_value = {
            "status": "identical",
            "behind_by": 0,
            "ahead_by":  0,
            "base_commit": "abc",
            "head_commit": "abc",
        }

        results = engine.run(force=True)
        assert results[0].status == "up_to_date"
        mock_client.sync_fork.assert_not_called()

    def test_run_below_min_commits_behind(self, engine, db, mock_client, app_config):
        app_config.sync.min_commits_behind = 5
        fid = db.enqueue_fork("upstream/repo")
        db.mark_fork_success(fid, "me/repo")

        mock_client.compare_commits.return_value = {
            "status": "behind",
            "behind_by": 2,       # below threshold
            "ahead_by":  0,
            "base_commit": "abc",
            "head_commit": "def",
        }

        results = engine.run(force=True)
        assert results[0].status == "up_to_date"
        mock_client.sync_fork.assert_not_called()

    def test_run_records_sync_history(self, engine, db, mock_client):
        fid = db.enqueue_fork("upstream/repo")
        db.mark_fork_success(fid, "me/repo")

        mock_client.compare_commits.return_value = {
            "status": "behind", "behind_by": 2, "ahead_by": 0,
            "base_commit": "abc", "head_commit": "def",
        }
        mock_client.sync_fork.return_value = (True, "synced")

        engine.run(force=True)

        last = db.last_sync("upstream/repo")
        assert last is not None
        assert last["status"] == "synced"

    def test_run_handles_sync_failure(self, engine, db, mock_client):
        fid = db.enqueue_fork("upstream/repo")
        db.mark_fork_success(fid, "me/repo")

        mock_client.compare_commits.return_value = {
            "status": "behind", "behind_by": 5, "ahead_by": 0,
            "base_commit": "a", "head_commit": "b",
        }
        mock_client.sync_fork.side_effect = Exception("Network error")

        results = engine.run(force=True)
        assert results[0].status == "failed"
        assert "Network error" in results[0].error

    def test_disabled_sync_returns_early(self, mock_client, db, app_config):
        app_config.sync.enabled = False
        engine = SyncEngine(mock_client, db, app_config)
        results = engine.run()
        assert results == []

    def test_force_overrides_disabled(self, mock_client, db, app_config):
        app_config.sync.enabled = False
        engine = SyncEngine(mock_client, db, app_config)
        fid = db.enqueue_fork("upstream/repo")
        db.mark_fork_success(fid, "me/repo")
        mock_client.compare_commits.return_value = {
            "status": "identical", "behind_by": 0, "ahead_by": 0,
            "base_commit": "a", "head_commit": "a",
        }
        results = engine.run(force=True)
        assert len(results) == 1

    def test_sync_single_not_a_fork(self, engine, mock_client):
        raw = make_repo_raw(full_name="me/project", is_fork=False)
        mock_client.get_repo.side_effect = None
        mock_client.get_repo.return_value = RepoInfo(raw)
        result = engine.sync_single("me/project")
        assert result.status == "skipped"

    def test_sync_single_not_found(self, engine, mock_client):
        mock_client.get_repo.side_effect = None
        mock_client.get_repo.return_value = None
        result = engine.sync_single("nobody/nowhere")
        assert result.status == "failed"

    def test_sync_single_success(self, engine, db, mock_client):
        fork_raw = make_repo_raw(
            full_name="me/linux",
            is_fork=True,
            parent_full_name="torvalds/linux",
        )
        mock_client.get_repo.side_effect = None
        mock_client.get_repo.return_value = RepoInfo(fork_raw)
        mock_client.compare_commits.return_value = {
            "status": "behind", "behind_by": 10, "ahead_by": 0,
            "base_commit": "abc", "head_commit": "def",
        }
        mock_client.sync_fork.return_value = (True, "synced")

        result = engine.sync_single("me/linux", force=True)
        assert result.status == "synced"
        assert result.commits_behind == 10
