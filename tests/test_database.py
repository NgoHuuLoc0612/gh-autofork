"""Tests for the Database class."""

from __future__ import annotations

import json
import time

import pytest

from gh_autofork.database import Database
from tests.conftest import make_repo_raw
from gh_autofork.github_client import RepoInfo


# ---------------------------------------------------------------------------
# Schema / migration
# ---------------------------------------------------------------------------

def test_database_creates_tables(db: Database) -> None:
    tables = {
        row["name"]
        for row in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    expected = {"repos", "forks", "sync_history", "jobs", "rate_limit_log", "kv_cache", "watchlist"}
    assert expected <= tables


def test_schema_versions_populated(db: Database) -> None:
    rows = db.fetchall("SELECT version FROM schema_versions ORDER BY version")
    versions = [r["version"] for r in rows]
    assert versions == [1, 2, 3]


# ---------------------------------------------------------------------------
# Repo table
# ---------------------------------------------------------------------------

class TestRepoTable:
    def test_upsert_and_get(self, db: Database) -> None:
        info = RepoInfo(make_repo_raw(full_name="alice/awesome"))
        db.upsert_repo(info.to_db_dict())
        row = db.get_repo("alice/awesome")
        assert row is not None
        assert row["owner"] == "alice"
        assert row["stars"] == 100
        assert row["language"] == "Python"

    def test_upsert_updates_stars(self, db: Database) -> None:
        info = RepoInfo(make_repo_raw(full_name="alice/awesome", stars=50))
        db.upsert_repo(info.to_db_dict())

        updated = RepoInfo(make_repo_raw(full_name="alice/awesome", stars=999))
        db.upsert_repo(updated.to_db_dict())

        row = db.get_repo("alice/awesome")
        assert row["stars"] == 999

    def test_get_missing_returns_none(self, db: Database) -> None:
        assert db.get_repo("nobody/norepo") is None

    def test_topics_stored_as_json(self, db: Database) -> None:
        info = RepoInfo(make_repo_raw(full_name="x/y", topics=["ml", "python"]))
        db.upsert_repo(info.to_db_dict())
        row = db.get_repo("x/y")
        assert json.loads(row["topics"]) == ["ml", "python"]


# ---------------------------------------------------------------------------
# Fork queue
# ---------------------------------------------------------------------------

class TestForkQueue:
    def _insert_repo(self, db: Database, full_name: str) -> int:
        info = RepoInfo(make_repo_raw(full_name=full_name))
        return db.upsert_repo(info.to_db_dict())

    def test_enqueue_returns_id(self, db: Database) -> None:
        fid = db.enqueue_fork("alice/lib")
        assert isinstance(fid, int)
        assert fid > 0

    def test_enqueue_idempotent_for_success(self, db: Database) -> None:
        fid1 = db.enqueue_fork("alice/lib")
        db.mark_fork_success(fid1, "myuser/lib")
        # Second enqueue after success → None
        fid2 = db.enqueue_fork("alice/lib")
        assert fid2 is None

    def test_enqueue_resets_failed(self, db: Database) -> None:
        fid1 = db.enqueue_fork("alice/lib2")
        db.mark_fork_failed(fid1, "network error")
        # Re-enqueue should reset status
        fid2 = db.enqueue_fork("alice/lib2")
        assert fid2 == fid1
        row = db.fetchone("SELECT status FROM forks WHERE id=?", (fid1,))
        assert row["status"] == "pending"

    def test_get_pending_forks(self, db: Database) -> None:
        db.enqueue_fork("a/r1")
        db.enqueue_fork("a/r2")
        pending = db.get_pending_forks()
        assert len(pending) == 2
        assert all(r["status"] == "pending" for r in pending)

    def test_mark_running_increments_attempt(self, db: Database) -> None:
        fid = db.enqueue_fork("alice/repo")
        db.mark_fork_running(fid)
        row = db.fetchone("SELECT attempt_count, status FROM forks WHERE id=?", (fid,))
        assert row["attempt_count"] == 1
        assert row["status"] == "running"

    def test_mark_success(self, db: Database) -> None:
        fid = db.enqueue_fork("alice/repo")
        db.mark_fork_success(fid, "myuser/repo")
        row = db.fetchone("SELECT status, fork_full_name FROM forks WHERE id=?", (fid,))
        assert row["status"] == "success"
        assert row["fork_full_name"] == "myuser/repo"

    def test_mark_failed(self, db: Database) -> None:
        fid = db.enqueue_fork("alice/repo")
        db.mark_fork_failed(fid, "timeout")
        row = db.fetchone("SELECT status, error_message FROM forks WHERE id=?", (fid,))
        assert row["status"] == "failed"
        assert "timeout" in row["error_message"]

    def test_count_by_status(self, db: Database) -> None:
        f1 = db.enqueue_fork("a/r1")
        f2 = db.enqueue_fork("a/r2")
        db.mark_fork_success(f1, "me/r1")
        counts = db.count_forks_by_status()
        assert counts.get("pending", 0) == 1
        assert counts.get("success", 0) == 1

    def test_retryable_forks(self, db: Database) -> None:
        fid = db.enqueue_fork("alice/repo")
        db.mark_fork_running(fid)
        db.mark_fork_failed(fid, "err")
        retries = db.get_retryable_forks(max_attempts=3)
        assert len(retries) == 1

        # exhausted
        for _ in range(2):
            db.mark_fork_running(fid)
            db.mark_fork_failed(fid, "err")
        retries2 = db.get_retryable_forks(max_attempts=3)
        assert len(retries2) == 0

    def test_org_uniqueness(self, db: Database) -> None:
        fid1 = db.enqueue_fork("a/r", organization="org1")
        fid2 = db.enqueue_fork("a/r", organization="org2")
        assert fid1 != fid2   # different orgs → different rows


# ---------------------------------------------------------------------------
# Sync history
# ---------------------------------------------------------------------------

class TestSyncHistory:
    def test_record_and_last(self, db: Database) -> None:
        db.enqueue_fork("alice/linux")
        db.record_sync({
            "fork_id":        1,
            "repo_full_name": "alice/linux",
            "fork_full_name": "me/linux",
            "upstream_sha":   "abc123",
            "fork_sha":       "def456",
            "status":         "synced",
            "commits_behind": 5,
            "error_message":  None,
            "synced_at":      "2024-01-01T00:00:00",
        })
        last = db.last_sync("alice/linux")
        assert last is not None
        assert last["status"] == "synced"
        assert last["commits_behind"] == 5


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class TestJobs:
    def test_job_lifecycle(self, db: Database) -> None:
        jid = db.create_job("test_job", {"param": "value"})
        assert jid > 0

        db.start_job(jid, total=10)
        db.update_job_progress(jid, 5, 4, 1, 0)
        db.finish_job(jid, success=True)

        job = db.get_job(jid)
        assert job["status"]   == "success"
        assert job["progress"] == 5
        assert job["total"]    == 10


# ---------------------------------------------------------------------------
# KV cache
# ---------------------------------------------------------------------------

class TestKVCache:
    def test_set_get_delete(self, db: Database) -> None:
        db.cache_set("test:key", {"hello": "world"})
        val = db.cache_get("test:key")
        assert val == {"hello": "world"}
        db.cache_delete("test:key")
        assert db.cache_get("test:key") is None

    def test_ttl_expiry(self, db: Database) -> None:
        db.cache_set("expires:soon", "data", ttl_seconds=-1)  # already expired
        val = db.cache_get("expires:soon")
        assert val is None

    def test_no_expiry(self, db: Database) -> None:
        db.cache_set("permanent", 42)
        assert db.cache_get("permanent") == 42


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

class TestWatchlist:
    def test_upsert_and_list(self, db: Database) -> None:
        db.upsert_watch("user", "torvalds")
        db.upsert_watch("org",  "microsoft")
        watches = db.list_watches(enabled_only=True)
        targets = {w["target"] for w in watches}
        assert "torvalds" in targets
        assert "microsoft" in targets

    def test_idempotent(self, db: Database) -> None:
        db.upsert_watch("user", "alice")
        db.upsert_watch("user", "alice")
        watches = [w for w in db.list_watches() if w["target"] == "alice"]
        assert len(watches) == 1

    def test_delete(self, db: Database) -> None:
        db.upsert_watch("user", "bob")
        n = db.delete_watch("user", "bob")
        assert n == 1
        watches = [w for w in db.list_watches() if w["target"] == "bob"]
        assert watches == []


# ---------------------------------------------------------------------------
# Integrity / vacuum
# ---------------------------------------------------------------------------

def test_integrity_check(db: Database) -> None:
    assert db.integrity_check() is True


def test_vacuum(db: Database) -> None:
    db.vacuum()  # just confirm it doesn't raise
