"""Tests for the BatchForker and ForkWorker."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call
import pytest

from gh_autofork.config import AppConfig
from gh_autofork.database import Database
from gh_autofork.exceptions import ForkError, RepoNotFoundError
from gh_autofork.forker import BatchForker, ForkResult, ForkWorker
from gh_autofork.github_client import RepoInfo
from tests.conftest import make_repo_raw


# ---------------------------------------------------------------------------
# ForkWorker
# ---------------------------------------------------------------------------

class TestForkWorker:
    def test_successful_fork(self, mock_client, db, app_config):
        worker = ForkWorker(mock_client, db, app_config)
        fid = db.enqueue_fork("alice/repo")
        rec = db.get_pending_forks()[0]
        result = worker.execute(rec)

        assert result.success is True
        assert result.fork_id == fid
        assert "testuser/repo" in result.fork_full_name
        assert result.error == ""

    def test_dry_run_does_not_call_api(self, mock_client, db, app_config):
        worker = ForkWorker(mock_client, db, app_config, dry_run=True)
        fid = db.enqueue_fork("alice/repo")
        rec = db.get_pending_forks()[0]
        result = worker.execute(rec)

        mock_client.fork_repo.assert_not_called()
        assert result.success is True
        assert result.dry_run is True

    def test_repo_not_found_marks_skipped(self, mock_client, db, app_config):
        mock_client.fork_repo.side_effect = RepoNotFoundError("alice/gone")
        worker = ForkWorker(mock_client, db, app_config)
        db.enqueue_fork("alice/gone")
        rec = db.get_pending_forks()[0]
        result = worker.execute(rec)

        assert result.skipped is True
        assert result.success is False

        row = db.fetchone("SELECT status FROM forks WHERE id=?", (result.fork_id,))
        assert row["status"] == "skipped"

    def test_fork_error_marks_failed(self, mock_client, db, app_config):
        mock_client.fork_repo.side_effect = ForkError("API error", repo="alice/repo")
        worker = ForkWorker(mock_client, db, app_config)
        db.enqueue_fork("alice/repo")
        rec = db.get_pending_forks()[0]
        result = worker.execute(rec)

        assert result.success is False
        assert result.skipped is False
        assert "API error" in result.error

        row = db.fetchone("SELECT status FROM forks WHERE id=?", (result.fork_id,))
        assert row["status"] == "failed"

    def test_unexpected_exception_marks_failed(self, mock_client, db, app_config):
        mock_client.fork_repo.side_effect = RuntimeError("unexpected!")
        worker = ForkWorker(mock_client, db, app_config)
        db.enqueue_fork("alice/repo")
        rec = db.get_pending_forks()[0]
        result = worker.execute(rec)

        assert result.success is False
        assert "RuntimeError" in result.error

    def test_duration_is_positive(self, mock_client, db, app_config):
        worker = ForkWorker(mock_client, db, app_config)
        db.enqueue_fork("alice/repo")
        rec = db.get_pending_forks()[0]
        result = worker.execute(rec)

        assert result.duration_s >= 0.0


# ---------------------------------------------------------------------------
# BatchForker.enqueue
# ---------------------------------------------------------------------------

class TestBatchForkerEnqueue:
    def test_enqueue_single(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        fid = forker.enqueue("alice/lib")
        assert fid is not None
        assert fid > 0

    def test_enqueue_already_forked_returns_none(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        fid = forker.enqueue("alice/lib")
        db.mark_fork_success(fid, "me/lib")
        # Second enqueue returns None
        fid2 = forker.enqueue("alice/lib")
        assert fid2 is None

    def test_enqueue_many(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        repos = ["alice/r1", "alice/r2", "alice/r3"]
        result = forker.enqueue_many(repos)
        assert len(result) == 3
        assert all(v is not None for v in result.values())

    def test_enqueue_many_with_filter(self, mock_client, db, app_config):
        from gh_autofork.filters import FilterChain, min_stars
        forker = BatchForker(mock_client, db, app_config)

        # Override get_repo to return low-star repos for r2
        def _get_repo(full_name):
            stars = 5 if "r2" in full_name else 500
            return RepoInfo(make_repo_raw(full_name=full_name, stars=stars))
        mock_client.get_repo.side_effect = _get_repo

        chain = FilterChain([min_stars(100)])
        result = forker.enqueue_many(["alice/r1", "alice/r2", "alice/r3"], filter_chain=chain)

        assert result["alice/r2"] is None   # filtered out
        assert result["alice/r1"] is not None
        assert result["alice/r3"] is not None

    def test_enqueue_many_skips_comments(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        result = forker.enqueue_many(["# comment", "", "alice/valid"])
        assert len([v for v in result.values() if v is not None]) == 1

    def test_enqueue_from_user(self, mock_client, db, app_config):
        repos = [RepoInfo(make_repo_raw(f"alice/repo{i}")) for i in range(3)]
        mock_client.list_user_repos.return_value = iter(repos)

        forker = BatchForker(mock_client, db, app_config)
        n = forker.enqueue_from_user("alice")
        assert n == 3

    def test_enqueue_from_org(self, mock_client, db, app_config):
        repos = [RepoInfo(make_repo_raw(f"myorg/repo{i}")) for i in range(5)]
        mock_client.list_org_repos.return_value = iter(repos)

        forker = BatchForker(mock_client, db, app_config)
        n = forker.enqueue_from_org("myorg")
        assert n == 5

    def test_enqueue_from_search(self, mock_client, db, app_config):
        repos = [RepoInfo(make_repo_raw(f"user/repo{i}")) for i in range(4)]
        mock_client.search_repos.return_value = iter(repos)

        forker = BatchForker(mock_client, db, app_config)
        n = forker.enqueue_from_search("topic:ml", limit=10)
        assert n == 4

    def test_enqueue_from_search_respects_limit(self, mock_client, db, app_config):
        repos = [RepoInfo(make_repo_raw(f"user/repo{i}")) for i in range(20)]
        mock_client.search_repos.return_value = iter(repos)

        forker = BatchForker(mock_client, db, app_config)
        n = forker.enqueue_from_search("topic:ml", limit=5)
        assert n == 5

    def test_enqueue_from_file(self, mock_client, db, app_config, tmp_path):
        f = tmp_path / "repos.txt"
        f.write_text("alice/r1\n# comment\nalice/r2\n")

        forker = BatchForker(mock_client, db, app_config)
        result = forker.enqueue_from_file(str(f))
        queued = sum(1 for v in result.values() if v is not None)
        assert queued == 2


# ---------------------------------------------------------------------------
# BatchForker.run
# ---------------------------------------------------------------------------

class TestBatchForkerRun:
    def test_run_empty_queue_returns_empty_list(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        results = forker.run()
        assert results == []

    def test_run_processes_all_pending(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        for i in range(5):
            forker.enqueue(f"alice/repo{i}")

        results = forker.run()
        assert len(results) == 5
        assert all(r.success for r in results)

    def test_run_updates_database(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        forker.enqueue("alice/repo")
        forker.run()

        counts = db.count_forks_by_status()
        assert counts.get("success", 0) == 1
        assert counts.get("pending", 0) == 0

    def test_run_dry_mode_no_api_calls(self, mock_client, db, app_config):
        app_config.dry_run = True
        forker = BatchForker(mock_client, db, app_config, dry_run=True)
        forker.enqueue("alice/repo")
        results = forker.run()

        mock_client.fork_repo.assert_not_called()
        assert results[0].dry_run is True
        assert results[0].success is True

    def test_run_invokes_progress_callback(self, mock_client, db, app_config):
        calls = []
        def on_progress(p):
            calls.append(p.done)

        forker = BatchForker(mock_client, db, app_config, on_progress=on_progress)
        forker.enqueue("alice/repo1")
        forker.enqueue("alice/repo2")
        forker.run()

        # Callback was called; final call has done >= 2
        assert any(c >= 2 for c in calls)

    def test_run_invokes_result_callback(self, mock_client, db, app_config):
        results_seen = []
        forker = BatchForker(mock_client, db, app_config, on_result=results_seen.append)
        forker.enqueue("alice/repo")
        forker.run()

        assert len(results_seen) == 1
        assert results_seen[0].success is True

    def test_run_includes_retryable_by_default(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        fid = forker.enqueue("alice/repo")
        # Simulate one failure
        db.mark_fork_running(fid)
        db.mark_fork_failed(fid, "timeout")

        results = forker.run(include_retries=True)
        assert len(results) == 1
        assert results[0].success is True   # retry succeeded

    def test_run_skips_retries_when_disabled(self, mock_client, db, app_config):
        forker = BatchForker(mock_client, db, app_config)
        fid = forker.enqueue("alice/repo")
        db.mark_fork_running(fid)
        db.mark_fork_failed(fid, "err")

        results = forker.run(include_retries=False)
        assert results == []

    def test_cancel_stops_processing(self, mock_client, db, app_config):
        import threading, time

        app_config.batch.concurrency = 1
        # Slow down each fork
        original = mock_client.fork_repo.side_effect
        def slow_fork(*args, **kwargs):
            time.sleep(0.05)
            return original(*args, **kwargs)
        mock_client.fork_repo.side_effect = slow_fork

        forker = BatchForker(mock_client, db, app_config)
        for i in range(10):
            forker.enqueue(f"alice/repo{i}")

        def cancel_after_delay():
            time.sleep(0.1)
            forker.cancel()

        t = threading.Thread(target=cancel_after_delay, daemon=True)
        t.start()
        results = forker.run()
        t.join()

        # Should have processed fewer than all 10
        assert len(results) < 10

    def test_job_id_tracked(self, mock_client, db, app_config):
        job_id = db.create_job("test", {})
        forker = BatchForker(mock_client, db, app_config, job_id=job_id)
        forker.enqueue("alice/repo")
        forker.run()

        job = db.get_job(job_id)
        assert job["status"] == "success"
        assert job["progress"] >= 1

    def test_webhook_notified_on_complete(self, mock_client, db, app_config):
        app_config.webhook_url = "https://example.com/hook"
        mock_client.notify_webhook.return_value = True

        forker = BatchForker(mock_client, db, app_config)
        forker.enqueue("alice/repo")
        forker.run()

        mock_client.notify_webhook.assert_called_once()
        call_args = mock_client.notify_webhook.call_args
        payload = call_args[0][1]
        assert payload["event"] == "batch_complete"
        assert payload["success"] == 1

    def test_concurrent_forks_use_multiple_workers(self, mock_client, db, app_config):
        import time as _time

        app_config.batch.concurrency = 4
        concurrent_peaks = []

        def counting_fork(full_name, **kwargs):
            _time.sleep(0.05)
            return RepoInfo(make_repo_raw(
                full_name=f"testuser/{full_name.split('/')[-1]}",
                is_fork=True,
            ))

        mock_client.fork_repo.side_effect = counting_fork

        forker = BatchForker(mock_client, db, app_config)
        for i in range(8):
            forker.enqueue(f"alice/repo{i}")

        start = _time.monotonic()
        results = forker.run()
        elapsed = _time.monotonic() - start

        # 8 tasks * 0.05s = 0.4s serial; with 4 workers ≈ 0.1s
        # Allow generous headroom for CI but confirm it didn't run fully serially
        assert len(results) == 8
        # Serial would be ≥ 0.4s; parallel should be well under 0.35s
        assert elapsed < 0.35, f"Expected parallel execution, but took {elapsed:.2f}s"
