"""
Composable filter chain for repository selection.

A Filter is a callable that receives a RepoInfo and returns True (keep) or False (discard).
Filters are composed into a FilterChain which short-circuits on the first rejection.

Usage::

    chain = FilterChain.from_config(cfg.filters)
    repos = [r for r in client.list_user_repos("torvalds") if chain(r)]
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Callable, List, Optional

from .config import FilterConfig
from .exceptions import FilterError
from .github_client import RepoInfo

log = logging.getLogger(__name__)

FilterFn = Callable[[RepoInfo], bool]


# ---------------------------------------------------------------------------
# Individual filter factories
# ---------------------------------------------------------------------------

def min_stars(n: int) -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return r.stars >= n
    _filter.__name__ = f"min_stars({n})"
    return _filter


def max_stars(n: int) -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return r.stars <= n
    _filter.__name__ = f"max_stars({n})"
    return _filter


def min_forks(n: int) -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return r.forks_count >= n
    _filter.__name__ = f"min_forks({n})"
    return _filter


def max_forks(n: int) -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return r.forks_count <= n
    _filter.__name__ = f"max_forks({n})"
    return _filter


def language_allowlist(langs: List[str]) -> FilterFn:
    lower = [l.lower() for l in langs]
    def _filter(r: RepoInfo) -> bool:
        return bool(r.language) and r.language.lower() in lower
    _filter.__name__ = f"language_in({langs})"
    return _filter


def language_blocklist(langs: List[str]) -> FilterFn:
    lower = [l.lower() for l in langs]
    def _filter(r: RepoInfo) -> bool:
        return not r.language or r.language.lower() not in lower
    _filter.__name__ = f"language_not_in({langs})"
    return _filter


def topics_any(required: List[str]) -> FilterFn:
    """Repo must have at least one of the specified topics."""
    lower = [t.lower() for t in required]
    def _filter(r: RepoInfo) -> bool:
        repo_topics = [t.lower() for t in (r.topics or [])]
        return any(t in repo_topics for t in lower)
    _filter.__name__ = f"topics_any({required})"
    return _filter


def topics_none(excluded: List[str]) -> FilterFn:
    """Repo must have none of the specified topics."""
    lower = [t.lower() for t in excluded]
    def _filter(r: RepoInfo) -> bool:
        repo_topics = [t.lower() for t in (r.topics or [])]
        return not any(t in repo_topics for t in lower)
    _filter.__name__ = f"topics_none({excluded})"
    return _filter


def min_size_kb(kb: int) -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return r.size_kb >= kb
    _filter.__name__ = f"min_size_kb({kb})"
    return _filter


def max_size_kb(kb: int) -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return r.size_kb <= kb
    _filter.__name__ = f"max_size_kb({kb})"
    return _filter


def exclude_archived() -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return not r.is_archived
    _filter.__name__ = "exclude_archived"
    return _filter


def exclude_forks() -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return not r.is_fork
    _filter.__name__ = "exclude_forks"
    return _filter


def exclude_private() -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return not r.is_private
    _filter.__name__ = "exclude_private"
    return _filter


def max_days_since_push(days: int) -> FilterFn:
    """Only include repos pushed to within the last *days* days."""
    def _filter(r: RepoInfo) -> bool:
        if not r.pushed_at:
            return True   # unknown, don't exclude
        try:
            pushed = datetime.fromisoformat(r.pushed_at.replace("Z", "+00:00"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            return pushed >= cutoff
        except ValueError:
            return True
    _filter.__name__ = f"max_days_since_push({days})"
    return _filter


def description_pattern(pattern: str) -> FilterFn:
    """Include repos whose description matches *pattern* (regex)."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise FilterError(f"Invalid description_pattern regex '{pattern}': {exc}", cause=exc)
    def _filter(r: RepoInfo) -> bool:
        return bool(rx.search(r.description or ""))
    _filter.__name__ = f"description_pattern({pattern!r})"
    return _filter


def name_pattern(pattern: str) -> FilterFn:
    """Include repos whose name matches *pattern* (regex)."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise FilterError(f"Invalid name_pattern regex '{pattern}': {exc}", cause=exc)
    def _filter(r: RepoInfo) -> bool:
        return bool(rx.search(r.name or ""))
    _filter.__name__ = f"name_pattern({pattern!r})"
    return _filter


def not_own_repo(login: str) -> FilterFn:
    """Exclude repos owned by the authenticated user (avoid forking own repos)."""
    lower = login.lower()
    def _filter(r: RepoInfo) -> bool:
        return r.owner.lower() != lower
    _filter.__name__ = f"not_own_repo({login})"
    return _filter


def has_license() -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return bool(r.license)
    _filter.__name__ = "has_license"
    return _filter


def license_allowlist(licenses: List[str]) -> FilterFn:
    upper = [l.upper() for l in licenses]
    def _filter(r: RepoInfo) -> bool:
        return (r.license or "").upper() in upper
    _filter.__name__ = f"license_in({licenses})"
    return _filter


def license_blocklist(licenses: List[str]) -> FilterFn:
    upper = [l.upper() for l in licenses]
    def _filter(r: RepoInfo) -> bool:
        return (r.license or "").upper() not in upper
    _filter.__name__ = f"license_not_in({licenses})"
    return _filter


def has_topics() -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return bool(r.topics)
    _filter.__name__ = "has_topics"
    return _filter


def min_open_issues(n: int) -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return r.open_issues >= n
    _filter.__name__ = f"min_open_issues({n})"
    return _filter


def max_open_issues(n: int) -> FilterFn:
    def _filter(r: RepoInfo) -> bool:
        return r.open_issues <= n
    _filter.__name__ = f"max_open_issues({n})"
    return _filter


def created_after(iso_date: str) -> FilterFn:
    """Only include repos created after *iso_date* (YYYY-MM-DD or full ISO 8601)."""
    try:
        cutoff = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise FilterError(f"Invalid date '{iso_date}': {exc}", cause=exc)
    def _filter(r: RepoInfo) -> bool:
        if not r.created_at:
            return True
        try:
            dt = datetime.fromisoformat(r.created_at.replace("Z", "+00:00"))
            return dt >= cutoff
        except ValueError:
            return True
    _filter.__name__ = f"created_after({iso_date})"
    return _filter


def created_before(iso_date: str) -> FilterFn:
    try:
        cutoff = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise FilterError(f"Invalid date '{iso_date}': {exc}", cause=exc)
    def _filter(r: RepoInfo) -> bool:
        if not r.created_at:
            return True
        try:
            dt = datetime.fromisoformat(r.created_at.replace("Z", "+00:00"))
            return dt <= cutoff
        except ValueError:
            return True
    _filter.__name__ = f"created_before({iso_date})"
    return _filter


# ---------------------------------------------------------------------------
# Filter chain
# ---------------------------------------------------------------------------

class FilterChain:
    """
    Ordered chain of filter functions.

    A repository passes if *all* filters return True (AND semantics).
    Call ``add()`` to append filters; use ``from_config()`` for convenience.
    """

    def __init__(self, filters: Optional[List[FilterFn]] = None):
        self._filters: List[FilterFn] = list(filters or [])

    def add(self, fn: FilterFn) -> "FilterChain":
        self._filters.append(fn)
        return self

    def add_if(self, condition: bool, fn: FilterFn) -> "FilterChain":
        if condition:
            self._filters.append(fn)
        return self

    def __call__(self, repo: RepoInfo) -> bool:
        for fn in self._filters:
            if not fn(repo):
                log.debug("Repo '%s' excluded by filter '%s'", repo.full_name, fn.__name__)
                return False
        return True

    def explain(self, repo: RepoInfo) -> List[str]:
        """Return a list of filter names that *reject* the repo."""
        rejections = []
        for fn in self._filters:
            if not fn(repo):
                rejections.append(fn.__name__)
        return rejections

    def __len__(self) -> int:
        return len(self._filters)

    def __repr__(self) -> str:
        return f"<FilterChain [{', '.join(f.__name__ for f in self._filters)}]>"

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: FilterConfig, auth_login: str = "") -> "FilterChain":
        """Build a FilterChain from a FilterConfig."""
        chain = cls()

        if cfg.exclude_archived:
            chain.add(exclude_archived())
        if cfg.exclude_forks:
            chain.add(exclude_forks())
        if cfg.exclude_private:
            chain.add(exclude_private())
        if auth_login:
            chain.add(not_own_repo(auth_login))
        if cfg.min_stars > 0:
            chain.add(min_stars(cfg.min_stars))
        if cfg.max_stars > 0:
            chain.add(max_stars(cfg.max_stars))
        if cfg.min_forks > 0:
            chain.add(min_forks(cfg.min_forks))
        if cfg.max_forks > 0:
            chain.add(max_forks(cfg.max_forks))
        if cfg.languages:
            chain.add(language_allowlist(cfg.languages))
        if cfg.exclude_languages:
            chain.add(language_blocklist(cfg.exclude_languages))
        if cfg.topics:
            chain.add(topics_any(cfg.topics))
        if cfg.exclude_topics:
            chain.add(topics_none(cfg.exclude_topics))
        if cfg.min_size_kb > 0:
            chain.add(min_size_kb(cfg.min_size_kb))
        if cfg.max_size_kb > 0:
            chain.add(max_size_kb(cfg.max_size_kb))
        if cfg.max_days_since_push > 0:
            chain.add(max_days_since_push(cfg.max_days_since_push))
        if cfg.description_pattern:
            chain.add(description_pattern(cfg.description_pattern))
        if cfg.name_pattern:
            chain.add(name_pattern(cfg.name_pattern))

        return chain

    @classmethod
    def from_expression(cls, expr: str) -> "FilterChain":
        """
        Parse a simple DSL expression into a FilterChain.

        Syntax (one filter per line):
            stars >= 100
            language python
            not language java
            topics machine-learning
            no archived
            min_size 50
            max_size 5000

        This is intentionally minimal — for complex needs use from_config().
        """
        chain = cls()
        for line in expr.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            cmd = parts[0].lower()

            if cmd == "stars" and len(parts) >= 3:
                op, n = parts[1], int(parts[2])
                if op == ">=":
                    chain.add(min_stars(n))
                elif op == "<=":
                    chain.add(max_stars(n))
                elif op == ">":
                    chain.add(min_stars(n + 1))
                elif op == "<":
                    chain.add(max_stars(n - 1))
            elif cmd == "forks" and len(parts) >= 3:
                op, n = parts[1], int(parts[2])
                if op in (">=", ">"):
                    chain.add(min_forks(n))
                elif op in ("<=", "<"):
                    chain.add(max_forks(n))
            elif cmd == "language" and len(parts) >= 2:
                chain.add(language_allowlist(parts[1:]))
            elif cmd in ("not", "no") and len(parts) >= 3:
                what = parts[1].lower()
                val  = parts[2]
                if what == "language":
                    chain.add(language_blocklist([val]))
                elif what == "topic":
                    chain.add(topics_none([val]))
                elif what == "archived":
                    chain.add(exclude_archived())
            elif cmd == "topics" and len(parts) >= 2:
                chain.add(topics_any(parts[1:]))
            elif cmd == "no" and len(parts) == 2 and parts[1] == "archived":
                chain.add(exclude_archived())
            elif cmd == "no" and len(parts) == 2 and parts[1] == "forks":
                chain.add(exclude_forks())
            elif cmd == "min_size" and len(parts) == 2:
                chain.add(min_size_kb(int(parts[1])))
            elif cmd == "max_size" and len(parts) == 2:
                chain.add(max_size_kb(int(parts[1])))
            elif cmd == "pushed_within" and len(parts) == 2:
                chain.add(max_days_since_push(int(parts[1])))
            else:
                log.warning("Unrecognised filter expression line: %r", line)

        return chain
