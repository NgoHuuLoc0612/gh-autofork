"""
Core batch-forking engine.

BatchForker orchestrates concurrent fork operations:

  - Reads pending forks from the SQLite queue
  - Forks them concurrently (ThreadPoolExecutor)
  - Records success/failure back to the database
  - Enforces per-worker delays to respect secondary rate limits
  - Supports dry-run mode (no API calls; just marks as would-succeed)
  - Emits progress events via an optional callback
  - Can be paused/cancelled mid-run
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .config import AppConfig
from .database import Database
from .exceptions import (
    AlreadyForkedError,
    ForkError,
    GHAutoForkError,
    RateLimitError,
    RepoNotFoundError,
)
from .filters import FilterChain
from .github_client import GitHubClient, RepoInfo

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Progress / event data classes
# ---------------------------------------------------------------------------

@dataclass
class ForkResult:
    fork_id: int
    repo_full_name: str
    fork_full_name: str = ""
    success: bool = False
    skipped: bool = False
    dry_run: bool = False
    error: str = ""
    duration_s: float = 0.0


@dataclass
class BatchProgress:
    total: int = 0
    done: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    current_repo: str = ""
    is_done: bool = False
    job_id: Optional[int] = None

    @property
    def pct(self) -> float:
        if self.total == 0:
            return 0.0
        return self.done / self.total * 100.0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)


ProgressCallback = Callable[[BatchProgress], None]
ResultCallback   = Callable[[ForkResult], None]


# ---------------------------------------------------------------------------
# ForkWorker – single-threaded forking logic
# ---------------------------------------------------------------------------

class ForkWorker:
    """
    Handles forking a single repo and all retry / backoff logic within
    the batch context.  Designed to be called from a thread pool.
    """

    def __init__(
        self,
        client: GitHubClient,
        db: Database,
        cfg: AppConfig,
        dry_run: bool = False,
    ):
        self._client  = client
        self._db      = db
        self._cfg     = cfg
        self._dry_run = dry_run

    def execute(self, fork_record: dict) -> ForkResult:
        """
        Process one fork queue entry.  Returns a ForkResult.
        This method is designed to be exception-safe (all errors are
        captured in the returned ForkResult).
        """
        fork_id       = fork_record["id"]
        repo_full_name = fork_record["repo_full_name"]
        organization  = fork_record.get("organization") or ""
        record_dry_run = bool(fork_record.get("dry_run")) or self._dry_run

        start = time.monotonic()
        self._db.mark_fork_running(fork_id)

        # ---- Dry run ----
        if record_dry_run:
            time.sleep(0.1)
            dummy_fork = f"{organization or self._client.auth_login}/{repo_full_name.split('/')[-1]}"
            self._db.mark_fork_success(fork_id, dummy_fork)
            log.info("[DRY-RUN] Would fork '%s' → '%s'", repo_full_name, dummy_fork)
            return ForkResult(
                fork_id=fork_id,
                repo_full_name=repo_full_name,
                fork_full_name=dummy_fork,
                success=True,
                dry_run=True,
                duration_s=time.monotonic() - start,
            )

        # ---- Real fork ----
        try:
            fork_repo = self._client.fork_repo(repo_full_name, organization=organization)
            self._db.mark_fork_success(fork_id, fork_repo.full_name)

            # Update the cached repo record with the source repo's metadata
            # (it may have been queued without a full API fetch)
            try:
                src = self._client.get_repo(repo_full_name)
                if src:
                    self._db.upsert_repo(src.to_db_dict())
            except Exception:
                pass  # non-critical

            log.info("Forked '%s' → '%s'", repo_full_name, fork_repo.full_name)
            delay = self._cfg.batch.fork_delay
            if delay > 0:
                time.sleep(delay)

            return ForkResult(
                fork_id=fork_id,
                repo_full_name=repo_full_name,
                fork_full_name=fork_repo.full_name,
                success=True,
                duration_s=time.monotonic() - start,
            )

        except RepoNotFoundError as exc:
            self._db.mark_fork_skipped(fork_id, f"Not found: {exc}")
            log.warning("Skipping '%s': repo not found.", repo_full_name)
            return ForkResult(
                fork_id=fork_id, repo_full_name=repo_full_name,
                skipped=True, error=str(exc),
                duration_s=time.monotonic() - start,
            )

        except AlreadyForkedError as exc:
            self._db.mark_fork_skipped(fork_id, "Already forked")
            log.info("Skipping '%s': already forked.", repo_full_name)
            return ForkResult(
                fork_id=fork_id, repo_full_name=repo_full_name,
                skipped=True, error=str(exc),
                duration_s=time.monotonic() - start,
            )

        except RateLimitError as exc:
            self._db.mark_fork_failed(fork_id, str(exc))
            log.error("Rate limit error forking '%s': %s", repo_full_name, exc)
            return ForkResult(
                fork_id=fork_id, repo_full_name=repo_full_name,
                success=False, error=str(exc),
                duration_s=time.monotonic() - start,
            )

        except (ForkError, GHAutoForkError) as exc:
            self._db.mark_fork_failed(fork_id, str(exc))
            log.error("Failed to fork '%s': %s", repo_full_name, exc)
            return ForkResult(
                fork_id=fork_id, repo_full_name=repo_full_name,
                success=False, error=str(exc),
                duration_s=time.monotonic() - start,
            )

        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            self._db.mark_fork_failed(fork_id, msg)
            log.exception("Unexpected error forking '%s'", repo_full_name)
            return ForkResult(
                fork_id=fork_id, repo_full_name=repo_full_name,
                success=False, error=msg,
                duration_s=time.monotonic() - start,
            )


# ---------------------------------------------------------------------------
# BatchForker
# ---------------------------------------------------------------------------

class BatchForker:
    """
    Manages a concurrent batch fork operation.

    Usage::

        forker = BatchForker(client, db, cfg)
        forker.enqueue("torvalds/linux")
        forker.enqueue("microsoft/vscode")
        results = forker.run()
    """

    def __init__(
        self,
        client: GitHubClient,
        db: Database,
        cfg: AppConfig,
        dry_run: bool = False,
        on_progress: Optional[ProgressCallback] = None,
        on_result: Optional[ResultCallback] = None,
        job_id: Optional[int] = None,
    ):
        self._client      = client
        self._db          = db
        self._cfg         = cfg
        self._dry_run     = dry_run or cfg.dry_run
        self._on_progress = on_progress
        self._on_result   = on_result
        self._job_id      = job_id
        self._cancel_flag = threading.Event()

    # ------------------------------------------------------------------
    # Queue management helpers
    # ------------------------------------------------------------------

    def enqueue(
        self,
        repo_full_name: str,
        organization: str = "",
        source: str = "manual",
        priority: int = 0,
    ) -> Optional[int]:
        """
        Add *repo_full_name* to the fork queue.
        Fetches basic repo metadata from the API if not cached.
        Returns the fork queue row id, or None if already successfully forked.
        """
        # Ensure repo is in the repos table
        db_repo = self._db.get_repo(repo_full_name)
        repo_id: Optional[int] = None
        if not db_repo:
            try:
                info = self._client.get_repo(repo_full_name)
                if info:
                    repo_id = self._db.upsert_repo(info.to_db_dict())
            except Exception as exc:
                log.debug("Could not fetch repo metadata for '%s': %s", repo_full_name, exc)
        else:
            repo_id = db_repo["id"]

        return self._db.enqueue_fork(
            repo_full_name,
            organization=organization,
            source=source,
            priority=priority,
            dry_run=self._dry_run,
            repo_id=repo_id,
        )

    def enqueue_many(
        self,
        repos: List[str],
        organization: str = "",
        source: str = "batch",
        filter_chain: Optional[FilterChain] = None,
    ) -> Dict[str, Optional[int]]:
        """
        Enqueue a list of 'owner/repo' strings.
        If *filter_chain* is provided, repos that don't match are silently skipped.
        Returns a mapping of repo_full_name → fork_queue_id (None if skipped/already done).
        """
        results: Dict[str, Optional[int]] = {}
        for full_name in repos:
            full_name = full_name.strip()
            if not full_name or full_name.startswith("#"):
                continue
            if filter_chain:
                # We need a RepoInfo to filter; fetch or create stub
                db_row = self._db.get_repo(full_name)
                if db_row:
                    stub = _db_row_to_repo_info(db_row)
                else:
                    try:
                        stub = self._client.get_repo(full_name)
                    except Exception:
                        stub = None
                if stub and not filter_chain(stub):
                    log.debug("Filtered out '%s'", full_name)
                    results[full_name] = None
                    continue
            fork_id = self.enqueue(full_name, organization=organization, source=source)
            results[full_name] = fork_id
        return results

    def enqueue_from_file(
        self,
        path: str,
        organization: str = "",
        filter_chain: Optional[FilterChain] = None,
    ) -> Dict[str, Optional[int]]:
        """
        Read a newline-separated list of 'owner/repo' entries from *path*.
        Lines starting with '#' are treated as comments.
        """
        repos = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    repos.append(line)
        return self.enqueue_many(repos, organization=organization, source="batch_file", filter_chain=filter_chain)

    def enqueue_from_user(
        self,
        username: str,
        organization: str = "",
        filter_chain: Optional[FilterChain] = None,
    ) -> int:
        """Enumerate and enqueue all public repos of *username*. Returns count enqueued."""
        count = 0
        for repo in self._client.list_user_repos(username):
            if filter_chain and not filter_chain(repo):
                continue
            self._db.upsert_repo(repo.to_db_dict())
            fid = self._db.enqueue_fork(
                repo.full_name,
                organization=organization,
                source="watch",
                dry_run=self._dry_run,
            )
            if fid is not None:
                count += 1
        return count

    def enqueue_from_org(
        self,
        org: str,
        organization: str = "",
        filter_chain: Optional[FilterChain] = None,
    ) -> int:
        """Enumerate and enqueue all public repos of *org*. Returns count enqueued."""
        count = 0
        for repo in self._client.list_org_repos(org):
            if filter_chain and not filter_chain(repo):
                continue
            self._db.upsert_repo(repo.to_db_dict())
            fid = self._db.enqueue_fork(
                repo.full_name,
                organization=organization,
                source="watch",
                dry_run=self._dry_run,
            )
            if fid is not None:
                count += 1
        return count

    def enqueue_from_search(
        self,
        query: str,
        organization: str = "",
        limit: int = 1000,
        filter_chain: Optional[FilterChain] = None,
    ) -> int:
        """Run a GitHub search and enqueue matching repos. Returns count enqueued."""
        count = 0
        try:
            for repo in self._client.search_repos(query):
                if count >= limit:
                    break
                if filter_chain and not filter_chain(repo):
                    continue
                self._db.upsert_repo(repo.to_db_dict())
                fid = self._db.enqueue_fork(
                    repo.full_name,
                    organization=organization,
                    source="search",
                    dry_run=self._dry_run,
                )
                if fid is not None:
                    count += 1
        except Exception as exc:
            log.error("Search failed for query '%s': %s", query, exc)
            raise
        return count

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run(
        self,
        include_retries: bool = True,
        max_workers: Optional[int] = None,
    ) -> List[ForkResult]:
        """
        Execute all pending (and optionally retryable-failed) fork tasks.

        :param include_retries: also process previously-failed tasks within
                                max_attempts.
        :param max_workers: override cfg.batch.concurrency.
        :returns: list of ForkResult objects in completion order.
        """
        pending = self._db.get_pending_forks(limit=10_000)
        if include_retries:
            retries = self._db.get_retryable_forks(
                max_attempts=self._cfg.batch.max_attempts, limit=5_000
            )
            # deduplicate by id
            seen: Set[int] = {r["id"] for r in pending}
            for r in retries:
                if r["id"] not in seen:
                    pending.append(r)
                    seen.add(r["id"])

        if not pending:
            log.info("No pending forks in queue.")
            return []

        concurrency = max_workers or self._cfg.batch.concurrency
        total = len(pending)
        progress = BatchProgress(total=total, job_id=self._job_id)
        if self._job_id:
            self._db.start_job(self._job_id, total)

        worker = ForkWorker(self._client, self._db, self._cfg, dry_run=self._dry_run)
        results: List[ForkResult] = []

        log.info("Starting batch fork: %d repos, concurrency=%d", total, concurrency)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            future_to_rec: Dict[Future, dict] = {}
            for rec in pending:
                if self._cancel_flag.is_set():
                    break
                progress.current_repo = rec["repo_full_name"]
                self._emit_progress(progress)
                f = pool.submit(worker.execute, rec)
                future_to_rec[f] = rec

            for future in as_completed(future_to_rec):
                if self._cancel_flag.is_set():
                    # Cancel remaining futures
                    for pending_future in future_to_rec:
                        pending_future.cancel()
                    break

                result = future.result()   # ForkWorker is exception-safe
                results.append(result)

                progress.done += 1
                if result.success:
                    progress.success += 1
                elif result.skipped:
                    progress.skipped += 1
                else:
                    progress.failed += 1

                if self._job_id:
                    self._db.update_job_progress(
                        self._job_id,
                        progress.done,
                        progress.success,
                        progress.failed,
                        progress.skipped,
                    )

                if self._on_result:
                    try:
                        self._on_result(result)
                    except Exception:
                        pass

                self._emit_progress(progress)

        progress.is_done = True
        self._emit_progress(progress)

        if self._job_id:
            success = progress.failed == 0
            self._db.finish_job(self._job_id, success=success)

        log.info(
            "Batch fork complete: %d success, %d failed, %d skipped",
            progress.success, progress.failed, progress.skipped,
        )

        # Notify webhook
        if self._cfg.webhook_url:
            self._client.notify_webhook(
                self._cfg.webhook_url,
                {
                    "event": "batch_complete",
                    "success": progress.success,
                    "failed":  progress.failed,
                    "skipped": progress.skipped,
                    "total":   total,
                    "dry_run": self._dry_run,
                },
            )

        return results

    def cancel(self) -> None:
        """Signal the running batch to stop after the current set of tasks completes."""
        log.info("Cancellation requested.")
        self._cancel_flag.set()

    def _emit_progress(self, progress: BatchProgress) -> None:
        if self._on_progress:
            try:
                self._on_progress(progress)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helper: convert a DB row back to a minimal RepoInfo
# ---------------------------------------------------------------------------

def _db_row_to_repo_info(row: dict) -> RepoInfo:
    import json
    raw = {
        "id":                 row.get("github_id", 0),
        "full_name":          row.get("full_name", ""),
        "owner":              {"login": row.get("owner", "")},
        "name":               row.get("name", ""),
        "description":        row.get("description", ""),
        "stargazers_count":   row.get("stars", 0),
        "forks_count":        row.get("forks_count", 0),
        "language":           row.get("language"),
        "topics":             json.loads(row.get("topics") or "[]"),
        "private":            bool(row.get("is_private")),
        "archived":           bool(row.get("is_archived")),
        "fork":               bool(row.get("is_fork")),
        "size":               row.get("size_kb", 0),
        "license":            {"spdx_id": row.get("license")} if row.get("license") else None,
        "homepage":           row.get("homepage", ""),
        "default_branch":     row.get("default_branch", "main"),
        "open_issues_count":  row.get("open_issues", 0),
        "clone_url":          row.get("clone_url", ""),
        "ssh_url":            row.get("ssh_url", ""),
        "created_at":         row.get("created_at", ""),
        "updated_at":         row.get("updated_at", ""),
        "pushed_at":          row.get("pushed_at", ""),
    }
    return RepoInfo(raw)
