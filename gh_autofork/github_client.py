"""
GitHub REST API v3 client for gh-autofork.

Anti-ban & security features:
  - Per-resource rate-limit buckets (core vs search)
  - Progressive slowdown before hitting quota wall
  - Full-jitter exponential backoff
  - Mandatory inter-fork delay with random jitter
  - Rotating User-Agent strings
  - ETag conditional GETs
  - Explicit abuse/spam detection with clear error messages
  - No infinite retry loops - fails fast with actionable errors
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import AppConfig, GithubConfig
from .exceptions import (
    AuthenticationError,
    ForkError,
    RateLimitError,
    RepoNotFoundError,
)

log = logging.getLogger(__name__)

_MAX_RETRIES      = 5
_BACKOFF_BASE     = 2.0
_BACKOFF_MAX      = 120.0
_SEARCH_MIN_DELAY = 1.2
_CORE_MIN_DELAY   = 0.05
_FORK_MIN_DELAY   = 2.0
_RATE_LIMIT_BUFFER = 10

_USER_AGENTS = [
    "gh-autofork/1.0 (compatible; +https://github.com/yourorg/gh-autofork)",
    "gh-autofork/1.0 Python/3.11 requests/2.31",
    "gh-autofork/1.0 Python/3.12 requests/2.32",
    "gh-autofork/1.0 Python/3.10 requests/2.30",
]


def _full_jitter(cap: float, base: float, attempt: int) -> float:
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def _parse_link_header(header: str) -> Dict[str, str]:
    links: Dict[str, str] = {}
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        url_part, *attrs = part.split(";")
        url = url_part.strip().strip("<>")
        for attr in attrs:
            key, _, val = attr.strip().partition("=")
            if key.strip() == "rel":
                links[val.strip().strip('"')] = url
    return links


class _RateLimitBucket:
    def __init__(self, resource: str, min_delay: float = _CORE_MIN_DELAY):
        self.resource   = resource
        self.remaining  = 5000
        self.limit      = 5000
        self.reset_at   = 0
        self.min_delay  = min_delay
        self._last_call = 0.0
        self._lock      = threading.Lock()

    def update(self, remaining: int, limit: int, reset_at: int) -> None:
        with self._lock:
            self.remaining = remaining
            self.limit     = limit
            self.reset_at  = reset_at

    def wait_if_needed(self) -> None:
        with self._lock:
            now = time.time()
            if self.remaining <= _RATE_LIMIT_BUFFER:
                wait = max(1, self.reset_at - now) + 2
                log.warning("[%s] Rate limit low (%d). Sleeping %.0fs.", self.resource, self.remaining, wait)
                time.sleep(wait)
                self._last_call = time.time()
                return
            elapsed = now - self._last_call
            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed)
            used_ratio = 1.0 - (self.remaining / max(self.limit, 1))
            if used_ratio > 0.8:
                time.sleep(self.min_delay * (used_ratio - 0.8) * 5)
            self._last_call = time.time()

    @property
    def seconds_until_reset(self) -> float:
        return max(0.0, self.reset_at - time.time())


class _SessionLocal(threading.local):
    session: Optional[requests.Session] = None


_tls = _SessionLocal()


def _make_session() -> requests.Session:
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=_MAX_RETRIES,
            backoff_factor=1.5,
            status_forcelist={500, 502, 503, 504},
            allowed_methods={"GET", "POST", "PUT", "PATCH", "DELETE"},
            raise_on_status=False,
        )
    )
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _get_session() -> requests.Session:
    if _tls.session is None:
        _tls.session = _make_session()
    return _tls.session


class RepoInfo:
    __slots__ = (
        "id", "full_name", "owner", "name", "description", "stars",
        "forks_count", "language", "topics", "is_private", "is_archived",
        "is_fork", "size_kb", "license", "homepage", "default_branch",
        "open_issues", "clone_url", "ssh_url", "created_at", "updated_at",
        "pushed_at", "fork_parent", "raw",
    )

    def __init__(self, raw: dict):
        self.raw = raw
        self.id: int            = raw.get("id", 0)
        self.full_name: str     = raw.get("full_name", "")
        self.owner: str         = (raw.get("owner") or {}).get("login", "")
        self.name: str          = raw.get("name", "")
        self.description: str   = raw.get("description") or ""
        self.stars: int         = raw.get("stargazers_count", 0)
        self.forks_count: int   = raw.get("forks_count", 0)
        self.language: str      = raw.get("language") or ""
        self.topics: List[str]  = raw.get("topics") or []
        self.is_private: bool   = raw.get("private", False)
        self.is_archived: bool  = raw.get("archived", False)
        self.is_fork: bool      = raw.get("fork", False)
        self.size_kb: int       = raw.get("size", 0)
        lic = raw.get("license") or {}
        self.license: str       = lic.get("spdx_id") or lic.get("name") or ""
        self.homepage: str      = raw.get("homepage") or ""
        self.default_branch: str = raw.get("default_branch", "main")
        self.open_issues: int   = raw.get("open_issues_count", 0)
        self.clone_url: str     = raw.get("clone_url", "")
        self.ssh_url: str       = raw.get("ssh_url", "")
        self.created_at: str    = raw.get("created_at", "")
        self.updated_at: str    = raw.get("updated_at", "")
        self.pushed_at: str     = raw.get("pushed_at", "")
        parent = raw.get("parent") or {}
        self.fork_parent: str   = parent.get("full_name", "")

    def to_db_dict(self) -> dict:
        import json
        return {
            "github_id":      self.id,
            "full_name":      self.full_name,
            "owner":          self.owner,
            "name":           self.name,
            "description":    self.description,
            "stars":          self.stars,
            "forks_count":    self.forks_count,
            "language":       self.language or None,
            "topics":         json.dumps(self.topics),
            "is_private":     int(self.is_private),
            "is_archived":    int(self.is_archived),
            "is_fork":        int(self.is_fork),
            "size_kb":        self.size_kb,
            "license":        self.license or None,
            "homepage":       self.homepage or None,
            "default_branch": self.default_branch,
            "open_issues":    self.open_issues,
            "clone_url":      self.clone_url or None,
            "ssh_url":        self.ssh_url or None,
            "created_at":     self.created_at or None,
            "updated_at":     self.updated_at or None,
            "pushed_at":      self.pushed_at or None,
            "fetched_at":     datetime.now(timezone.utc).isoformat(),
        }

    def __repr__(self) -> str:
        return f"<RepoInfo {self.full_name} ★{self.stars}>"


class RateLimitInfo:
    def __init__(self, raw: dict):
        core = (raw.get("resources") or {}).get("core") or raw
        self.limit:     int = core.get("limit", 0)
        self.remaining: int = core.get("remaining", 0)
        self.reset:     int = core.get("reset", 0)
        self.used:      int = core.get("used", 0)

    @property
    def reset_dt(self) -> datetime:
        return datetime.fromtimestamp(self.reset, tz=timezone.utc)

    @property
    def seconds_until_reset(self) -> float:
        return max(0.0, self.reset - time.time())

    def __repr__(self) -> str:
        return f"<RateLimitInfo remaining={self.remaining}/{self.limit} resets_in={int(self.seconds_until_reset)}s>"


class GitHubClient:
    """Rate-limit-aware, anti-ban GitHub REST API v3 client."""

    def __init__(self, cfg: AppConfig):
        self._cfg: GithubConfig = cfg.github
        self._base_url = self._cfg.api_url.rstrip("/")

        self._buckets: Dict[str, _RateLimitBucket] = {
            "core":    _RateLimitBucket("core",   min_delay=_CORE_MIN_DELAY),
            "search":  _RateLimitBucket("search", min_delay=_SEARCH_MIN_DELAY),
            "graphql": _RateLimitBucket("graphql",min_delay=_CORE_MIN_DELAY),
        }

        self._semaphore = threading.Semaphore(cfg.batch.concurrency)
        self._ua_index  = 0
        self._ua_lock   = threading.Lock()

        self._base_headers = {
            "Authorization":        f"Bearer {self._cfg.token}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        self._etags: Dict[str, str] = {}
        self._etag_lock = threading.Lock()

        self._auth_user: Optional[str] = None
        self._auth_user_lock = threading.Lock()
        self._rate_limit: Optional[RateLimitInfo] = None
        self._rate_lock = threading.Lock()

        self._last_fork_time = 0.0
        self._fork_lock = threading.Lock()

    def _next_user_agent(self) -> str:
        with self._ua_lock:
            ua = _USER_AGENTS[self._ua_index % len(_USER_AGENTS)]
            self._ua_index += 1
        return ua

    def _resource_for(self, url: str) -> str:
        if "/search/" in url:
            return "search"
        if "/graphql" in url:
            return "graphql"
        return "core"

    def _update_rate_limits(self, resp: requests.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        limit     = resp.headers.get("X-RateLimit-Limit")
        reset     = resp.headers.get("X-RateLimit-Reset")
        resource  = resp.headers.get("X-RateLimit-Resource", "core")
        if remaining is not None and reset is not None:
            bucket = self._buckets.get(resource) or self._buckets["core"]
            bucket.update(int(remaining), int(limit or bucket.limit), int(reset))
        # also keep legacy _rate_limit for compat
        if remaining is not None and reset is not None:
            with self._rate_lock:
                self._rate_limit = RateLimitInfo({
                    "remaining": int(remaining),
                    "limit":     int(limit or 0),
                    "reset":     int(reset),
                })

    def _get_etag(self, url: str) -> Optional[str]:
        with self._etag_lock:
            return self._etags.get(url)

    def _set_etag(self, url: str, etag: str) -> None:
        with self._etag_lock:
            self._etags[url] = etag

    def _enforce_fork_delay(self) -> None:
        with self._fork_lock:
            elapsed = time.time() - self._last_fork_time
            if elapsed < _FORK_MIN_DELAY:
                extra = random.uniform(0, 1.0)
                time.sleep(_FORK_MIN_DELAY - elapsed + extra)
            self._last_fork_time = time.time()

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        use_etag: bool = False,
        is_fork: bool = False,
    ) -> requests.Response:
        url      = endpoint if endpoint.startswith("http") else f"{self._base_url}/{endpoint.lstrip('/')}"
        resource = self._resource_for(url)
        bucket   = self._buckets.get(resource, self._buckets["core"])

        if is_fork:
            self._enforce_fork_delay()

        last_resp: Optional[requests.Response] = None

        with self._semaphore:
            for attempt in range(_MAX_RETRIES + 1):
                bucket.wait_if_needed()

                headers = dict(self._base_headers)
                headers["User-Agent"] = self._next_user_agent()

                if use_etag and method.upper() == "GET":
                    etag = self._get_etag(url)
                    if etag:
                        headers["If-None-Match"] = etag

                try:
                    resp = _get_session().request(
                        method, url,
                        headers=headers,
                        params=params,
                        json=json_body,
                        timeout=(15, 45),
                    )
                except requests.RequestException as exc:
                    if attempt < _MAX_RETRIES:
                        delay = _full_jitter(_BACKOFF_MAX, _BACKOFF_BASE, attempt)
                        log.warning("Network error (attempt %d/%d), retry in %.1fs: %s", attempt+1, _MAX_RETRIES, delay, exc)
                        time.sleep(delay)
                        continue
                    raise

                self._update_rate_limits(resp)
                last_resp = resp

                # ── Success ──────────────────────────────────────────────
                if resp.status_code in (200, 201, 202, 204):
                    if use_etag and (etag := resp.headers.get("ETag")):
                        self._set_etag(url, etag)
                    return resp

                if resp.status_code == 304:
                    return resp

                # ── Auth ─────────────────────────────────────────────────
                if resp.status_code == 401:
                    raise AuthenticationError(
                        "GitHub token is invalid or expired. Ensure the token has the 'repo' scope."
                    )

                # ── 403 / 429 ────────────────────────────────────────────
                if resp.status_code in (403, 429):
                    body_lower      = resp.text.lower()
                    retry_after_hdr = resp.headers.get("Retry-After")
                    x_remaining     = resp.headers.get("X-RateLimit-Remaining", "1")

                    # Account flagged as spam — no point retrying
                    if "spammy" in body_lower or "flagged" in body_lower:
                        try:
                            msg = resp.json().get("message", "Account flagged as spammy")
                        except Exception:
                            msg = "Account flagged as spammy"
                        raise ForkError(
                            f"GitHub rejected request: {msg}. "
                            f"Your account has been flagged. Add profile info, star repos, "
                            f"wait a few days, then try again.",
                            repo="",
                        )

                    if retry_after_hdr:
                        wait = int(retry_after_hdr) + random.uniform(1, 5)
                        log.warning("Retry-After=%s. Sleeping %.0fs.", retry_after_hdr, wait)
                        time.sleep(wait)
                        continue

                    if x_remaining == "0" or "rate limit" in body_lower:
                        reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                        wait = max(5, reset_ts - time.time()) + random.uniform(2, 8)
                        log.warning("Rate limit exhausted. Sleeping %.0fs.", wait)
                        time.sleep(wait)
                        continue

                    if "abuse" in body_lower or "secondary" in body_lower:
                        if attempt >= _MAX_RETRIES - 1:
                            try:
                                msg = resp.json().get("message", resp.text[:200])
                            except Exception:
                                msg = resp.text[:200]
                            raise ForkError(
                                f"GitHub abuse detection after {_MAX_RETRIES} retries: {msg}",
                                repo="",
                            )
                        wait = self._cfg.abuse_retry_delay + random.uniform(5, 30)
                        log.warning("Abuse detection. Sleeping %.0fs.", wait)
                        time.sleep(wait)
                        continue

                    # Generic 403
                    return resp

                # ── Not found / Unprocessable ────────────────────────────
                if resp.status_code in (404, 422):
                    return resp

                # ── Server errors ─────────────────────────────────────────
                if resp.status_code >= 500:
                    if attempt < _MAX_RETRIES:
                        delay = _full_jitter(_BACKOFF_MAX, _BACKOFF_BASE, attempt)
                        log.warning("Server error %d, retry in %.1fs.", resp.status_code, delay)
                        time.sleep(delay)
                        continue
                    resp.raise_for_status()

                resp.raise_for_status()

        # Loop exhausted — raise from last response if available
        if last_resp is not None:
            last_resp.raise_for_status()
        raise RuntimeError(f"Request to {url} failed after {_MAX_RETRIES} retries")

    def _paginate(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        key: Optional[str] = None,
    ) -> Generator[dict, None, None]:
        url: Optional[str] = (
            endpoint if endpoint.startswith("http") else f"{self._base_url}/{endpoint.lstrip('/')}"
        )
        _params = {"per_page": self._cfg.per_page, **(params or {})}

        while url:
            resp = self._request("GET", url, params=_params, use_etag=True)
            if resp.status_code == 304:
                return

            if resp.status_code == 422:
                try:
                    err  = resp.json()
                    msg  = err.get("message", "Unprocessable Entity")
                    errs = err.get("errors", [])
                except Exception:
                    msg, errs = "Unprocessable Entity", []
                log.error("GitHub API 422 for '%s': %s %s", url, msg, errs)
                raise ForkError(
                    f"GitHub API rejected request (422): {msg}. "
                    f"Check query syntax or account permissions.",
                    repo="",
                )

            if resp.status_code not in (200, 201, 202):
                log.error("Unexpected status %d: %s", resp.status_code, resp.text[:200])
                resp.raise_for_status()

            _params = None

            data  = resp.json()
            items = data.get(key) if key else data
            if isinstance(items, list):
                yield from items
            elif isinstance(items, dict):
                yield items

            links = _parse_link_header(resp.headers.get("Link", ""))
            url   = links.get("next")
            if url:
                time.sleep(random.uniform(0.2, 0.8))

    def get_authenticated_user(self) -> dict:
        with self._auth_user_lock:
            if self._auth_user is not None:
                return {"login": self._auth_user}
            resp = self._request("GET", "/user")
            data = resp.json()
            self._auth_user = data.get("login", "")
            return data

    @property
    def auth_login(self) -> str:
        return self.get_authenticated_user()["login"]

    def get_rate_limit(self) -> RateLimitInfo:
        resp = self._request("GET", "/rate_limit")
        data = resp.json()
        rl = RateLimitInfo(data)
        resources = (data.get("resources") or {})
        for name, info in resources.items():
            if name in self._buckets:
                self._buckets[name].update(
                    info.get("remaining", 5000),
                    info.get("limit", 5000),
                    info.get("reset", 0),
                )
        return rl

    @property
    def current_rate_limit(self) -> Optional[RateLimitInfo]:
        with self._rate_lock:
            return self._rate_limit

    def get_repo(self, full_name: str) -> Optional["RepoInfo"]:
        resp = self._request("GET", f"/repos/{full_name}", use_etag=True)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return RepoInfo(resp.json())

    def list_user_repos(self, username: str, repo_type: str = "public") -> Generator["RepoInfo", None, None]:
        for raw in self._paginate(f"/users/{username}/repos",
                                   params={"type": repo_type, "sort": "updated", "direction": "desc"}):
            yield RepoInfo(raw)

    def list_org_repos(self, org: str, repo_type: str = "public") -> Generator["RepoInfo", None, None]:
        for raw in self._paginate(f"/orgs/{org}/repos",
                                   params={"type": repo_type, "sort": "updated", "direction": "desc"}):
            yield RepoInfo(raw)

    def list_authenticated_user_repos(self) -> Generator["RepoInfo", None, None]:
        for raw in self._paginate("/user/repos", params={"affiliation": "owner"}):
            yield RepoInfo(raw)

    def search_repos(self, query: str, sort: str = "stars", order: str = "desc") -> Generator["RepoInfo", None, None]:
        params = {"q": query, "sort": sort, "order": order}
        for raw in self._paginate("/search/repositories", params=params, key="items"):
            yield RepoInfo(raw)

    def fork_repo(
        self,
        full_name: str,
        organization: str = "",
        default_branch_only: bool = False,
    ) -> "RepoInfo":
        resp = self._request("GET", f"/repos/{full_name}", use_etag=True)
        if resp.status_code == 404:
            raise RepoNotFoundError(full_name)

        body: Dict[str, Any] = {}
        if organization:
            body["organization"] = organization
        if default_branch_only:
            body["default_branch_only"] = True

        fork_resp = self._request(
            "POST", f"/repos/{full_name}/forks",
            json_body=body,
            is_fork=True,
        )

        if fork_resp.status_code == 404:
            raise RepoNotFoundError(full_name)

        if fork_resp.status_code == 422:
            error_data = fork_resp.json()
            msg = error_data.get("message", "Unprocessable entity")
            existing = self._find_existing_fork(full_name, organization)
            if existing:
                return existing
            raise ForkError(f"Cannot fork '{full_name}': {msg}", repo=full_name)

        if fork_resp.status_code not in (200, 201, 202):
            fork_resp.raise_for_status()

        fork_data      = fork_resp.json()
        fork_full_name = fork_data.get("full_name", "")

        for poll in range(12):
            time.sleep(3 + random.uniform(0, 1))
            confirm = self._request("GET", f"/repos/{fork_full_name}")
            if confirm.status_code == 200:
                return RepoInfo(confirm.json())
            log.debug("Fork '%s' not ready yet (poll %d/12)…", fork_full_name, poll + 1)

        return RepoInfo(fork_data)

    def _find_existing_fork(self, upstream: str, organization: str = "") -> Optional["RepoInfo"]:
        owner     = organization or self.auth_login
        repo_name = upstream.split("/")[-1]
        candidate = f"{owner}/{repo_name}"
        resp = self._request("GET", f"/repos/{candidate}", use_etag=True)
        if resp.status_code == 200:
            data = resp.json()
            parent = (data.get("parent") or {}).get("full_name", "")
            if parent.lower() == upstream.lower() or data.get("fork"):
                return RepoInfo(data)
        return None

    def sync_fork(self, fork_full_name: str) -> Tuple[bool, str]:
        resp = self._request(
            "POST", f"/repos/{fork_full_name}/merge-upstream",
            json_body={"branch": self._get_default_branch(fork_full_name)},
            is_fork=True,
        )
        if resp.status_code == 200:
            merge_type = resp.json().get("merge_type", "")
            if merge_type == "none":
                return False, "Already up to date"
            return True, f"Synced ({merge_type})"
        if resp.status_code == 409:
            return False, "Merge conflict; manual intervention required"
        if resp.status_code == 422:
            return False, "Unprocessable: " + resp.json().get("message", "")
        resp.raise_for_status()
        return False, "Unknown"

    def _get_default_branch(self, full_name: str) -> str:
        resp = self._request("GET", f"/repos/{full_name}", use_etag=True)
        if resp.status_code == 200:
            return resp.json().get("default_branch", "main")
        return "main"

    def compare_commits(self, upstream: str, fork: str) -> Dict[str, Any]:
        fork_owner, _     = fork.split("/", 1)
        upstream_owner, _ = upstream.split("/", 1)
        fork_branch       = self._get_default_branch(fork)
        upstream_branch   = self._get_default_branch(upstream)
        base = f"{upstream_owner}:{upstream_branch}"
        head = f"{fork_owner}:{fork_branch}"
        resp = self._request("GET", f"/repos/{fork}/compare/{base}...{head}", use_etag=True)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "status":      data.get("status", ""),
                "ahead_by":    data.get("ahead_by", 0),
                "behind_by":   data.get("behind_by", 0),
                "base_commit": (data.get("base_commit") or {}).get("sha", ""),
                "head_commit": (data.get("merge_base_commit") or {}).get("sha", ""),
            }
        return {"status": "error", "ahead_by": 0, "behind_by": 0}

    def list_user_forks(self, username: str) -> Generator["RepoInfo", None, None]:
        for repo in self.list_user_repos(username):
            if repo.is_fork:
                yield repo

    def trending_repos(self, language: str = "", since: str = "daily", min_stars: int = 10) -> Generator["RepoInfo", None, None]:
        from datetime import date, timedelta
        cutoff    = {"daily": 1, "weekly": 7, "monthly": 30}.get(since, 7)
        from_date = (date.today() - timedelta(days=cutoff)).isoformat()
        q_parts   = [f"stars:>={min_stars}", f"pushed:>={from_date}"]
        if language:
            q_parts.append(f"language:{language}")
        yield from self.search_repos(" ".join(q_parts), sort="stars", order="desc")

    def notify_webhook(self, url: str, payload: dict) -> bool:
        if not url:
            return False
        try:
            r = requests.post(url, json=payload, timeout=10)
            return r.ok
        except Exception as exc:
            log.warning("Webhook failed: %s", exc)
            return False
