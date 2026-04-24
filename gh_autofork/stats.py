"""
Statistics and reporting for gh-autofork.

Generates human-readable and machine-readable summaries of
the fork queue state, sync history, job history, and rate limits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .database import Database
from .github_client import GitHubClient, RateLimitInfo


@dataclass
class ForkQueueStats:
    total: int
    pending: int
    running: int
    success: int
    failed: int
    skipped: int
    cancelled: int

    # computed
    success_rate_pct: float = 0.0

    def __post_init__(self):
        completed = self.success + self.failed + self.skipped
        self.success_rate_pct = (self.success / completed * 100) if completed > 0 else 0.0


@dataclass
class TopLanguage:
    language: str
    count: int


@dataclass
class RecentFork:
    repo: str
    fork: str
    completed_at: str


@dataclass
class JobSummary:
    id: int
    job_type: str
    status: str
    progress: int
    total: int
    success_count: int
    fail_count: int
    skip_count: int
    created_at: str
    completed_at: Optional[str]

    @property
    def duration(self) -> str:
        if not self.completed_at or not self.created_at:
            return "N/A"
        try:
            start = datetime.fromisoformat(self.created_at)
            end   = datetime.fromisoformat(self.completed_at)
            secs  = int((end - start).total_seconds())
            if secs < 60:
                return f"{secs}s"
            return f"{secs // 60}m {secs % 60}s"
        except Exception:
            return "?"


@dataclass
class FullStats:
    generated_at: str
    queue: ForkQueueStats
    top_languages: List[TopLanguage]
    recent_success: List[RecentFork]
    job_history: List[JobSummary]
    rate_limit: Optional[Dict[str, Any]]
    watchlist: List[Dict[str, Any]]
    sync_summary: Dict[str, int]


def collect_stats(db: Database, client: Optional[GitHubClient] = None) -> FullStats:
    """Gather all statistics into a FullStats object."""

    # Queue counts
    counts = db.count_forks_by_status()
    queue = ForkQueueStats(
        total     = sum(counts.values()),
        pending   = counts.get("pending", 0),
        running   = counts.get("running", 0),
        success   = counts.get("success", 0),
        failed    = counts.get("failed", 0),
        skipped   = counts.get("skipped", 0),
        cancelled = counts.get("cancelled", 0),
    )

    # Top languages
    raw_stats = db.fork_stats()
    top_langs = [
        TopLanguage(language=r["language"], count=r["cnt"])
        for r in raw_stats.get("top_languages", [])
    ]

    # Recent successful forks
    recent = [
        RecentFork(
            repo=r["repo_full_name"],
            fork=r.get("fork_full_name") or "",
            completed_at=r.get("completed_at") or "",
        )
        for r in raw_stats.get("recent_success", [])
    ]

    # Job history
    raw_jobs = db.list_jobs(limit=10)
    job_history = [
        JobSummary(
            id=j["id"],
            job_type=j["job_type"],
            status=j["status"],
            progress=j.get("progress", 0),
            total=j.get("total", 0),
            success_count=j.get("success_count", 0),
            fail_count=j.get("fail_count", 0),
            skip_count=j.get("skip_count", 0),
            created_at=j.get("created_at", ""),
            completed_at=j.get("completed_at"),
        )
        for j in raw_jobs
    ]

    # Rate limit
    rl_data: Optional[Dict[str, Any]] = None
    if client:
        try:
            rl: RateLimitInfo = client.get_rate_limit()
            rl_data = {
                "remaining": rl.remaining,
                "limit":     rl.limit,
                "used":      rl.used,
                "reset_at":  rl.reset_dt.strftime("%Y-%m-%d %H:%M UTC"),
                "seconds_until_reset": int(rl.seconds_until_reset),
            }
        except Exception:
            # Fallback to cached DB value
            db_rl = db.latest_rate_limit()
            if db_rl:
                rl_data = {
                    "remaining": db_rl["remaining"],
                    "limit":     db_rl["limit_total"],
                    "reset_at":  db_rl["reset_at"],
                }

    # Watchlist
    watchlist = db.list_watches(enabled_only=False)

    # Sync summary
    raw_sync = db.fetchall(
        "SELECT status, COUNT(*) as cnt FROM sync_history GROUP BY status"
    )
    sync_summary = {r["status"]: r["cnt"] for r in raw_sync}

    return FullStats(
        generated_at  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        queue         = queue,
        top_languages = top_langs,
        recent_success= recent,
        job_history   = job_history,
        rate_limit    = rl_data,
        watchlist     = watchlist,
        sync_summary  = sync_summary,
    )


def stats_to_dict(stats: FullStats) -> dict:
    """Convert FullStats to a JSON-serialisable dict."""
    return {
        "generated_at":  stats.generated_at,
        "queue":         asdict(stats.queue),
        "top_languages": [asdict(l) for l in stats.top_languages],
        "recent_success":[asdict(r) for r in stats.recent_success],
        "job_history":   [
            {**asdict(j), "duration": j.duration}
            for j in stats.job_history
        ],
        "rate_limit":    stats.rate_limit,
        "watchlist":     stats.watchlist,
        "sync_summary":  stats.sync_summary,
    }


def stats_to_json(stats: FullStats, indent: int = 2) -> str:
    return json.dumps(stats_to_dict(stats), indent=indent, default=str)


def format_stats_text(stats: FullStats) -> str:
    """Return a human-readable multi-line text summary."""
    lines: List[str] = []
    sep = "─" * 60

    lines.append(f"gh-autofork statistics  [{stats.generated_at}]")
    lines.append(sep)

    # Queue
    q = stats.queue
    lines.append("FORK QUEUE")
    lines.append(f"  Total:    {q.total:,}")
    lines.append(f"  Pending:  {q.pending:,}")
    lines.append(f"  Running:  {q.running:,}")
    lines.append(f"  Success:  {q.success:,}  ({q.success_rate_pct:.1f}%)")
    lines.append(f"  Failed:   {q.failed:,}")
    lines.append(f"  Skipped:  {q.skipped:,}")
    lines.append("")

    # Rate limit
    if stats.rate_limit:
        rl = stats.rate_limit
        pct_used = (rl.get("used", 0) / rl.get("limit", 1)) * 100 if rl.get("limit") else 0
        lines.append("GITHUB RATE LIMIT")
        lines.append(f"  Remaining: {rl['remaining']:,} / {rl['limit']:,}  ({pct_used:.0f}% used)")
        lines.append(f"  Resets at: {rl['reset_at']}")
        lines.append("")

    # Top languages
    if stats.top_languages:
        lines.append("TOP LANGUAGES (forked)")
        for tl in stats.top_languages[:5]:
            lines.append(f"  {tl.language:<20} {tl.count:,}")
        lines.append("")

    # Sync
    if stats.sync_summary:
        lines.append("SYNC HISTORY")
        for status, count in stats.sync_summary.items():
            lines.append(f"  {status:<15} {count:,}")
        lines.append("")

    # Recent
    if stats.recent_success:
        lines.append("RECENTLY FORKED")
        for rf in stats.recent_success[:5]:
            ts = rf.completed_at[:16] if rf.completed_at else ""
            lines.append(f"  [{ts}] {rf.repo} → {rf.fork}")
        lines.append("")

    # Watchlist
    if stats.watchlist:
        lines.append("WATCHLIST")
        for w in stats.watchlist:
            enabled = "✓" if w.get("enabled") else "✗"
            last_run = (w.get("last_run_at") or "never")[:16]
            lines.append(f"  {enabled} {w['kind']:<8} {w['target']:<30} last: {last_run}")
        lines.append("")

    # Jobs
    if stats.job_history:
        lines.append("RECENT JOBS")
        lines.append(f"  {'ID':<5} {'Type':<22} {'Status':<10} {'Done/Total':<12} {'Duration'}")
        lines.append("  " + "─" * 58)
        for j in stats.job_history:
            lines.append(
                f"  {j.id:<5} {j.job_type:<22} {j.status:<10} "
                f"{j.progress}/{j.total:<9} {j.duration}"
            )

    lines.append(sep)
    return "\n".join(lines)
