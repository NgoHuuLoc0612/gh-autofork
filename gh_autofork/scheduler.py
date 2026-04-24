"""
Background scheduler daemon for gh-autofork.

Runs in the foreground (or as a systemd/launchd service) and executes
jobs on a cron-like schedule:

  - fork_cron  → BatchForker.run() with watchlist sources
  - sync_cron  → SyncEngine.run()

Uses the ``croniter`` library for cron expression parsing.
Falls back to simple interval scheduling if croniter is not installed.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Callable, List, Optional

from .config import AppConfig, load_config
from .database import Database
from .filters import FilterChain
from .forker import BatchForker
from .github_client import GitHubClient
from .sync import SyncEngine
from .utils import setup_logging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cron-next helper
# ---------------------------------------------------------------------------

def _next_run(cron_expr: str, after: Optional[datetime] = None) -> datetime:
    """
    Return the next datetime for *cron_expr* after *after* (UTC).
    Falls back to "run in 1 hour" if croniter is unavailable.
    """
    if not cron_expr:
        # Disabled – return far future
        return datetime(9999, 12, 31, tzinfo=timezone.utc)

    now = after or datetime.now(timezone.utc)
    try:
        from croniter import croniter  # type: ignore
        it = croniter(cron_expr, now)
        nxt = it.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return nxt
    except ImportError:
        # No croniter – use 1-hour intervals
        from datetime import timedelta
        return now + timedelta(hours=1)
    except Exception as exc:
        log.warning("Invalid cron expression '%s': %s; defaulting to 1h interval.", cron_expr, exc)
        from datetime import timedelta
        return now + timedelta(hours=1)


def _seconds_until(target: datetime) -> float:
    now = datetime.now(timezone.utc)
    return max(0.0, (target - now).total_seconds())


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Long-running daemon that executes fork and sync jobs on schedule.

    Call ``start()`` (blocking) or ``start_background()`` (non-blocking thread).
    Call ``stop()`` to gracefully shut down.
    """

    def __init__(self, cfg: AppConfig, config_file: Optional[Path] = None):
        self._cfg = cfg
        self._config_file = config_file
        self._stop_event = Event()

        self._db     = Database(cfg.db_path)
        self._client = GitHubClient(cfg)
        self._filter = FilterChain.from_config(
            cfg.filters, auth_login=self._client.auth_login
        )

        self._fork_thread: Optional[Thread] = None
        self._sync_thread: Optional[Thread] = None

        # Register SIGTERM / SIGINT handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        log.info("Received signal %d; shutting down…", signum)
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Block until stop() is called or a signal is received."""
        log.info("gh-autofork scheduler starting…")
        self._run_startup_repos()

        # Schedule next run times
        fork_next = _next_run(self._cfg.scheduler.fork_cron)
        sync_next = _next_run(self._cfg.scheduler.sync_cron)

        log.info("Next fork job:  %s", fork_next.strftime("%Y-%m-%d %H:%M UTC"))
        log.info("Next sync job:  %s", sync_next.strftime("%Y-%m-%d %H:%M UTC"))

        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc)

            if self._cfg.scheduler.fork_cron and now >= fork_next:
                if not self._is_running(self._fork_thread):
                    self._fork_thread = Thread(
                        target=self._run_fork_job, daemon=True, name="fork-worker"
                    )
                    self._fork_thread.start()
                fork_next = _next_run(self._cfg.scheduler.fork_cron)
                log.info("Next fork job:  %s", fork_next.strftime("%Y-%m-%d %H:%M UTC"))

            if self._cfg.scheduler.sync_cron and now >= sync_next:
                if not self._is_running(self._sync_thread):
                    self._sync_thread = Thread(
                        target=self._run_sync_job, daemon=True, name="sync-worker"
                    )
                    self._sync_thread.start()
                sync_next = _next_run(self._cfg.scheduler.sync_cron)
                log.info("Next sync job:  %s", sync_next.strftime("%Y-%m-%d %H:%M UTC"))

            self._stop_event.wait(timeout=30)  # check every 30 s

        # Graceful shutdown: wait for running jobs
        log.info("Waiting for running jobs to finish…")
        for t in (self._fork_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=120)
        log.info("Scheduler stopped.")

    def start_background(self) -> Thread:
        """Start the scheduler in a daemon thread and return it."""
        t = Thread(target=self.start, daemon=True, name="gh-autofork-scheduler")
        t.start()
        return t

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Internal job runners
    # ------------------------------------------------------------------

    def _run_startup_repos(self) -> None:
        """Fork repos listed in cfg.startup_repos immediately at daemon start."""
        repos = self._cfg.startup_repos
        if not repos:
            return
        log.info("Queuing %d startup repos…", len(repos))
        job_id = self._db.create_job("startup_fork", {"repos": repos})
        forker = BatchForker(
            self._client, self._db, self._cfg,
            dry_run=self._cfg.dry_run,
            job_id=job_id,
        )
        for repo in repos:
            forker.enqueue(repo.strip(), source="startup")
        forker.run()

    def _run_fork_job(self) -> None:
        """Execute a full fork cycle: reload config, process watchlist, run batch."""
        log.info("[fork-job] Starting scheduled fork cycle.")
        try:
            # Reload config from disk so hot-edited YAML takes effect
            cfg = _reload_or_use(self._cfg, self._config_file)
            client = GitHubClient(cfg)
            filter_chain = FilterChain.from_config(cfg.filters, auth_login=client.auth_login)

            job_id = self._db.create_job("scheduled_fork", {
                "fork_cron": cfg.scheduler.fork_cron,
            })
            forker = BatchForker(
                client, self._db, cfg,
                dry_run=cfg.dry_run,
                job_id=job_id,
            )

            # Process watchlist
            watches = self._db.list_watches(enabled_only=True)
            for w in watches:
                wid, kind, target = w["id"], w["kind"], w["target"]
                try:
                    if kind == "user":
                        n = forker.enqueue_from_user(target, filter_chain=filter_chain)
                        log.info("[fork-job] Queued %d repos from user '%s'.", n, target)
                    elif kind == "org":
                        n = forker.enqueue_from_org(target, filter_chain=filter_chain)
                        log.info("[fork-job] Queued %d repos from org '%s'.", n, target)
                    elif kind == "search":
                        n = forker.enqueue_from_search(target, filter_chain=filter_chain)
                        log.info("[fork-job] Queued %d repos from search '%s'.", n, target)
                    self._db.mark_watch_ran(wid)
                except Exception as exc:
                    log.error("[fork-job] Error processing watchlist entry '%s': %s", target, exc)

            # Also run saved searches from config
            for q in cfg.saved_searches:
                try:
                    n = forker.enqueue_from_search(q, filter_chain=filter_chain)
                    log.info("[fork-job] Queued %d repos from saved search '%s'.", n, q)
                except Exception as exc:
                    log.error("[fork-job] Error with saved search '%s': %s", q, exc)

            forker.run()
            log.info("[fork-job] Scheduled fork cycle complete.")
        except Exception as exc:
            log.exception("[fork-job] Unhandled error in fork cycle: %s", exc)

    def _run_sync_job(self) -> None:
        """Execute a full sync cycle."""
        log.info("[sync-job] Starting scheduled sync cycle.")
        try:
            cfg = _reload_or_use(self._cfg, self._config_file)
            client = GitHubClient(cfg)
            job_id = self._db.create_job("scheduled_sync", {
                "sync_cron": cfg.scheduler.sync_cron,
            })
            engine = SyncEngine(client, self._db, cfg, job_id=job_id)
            engine.run()
            log.info("[sync-job] Scheduled sync cycle complete.")
        except Exception as exc:
            log.exception("[sync-job] Unhandled error in sync cycle: %s", exc)

    @staticmethod
    def _is_running(thread: Optional[Thread]) -> bool:
        return thread is not None and thread.is_alive()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _reload_or_use(current: AppConfig, config_file: Optional[Path]) -> AppConfig:
    try:
        return load_config(config_file)
    except Exception as exc:
        log.warning("Could not reload config (%s); using current config.", exc)
        return current


# ---------------------------------------------------------------------------
# Entry point used by the CLI `daemon` command
# ---------------------------------------------------------------------------

def run_daemon(config_file: Optional[Path] = None) -> None:
    """Load config, set up logging, start scheduler (blocking)."""
    cfg = load_config(config_file)
    setup_logging(cfg)
    scheduler = Scheduler(cfg, config_file=config_file)
    scheduler.start()
