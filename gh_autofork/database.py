"""
SQLite persistence layer for gh-autofork.

All tables are created / migrated on first connection.
The module exposes a Database class that encapsulates every query
the rest of the library needs to make.

Schema version history is stored in the `schema_versions` table;
each migration function is keyed by a monotonically increasing integer.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

from .exceptions import DatabaseError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    """Return rows as dicts keyed by column name."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 3

_MIGRATIONS: Dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_versions (
        version   INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS repos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        github_id       INTEGER,
        full_name       TEXT    UNIQUE NOT NULL,
        owner           TEXT    NOT NULL,
        name            TEXT    NOT NULL,
        description     TEXT,
        stars           INTEGER DEFAULT 0,
        forks_count     INTEGER DEFAULT 0,
        language        TEXT,
        topics          TEXT,           -- JSON array
        is_private      INTEGER DEFAULT 0,
        is_archived     INTEGER DEFAULT 0,
        is_fork         INTEGER DEFAULT 0,
        size_kb         INTEGER DEFAULT 0,
        license         TEXT,
        homepage        TEXT,
        default_branch  TEXT    DEFAULT 'main',
        open_issues     INTEGER DEFAULT 0,
        clone_url       TEXT,
        ssh_url         TEXT,
        created_at      TEXT,
        updated_at      TEXT,
        pushed_at       TEXT,
        fetched_at      TEXT    DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_repos_owner    ON repos(owner);
    CREATE INDEX IF NOT EXISTS idx_repos_language ON repos(language);
    CREATE INDEX IF NOT EXISTS idx_repos_stars    ON repos(stars);

    CREATE TABLE IF NOT EXISTS forks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_id         INTEGER REFERENCES repos(id) ON DELETE CASCADE,
        repo_full_name  TEXT    NOT NULL,
        fork_full_name  TEXT,
        status          TEXT    NOT NULL DEFAULT 'pending',
        -- pending | running | success | failed | skipped | cancelled
        error_message   TEXT,
        attempt_count   INTEGER DEFAULT 0,
        organization    TEXT,
        queued_at       TEXT    DEFAULT (datetime('now')),
        started_at      TEXT,
        completed_at    TEXT,
        last_attempt_at TEXT,
        source          TEXT    DEFAULT 'manual',
        -- manual | batch | search | watch | startup
        metadata        TEXT    DEFAULT '{}'
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_forks_repo_org
        ON forks(repo_full_name, COALESCE(organization, ''));

    CREATE INDEX IF NOT EXISTS idx_forks_status       ON forks(status);
    CREATE INDEX IF NOT EXISTS idx_forks_queued_at    ON forks(queued_at);
    CREATE INDEX IF NOT EXISTS idx_forks_repo_full_name ON forks(repo_full_name);

    CREATE TABLE IF NOT EXISTS sync_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        fork_id         INTEGER REFERENCES forks(id) ON DELETE CASCADE,
        repo_full_name  TEXT    NOT NULL,
        fork_full_name  TEXT,
        upstream_sha    TEXT,
        fork_sha        TEXT,
        status          TEXT    NOT NULL,   -- up_to_date | synced | failed
        commits_behind  INTEGER DEFAULT 0,
        error_message   TEXT,
        synced_at       TEXT    DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_sync_repo ON sync_history(repo_full_name);

    CREATE TABLE IF NOT EXISTS jobs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type        TEXT    NOT NULL,
        status          TEXT    NOT NULL DEFAULT 'pending',
        -- pending | running | success | failed | cancelled
        config_json     TEXT    DEFAULT '{}',
        progress        INTEGER DEFAULT 0,
        total           INTEGER DEFAULT 0,
        success_count   INTEGER DEFAULT 0,
        fail_count      INTEGER DEFAULT 0,
        skip_count      INTEGER DEFAULT 0,
        error_message   TEXT,
        created_at      TEXT    DEFAULT (datetime('now')),
        started_at      TEXT,
        completed_at    TEXT
    );

    CREATE TABLE IF NOT EXISTS rate_limit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        resource    TEXT    NOT NULL DEFAULT 'core',
        remaining   INTEGER NOT NULL,
        limit_total INTEGER NOT NULL,
        reset_at    TEXT,
        logged_at   TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS kv_cache (
        key         TEXT    PRIMARY KEY,
        value       TEXT    NOT NULL,
        expires_at  TEXT,
        updated_at  TEXT    DEFAULT (datetime('now'))
    );
    """,

    2: """
    ALTER TABLE forks ADD COLUMN priority INTEGER DEFAULT 0;
    ALTER TABLE forks ADD COLUMN dry_run   INTEGER DEFAULT 0;
    """,

    3: """
    -- Watchlist table: users/orgs whose repos are periodically checked
    CREATE TABLE IF NOT EXISTS watchlist (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT    NOT NULL,   -- user | org | search
        target      TEXT    NOT NULL,
        enabled     INTEGER DEFAULT 1,
        last_run_at TEXT,
        run_count   INTEGER DEFAULT 0,
        created_at  TEXT    DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_kind_target ON watchlist(kind, target);
    """,
}


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    """
    Thread-safe SQLite wrapper.

    Each thread gets its own connection (via threading.local).
    Write operations are protected by a re-entrant lock so that only one
    thread modifies the database at a time.
    """

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        # Ensure schema is up to date on the calling (main) thread
        self._migrate()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                timeout=30,
                isolation_level=None,   # autocommit; we handle transactions manually
            )
            conn.row_factory = _row_factory
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-32768")   # 32 MB page cache
            self._local.conn = conn
        return self._local.conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._connect()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that wraps statements in a write-locked transaction."""
        with self._write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable) -> sqlite3.Cursor:
        return self.conn.executemany(sql, seq)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> List[dict]:
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Schema migration
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                # Ensure schema_versions exists first (outside any transaction)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_versions (
                        version    INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                """)
                applied = {
                    row["version"]
                    for row in conn.execute("SELECT version FROM schema_versions").fetchall()
                }
                for version in sorted(_MIGRATIONS):
                    if version in applied:
                        continue
                    log.debug("Applying DB migration v%d", version)
                    # executescript() issues COMMIT before running, so we use it directly.
                    # For ALTER TABLE statements that may fail if column already exists,
                    # we split and handle each statement individually.
                    sql = _MIGRATIONS[version]
                    # Strip full-line comments, then split by ";" to get individual statements
                    clean_lines = [
                        line for line in sql.splitlines()
                        if line.strip() and not line.strip().startswith("--")
                    ]
                    clean_sql = "\n".join(clean_lines)
                    statements = [s.strip() for s in clean_sql.split(";") if s.strip()]
                    for stmt in statements:
                        try:
                            conn.execute(stmt)
                        except Exception as stmt_exc:
                            # Allow "duplicate column" errors for ALTER TABLE ADD COLUMN
                            if "duplicate column" in str(stmt_exc).lower():
                                log.debug("Column already exists (skipping): %s", stmt_exc)
                            else:
                                raise
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_versions(version, applied_at) VALUES (?,?)",
                        (version, _now_iso()),
                    )
            except Exception as exc:
                raise DatabaseError(f"Schema migration failed: {exc}", cause=exc) from exc

    # ------------------------------------------------------------------
    # Repo table
    # ------------------------------------------------------------------

    def upsert_repo(self, repo: dict) -> int:
        """Insert or update a repo record. Returns the rowid."""
        sql = """
        INSERT INTO repos (
            github_id, full_name, owner, name, description,
            stars, forks_count, language, topics, is_private,
            is_archived, is_fork, size_kb, license, homepage,
            default_branch, open_issues, clone_url, ssh_url,
            created_at, updated_at, pushed_at, fetched_at
        ) VALUES (
            :github_id, :full_name, :owner, :name, :description,
            :stars, :forks_count, :language, :topics, :is_private,
            :is_archived, :is_fork, :size_kb, :license, :homepage,
            :default_branch, :open_issues, :clone_url, :ssh_url,
            :created_at, :updated_at, :pushed_at, :fetched_at
        )
        ON CONFLICT(full_name) DO UPDATE SET
            github_id       = excluded.github_id,
            description     = excluded.description,
            stars           = excluded.stars,
            forks_count     = excluded.forks_count,
            language        = excluded.language,
            topics          = excluded.topics,
            is_private      = excluded.is_private,
            is_archived     = excluded.is_archived,
            size_kb         = excluded.size_kb,
            license         = excluded.license,
            homepage        = excluded.homepage,
            open_issues     = excluded.open_issues,
            updated_at      = excluded.updated_at,
            pushed_at       = excluded.pushed_at,
            fetched_at      = excluded.fetched_at
        """
        with self.transaction():
            cur = self.conn.execute(sql, repo)
            return cur.lastrowid

    def get_repo(self, full_name: str) -> Optional[dict]:
        return self.fetchone(
            "SELECT * FROM repos WHERE full_name = ?", (full_name,)
        )

    def search_repos(
        self,
        language: Optional[str] = None,
        min_stars: int = 0,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        clauses = ["1=1"]
        params: list = []
        if language:
            clauses.append("language = ?")
            params.append(language)
        if min_stars:
            clauses.append("stars >= ?")
            params.append(min_stars)
        params += [limit, offset]
        return self.fetchall(
            f"SELECT * FROM repos WHERE {' AND '.join(clauses)} "
            f"ORDER BY stars DESC LIMIT ? OFFSET ?",
            tuple(params),
        )

    # ------------------------------------------------------------------
    # Forks table
    # ------------------------------------------------------------------

    def enqueue_fork(
        self,
        repo_full_name: str,
        organization: str = "",
        source: str = "manual",
        priority: int = 0,
        dry_run: bool = False,
        repo_id: Optional[int] = None,
    ) -> Optional[int]:
        """
        Add a repo to the fork queue.  Returns the fork row id, or None if
        there is already a successful fork record for this repo+org.
        """
        org_val = organization or None
        # Check for existing successful fork
        existing = self.fetchone(
            "SELECT id, status FROM forks "
            "WHERE repo_full_name = ? AND COALESCE(organization, '') = ?",
            (repo_full_name, organization),
        )
        if existing:
            if existing["status"] == "success":
                return None          # already done
            # reset failed/skipped entries so they can be retried
            if existing["status"] in ("failed", "skipped"):
                with self.transaction():
                    self.conn.execute(
                        "UPDATE forks SET status='pending', error_message=NULL, "
                        "attempt_count=0, last_attempt_at=NULL WHERE id=?",
                        (existing["id"],),
                    )
            return existing["id"]

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO forks
                    (repo_id, repo_full_name, organization, source, status,
                     priority, dry_run, queued_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    repo_id,
                    repo_full_name,
                    org_val,
                    source,
                    priority,
                    1 if dry_run else 0,
                    _now_iso(),
                ),
            )
            return cur.lastrowid

    def get_pending_forks(self, limit: int = 500) -> List[dict]:
        return self.fetchall(
            """
            SELECT f.*, r.stars, r.language, r.description
            FROM forks f
            LEFT JOIN repos r ON r.full_name = f.repo_full_name
            WHERE f.status = 'pending'
            ORDER BY f.priority DESC, f.queued_at ASC
            LIMIT ?
            """,
            (limit,),
        )

    def get_retryable_forks(self, max_attempts: int = 3, limit: int = 200) -> List[dict]:
        return self.fetchall(
            """
            SELECT * FROM forks
            WHERE status = 'failed' AND attempt_count < ?
            ORDER BY last_attempt_at ASC
            LIMIT ?
            """,
            (max_attempts, limit),
        )

    def mark_fork_running(self, fork_id: int) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE forks SET status='running', started_at=?, "
                "attempt_count=attempt_count+1, last_attempt_at=? WHERE id=?",
                (_now_iso(), _now_iso(), fork_id),
            )

    def mark_fork_success(self, fork_id: int, fork_full_name: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE forks SET status='success', fork_full_name=?, "
                "completed_at=? WHERE id=?",
                (fork_full_name, _now_iso(), fork_id),
            )

    def mark_fork_failed(self, fork_id: int, error: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE forks SET status='failed', error_message=?, "
                "completed_at=? WHERE id=?",
                (error[:1024], _now_iso(), fork_id),
            )

    def mark_fork_skipped(self, fork_id: int, reason: str = "") -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE forks SET status='skipped', error_message=?, "
                "completed_at=? WHERE id=?",
                (reason[:512], _now_iso(), fork_id),
            )

    def get_fork(self, repo_full_name: str, organization: str = "") -> Optional[dict]:
        return self.fetchone(
            "SELECT * FROM forks WHERE repo_full_name=? AND COALESCE(organization,'')=?",
            (repo_full_name, organization),
        )

    def list_forks(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        if status:
            return self.fetchall(
                "SELECT * FROM forks WHERE status=? ORDER BY queued_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        return self.fetchall(
            "SELECT * FROM forks ORDER BY queued_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def count_forks_by_status(self) -> Dict[str, int]:
        rows = self.fetchall(
            "SELECT status, COUNT(*) as cnt FROM forks GROUP BY status"
        )
        return {r["status"]: r["cnt"] for r in rows}

    def delete_fork_record(self, repo_full_name: str, organization: str = "") -> int:
        with self.transaction():
            cur = self.conn.execute(
                "DELETE FROM forks WHERE repo_full_name=? AND COALESCE(organization,'')=?",
                (repo_full_name, organization),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Sync history
    # ------------------------------------------------------------------

    def record_sync(self, record: dict) -> int:
        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO sync_history
                    (fork_id, repo_full_name, fork_full_name, upstream_sha,
                     fork_sha, status, commits_behind, error_message, synced_at)
                VALUES
                    (:fork_id, :repo_full_name, :fork_full_name, :upstream_sha,
                     :fork_sha, :status, :commits_behind, :error_message, :synced_at)
                """,
                record,
            )
            return cur.lastrowid

    def last_sync(self, repo_full_name: str) -> Optional[dict]:
        return self.fetchone(
            "SELECT * FROM sync_history WHERE repo_full_name=? ORDER BY synced_at DESC LIMIT 1",
            (repo_full_name,),
        )

    def sync_history(self, repo_full_name: str, limit: int = 20) -> List[dict]:
        return self.fetchall(
            "SELECT * FROM sync_history WHERE repo_full_name=? ORDER BY synced_at DESC LIMIT ?",
            (repo_full_name, limit),
        )

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def create_job(self, job_type: str, config: dict) -> int:
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO jobs (job_type, config_json, created_at) VALUES (?, ?, ?)",
                (job_type, json.dumps(config), _now_iso()),
            )
            return cur.lastrowid

    def start_job(self, job_id: int, total: int = 0) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE jobs SET status='running', started_at=?, total=? WHERE id=?",
                (_now_iso(), total, job_id),
            )

    def update_job_progress(
        self,
        job_id: int,
        progress: int,
        success: int = 0,
        fail: int = 0,
        skip: int = 0,
    ) -> None:
        self.conn.execute(
            "UPDATE jobs SET progress=?, success_count=?, fail_count=?, skip_count=? WHERE id=?",
            (progress, success, fail, skip, job_id),
        )

    def finish_job(
        self,
        job_id: int,
        success: bool = True,
        error: str = "",
    ) -> None:
        status = "success" if success else "failed"
        with self.transaction():
            self.conn.execute(
                "UPDATE jobs SET status=?, error_message=?, completed_at=? WHERE id=?",
                (status, error or None, _now_iso(), job_id),
            )

    def get_job(self, job_id: int) -> Optional[dict]:
        return self.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))

    def list_jobs(self, limit: int = 20) -> List[dict]:
        return self.fetchall(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------------
    # Rate-limit log
    # ------------------------------------------------------------------

    def log_rate_limit(self, resource: str, remaining: int, limit: int, reset_at: str) -> None:
        self.conn.execute(
            "INSERT INTO rate_limit_log (resource, remaining, limit_total, reset_at) VALUES (?,?,?,?)",
            (resource, remaining, limit, reset_at),
        )

    def latest_rate_limit(self, resource: str = "core") -> Optional[dict]:
        return self.fetchone(
            "SELECT * FROM rate_limit_log WHERE resource=? ORDER BY logged_at DESC LIMIT 1",
            (resource,),
        )

    # ------------------------------------------------------------------
    # KV cache
    # ------------------------------------------------------------------

    def cache_set(self, key: str, value: Any, ttl_seconds: int = 0) -> None:
        expires = None
        if ttl_seconds != 0:
            expires = datetime.utcfromtimestamp(
                datetime.utcnow().timestamp() + ttl_seconds
            ).isoformat()
        serialised = json.dumps(value)
        self.conn.execute(
            "INSERT OR REPLACE INTO kv_cache (key, value, expires_at, updated_at) VALUES (?,?,?,?)",
            (key, serialised, expires, _now_iso()),
        )

    def cache_get(self, key: str) -> Optional[Any]:
        row = self.fetchone("SELECT value, expires_at FROM kv_cache WHERE key=?", (key,))
        if not row:
            return None
        if row["expires_at"]:
            now_iso = datetime.utcnow().isoformat()
            if row["expires_at"] < now_iso:
                self.conn.execute("DELETE FROM kv_cache WHERE key=?", (key,))
                return None
        return json.loads(row["value"])

    def cache_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv_cache WHERE key=?", (key,))

    def cache_purge_expired(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM kv_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
            (_now_iso(),),
        )
        return cur.rowcount

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    def upsert_watch(self, kind: str, target: str, enabled: bool = True) -> int:
        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO watchlist (kind, target, enabled, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, target) DO UPDATE SET enabled=excluded.enabled
                """,
                (kind, target, 1 if enabled else 0, _now_iso()),
            )
            return cur.lastrowid

    def list_watches(self, enabled_only: bool = True) -> List[dict]:
        sql = "SELECT * FROM watchlist"
        if enabled_only:
            sql += " WHERE enabled=1"
        return self.fetchall(sql)

    def mark_watch_ran(self, watch_id: int) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE watchlist SET last_run_at=?, run_count=run_count+1 WHERE id=?",
                (_now_iso(), watch_id),
            )

    def delete_watch(self, kind: str, target: str) -> int:
        with self.transaction():
            cur = self.conn.execute(
                "DELETE FROM watchlist WHERE kind=? AND target=?", (kind, target)
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------

    def fork_stats(self) -> dict:
        counts = self.count_forks_by_status()
        total = sum(counts.values())
        recent = self.fetchall(
            "SELECT * FROM forks WHERE status='success' ORDER BY completed_at DESC LIMIT 5"
        )
        top_langs = self.fetchall(
            """
            SELECT r.language, COUNT(*) as cnt
            FROM forks f
            JOIN repos r ON r.full_name = f.repo_full_name
            WHERE f.status='success' AND r.language IS NOT NULL
            GROUP BY r.language ORDER BY cnt DESC LIMIT 10
            """
        )
        return {
            "counts": counts,
            "total": total,
            "recent_success": recent,
            "top_languages": top_langs,
        }

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def integrity_check(self) -> bool:
        result = self.fetchone("PRAGMA integrity_check")
        return result is not None and result.get("integrity_check") == "ok"
