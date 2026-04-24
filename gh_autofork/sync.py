"""
Fork synchronization engine.

The SyncEngine iterates over all successfully forked repositories in
the database, checks how far behind each fork is from its upstream,
and optionally syncs them via the GitHub API's merge-upstream endpoint.

Sync results are recorded in the sync_history table.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

from .config import AppConfig
from .database import Database
from .exceptions import GHAutoForkError, SyncError
from .github_client import GitHubClient

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    repo_full_name: str
    fork_full_name: str
    status: str          # up_to_date | synced | failed | skipped
    commits_behind: int = 0
    error: str = ""
    duration_s: float = 0.0


SyncProgressCallback = Callable[[int, int, str], None]


class SyncEngine:
    """
    Checks each successfully forked repo against its upstream and syncs
    if it is behind.

    Usage::

        engine = SyncEngine(client, db, cfg)
        results = engine.run()
    """

    def __init__(
        self,
        client: GitHubClient,
        db: Database,
        cfg: AppConfig,
        on_progress: Optional[SyncProgressCallback] = None,
        job_id: Optional[int] = None,
    ):
        self._client      = client
        self._db          = db
        self._cfg         = cfg
        self._sync_cfg    = cfg.sync
        self._on_progress = on_progress
        self._job_id      = job_id

    def run(self, force: bool = False) -> List[SyncResult]:
        """
        Sync all forks.

        :param force: sync even if the fork appears up-to-date in the last
                      sync record (useful after a long offline period).
        :returns: list of SyncResult in processing order.
        """
        if not self._sync_cfg.enabled and not force:
            log.info("Fork sync is disabled in config (sync.enabled=false).")
            return []

        successful_forks = self._db.list_forks(status="success", limit=50_000)
        if not successful_forks:
            log.info("No successfully forked repos to sync.")
            return []

        total = len(successful_forks)
        if self._job_id:
            self._db.start_job(self._job_id, total)

        results: List[SyncResult] = []
        for idx, fork_rec in enumerate(successful_forks, start=1):
            repo_full_name = fork_rec["repo_full_name"]
            fork_full_name = fork_rec.get("fork_full_name") or ""

            if self._on_progress:
                self._on_progress(idx, total, repo_full_name)

            if not fork_full_name:
                log.debug("Skipping '%s': fork_full_name unknown.", repo_full_name)
                continue

            result = self._sync_one(repo_full_name, fork_full_name, fork_rec["id"], force=force)
            results.append(result)

            # Small delay to avoid abuse detection
            time.sleep(0.3)

        if self._job_id:
            success_count = sum(1 for r in results if r.status in ("synced", "up_to_date"))
            fail_count    = sum(1 for r in results if r.status == "failed")
            self._db.update_job_progress(self._job_id, total, success_count, fail_count, 0)
            self._db.finish_job(self._job_id, success=fail_count == 0)

        synced = sum(1 for r in results if r.status == "synced")
        up_to_date = sum(1 for r in results if r.status == "up_to_date")
        failed = sum(1 for r in results if r.status == "failed")
        log.info("Sync complete: %d synced, %d up-to-date, %d failed.", synced, up_to_date, failed)

        return results

    def _sync_one(
        self,
        repo_full_name: str,
        fork_full_name: str,
        fork_id: int,
        force: bool = False,
    ) -> SyncResult:
        start = time.monotonic()

        # Optionally check last sync record to skip if recently synced
        if not force:
            last = self._db.last_sync(repo_full_name)
            if last and last["status"] == "synced":
                # re-sync only if 6+ hours have passed
                try:
                    synced_dt = datetime.fromisoformat(last["synced_at"])
                    age_hours = (datetime.utcnow() - synced_dt.replace(tzinfo=None)).total_seconds() / 3600
                    if age_hours < 6:
                        log.debug("Skipping sync for '%s': synced %.1fh ago.", fork_full_name, age_hours)
                        return SyncResult(
                            repo_full_name=repo_full_name,
                            fork_full_name=fork_full_name,
                            status="up_to_date",
                            duration_s=time.monotonic() - start,
                        )
                except Exception:
                    pass

        try:
            # First, compare commits to know how far behind we are
            compare = self._client.compare_commits(repo_full_name, fork_full_name)
            behind = compare.get("behind_by", 0)
            upstream_sha = compare.get("base_commit", "")
            fork_sha     = compare.get("head_commit", "")

            min_behind = self._sync_cfg.min_commits_behind
            if behind < min_behind:
                log.debug(
                    "'%s' is %d commits behind (min=%d); skipping.",
                    fork_full_name, behind, min_behind,
                )
                self._record_sync(fork_id, repo_full_name, fork_full_name,
                                  upstream_sha, fork_sha, "up_to_date", 0)
                return SyncResult(
                    repo_full_name=repo_full_name,
                    fork_full_name=fork_full_name,
                    status="up_to_date",
                    commits_behind=behind,
                    duration_s=time.monotonic() - start,
                )

            # Sync
            log.info("Syncing '%s' (%d commits behind)…", fork_full_name, behind)
            changed, message = self._client.sync_fork(fork_full_name)

            status = "synced" if changed else "up_to_date"
            self._record_sync(fork_id, repo_full_name, fork_full_name,
                              upstream_sha, fork_sha, status, behind)

            log.info("Sync '%s': %s", fork_full_name, message)
            return SyncResult(
                repo_full_name=repo_full_name,
                fork_full_name=fork_full_name,
                status=status,
                commits_behind=behind,
                duration_s=time.monotonic() - start,
            )

        except GHAutoForkError as exc:
            err = str(exc)
            log.error("Sync failed for '%s': %s", fork_full_name, err)
            self._record_sync(fork_id, repo_full_name, fork_full_name,
                              "", "", "failed", 0, err)
            return SyncResult(
                repo_full_name=repo_full_name,
                fork_full_name=fork_full_name,
                status="failed",
                error=err,
                duration_s=time.monotonic() - start,
            )

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log.exception("Unexpected error syncing '%s'", fork_full_name)
            self._record_sync(fork_id, repo_full_name, fork_full_name,
                              "", "", "failed", 0, err)
            return SyncResult(
                repo_full_name=repo_full_name,
                fork_full_name=fork_full_name,
                status="failed",
                error=err,
                duration_s=time.monotonic() - start,
            )

    def _record_sync(
        self,
        fork_id: int,
        repo_full_name: str,
        fork_full_name: str,
        upstream_sha: str,
        fork_sha: str,
        status: str,
        commits_behind: int,
        error: str = "",
    ) -> None:
        self._db.record_sync({
            "fork_id":        fork_id if fork_id else None,
            "repo_full_name": repo_full_name,
            "fork_full_name": fork_full_name,
            "upstream_sha":   upstream_sha or None,
            "fork_sha":       fork_sha or None,
            "status":         status,
            "commits_behind": commits_behind,
            "error_message":  error or None,
            "synced_at":      datetime.now(timezone.utc).isoformat(),
        })

    def sync_single(self, fork_full_name: str, force: bool = False) -> SyncResult:
        """
        Sync a single fork by its full name (e.g. 'myuser/linux').
        Looks up the upstream from the GitHub API.
        """
        # Fetch the fork's metadata to find its parent
        repo_info = self._client.get_repo(fork_full_name)
        if repo_info is None:
            return SyncResult(
                repo_full_name="",
                fork_full_name=fork_full_name,
                status="failed",
                error="Repository not found",
            )
        if not repo_info.is_fork:
            return SyncResult(
                repo_full_name=repo_info.full_name,
                fork_full_name=fork_full_name,
                status="skipped",
                error="Not a fork",
            )

        upstream = repo_info.fork_parent
        if not upstream:
            return SyncResult(
                repo_full_name="",
                fork_full_name=fork_full_name,
                status="failed",
                error="Cannot determine upstream parent",
            )

        # Locate the DB record
        fork_rec = self._db.get_fork(upstream, "")
        fork_id = fork_rec["id"] if fork_rec else 0

        return self._sync_one(upstream, fork_full_name, fork_id, force=force)
