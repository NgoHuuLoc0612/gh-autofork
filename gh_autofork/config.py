"""
Configuration management for gh-autofork.

Config is loaded from (in priority order, highest first):
  1. Environment variables  (GH_AUTOFORK_*)
  2. ~/.config/gh-autofork/config.yaml
  3. Built-in defaults

All fields are documented with their types and defaults.
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .exceptions import ConfigError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default locations
# ---------------------------------------------------------------------------

_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DEFAULT_CONFIG_DIR = _XDG_CONFIG / "gh-autofork"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.yaml"
DEFAULT_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "gh-autofork"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "autofork.db"
DEFAULT_LOG_FILE = DEFAULT_DATA_DIR / "autofork.log"


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------

@dataclass
class GithubConfig:
    """GitHub API settings."""
    token: str = ""
    api_url: str = "https://api.github.com"
    # if set, forks are created under this org instead of the authenticated user
    default_org: str = ""
    per_page: int = 100
    # seconds to sleep when a secondary rate limit is hit
    abuse_retry_delay: int = 60


@dataclass
class BatchConfig:
    """Batch forking settings."""
    # max concurrent fork requests (each uses a short-lived thread)
    concurrency: int = 4
    # seconds between individual fork API calls (per-worker)
    fork_delay: float = 0.5
    # max attempts per repo before marking as permanently failed
    max_attempts: int = 3
    # exponential backoff base (seconds) for retries
    retry_backoff_base: float = 2.0
    retry_backoff_max: float = 300.0


@dataclass
class FilterConfig:
    """Default filters applied when discovering repos via search/user/org."""
    min_stars: int = 0
    max_stars: int = 0          # 0 = no limit
    min_forks: int = 0
    max_forks: int = 0          # 0 = no limit
    languages: List[str] = field(default_factory=list)
    exclude_languages: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    exclude_topics: List[str] = field(default_factory=list)
    min_size_kb: int = 0
    max_size_kb: int = 0        # 0 = no limit
    exclude_archived: bool = True
    exclude_forks: bool = True
    exclude_private: bool = True
    # repo must have been pushed within this many days (0 = no limit)
    max_days_since_push: int = 0
    # keyword regex matched against description (empty = no filter)
    description_pattern: str = ""
    # keyword regex matched against repo name (empty = no filter)
    name_pattern: str = ""


@dataclass
class SyncConfig:
    """Fork-sync settings."""
    enabled: bool = True
    # strategy: 'merge' | 'rebase' (GitHub API supports merge via sync endpoint)
    strategy: str = "merge"
    # only sync if fork is more than this many commits behind
    min_commits_behind: int = 1


@dataclass
class SchedulerConfig:
    """Background scheduler settings."""
    # cron expression for auto-fork jobs (empty = disabled)
    fork_cron: str = "0 3 * * *"      # 3 AM daily
    # cron expression for sync jobs (empty = disabled)
    sync_cron: str = "0 4 * * *"      # 4 AM daily
    # whether the scheduler runs as a background daemon or in the foreground
    daemon: bool = True


@dataclass
class LogConfig:
    """Logging settings."""
    level: str = "INFO"
    file: str = str(DEFAULT_LOG_FILE)
    max_bytes: int = 10 * 1024 * 1024   # 10 MB
    backup_count: int = 5
    # also echo to stderr
    stderr: bool = True


@dataclass
class AppConfig:
    """Root configuration object."""
    github: GithubConfig = field(default_factory=GithubConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    logging: LogConfig = field(default_factory=LogConfig)

    db_path: str = str(DEFAULT_DB_PATH)
    config_dir: str = str(DEFAULT_CONFIG_DIR)

    # repos to auto-fork on startup (list of "owner/repo" strings)
    startup_repos: List[str] = field(default_factory=list)

    # users/orgs whose public repos should be auto-forked
    watch_users: List[str] = field(default_factory=list)
    watch_orgs: List[str] = field(default_factory=list)

    # saved search queries that are executed on each scheduler run
    saved_searches: List[str] = field(default_factory=list)

    # notifications
    webhook_url: str = ""

    # dry-run by default (set to False to actually fork)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (non-destructive copy)."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _env_overrides() -> dict:
    """
    Scan environment for GH_AUTOFORK_* variables and return a nested dict.

    Naming convention:  GH_AUTOFORK_SECTION__KEY=value
    Example:            GH_AUTOFORK_GITHUB__TOKEN=ghp_xxx
                        GH_AUTOFORK_BATCH__CONCURRENCY=8
                        GH_AUTOFORK_DRY_RUN=true
    """
    out: dict = {}
    prefix = "GH_AUTOFORK_"
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        stripped = key[len(prefix):]
        parts = stripped.lower().split("__", maxsplit=1)

        # type coercion
        val: Any = raw
        if raw.lower() in ("true", "1", "yes"):
            val = True
        elif raw.lower() in ("false", "0", "no"):
            val = False
        else:
            try:
                val = int(raw)
            except ValueError:
                try:
                    val = float(raw)
                except ValueError:
                    pass

        if len(parts) == 2:
            section, sub_key = parts
            out.setdefault(section, {})[sub_key] = val
        else:
            out[parts[0]] = val

    return out


def _dict_to_config(data: dict) -> AppConfig:
    """Hydrate an AppConfig from a nested plain dict."""

    def _pick(d: dict, cls):
        """Return only the keys that exist as fields on *cls*."""
        import dataclasses
        names = {f.name for f in dataclasses.fields(cls)}
        return {k: v for k, v in d.items() if k in names}

    cfg = AppConfig()

    if "github" in data:
        cfg.github = GithubConfig(**_pick(data["github"], GithubConfig))
    if "batch" in data:
        cfg.batch = BatchConfig(**_pick(data["batch"], BatchConfig))
    if "filters" in data:
        d = _pick(data["filters"], FilterConfig)
        # ensure list fields are lists
        for lf in ("languages", "exclude_languages", "topics", "exclude_topics"):
            if lf in d and isinstance(d[lf], str):
                d[lf] = [x.strip() for x in d[lf].split(",") if x.strip()]
        cfg.filters = FilterConfig(**d)
    if "sync" in data:
        cfg.sync = SyncConfig(**_pick(data["sync"], SyncConfig))
    if "scheduler" in data:
        cfg.scheduler = SchedulerConfig(**_pick(data["scheduler"], SchedulerConfig))
    if "logging" in data:
        cfg.logging = LogConfig(**_pick(data["logging"], LogConfig))

    # top-level scalar / list fields
    for f in ("db_path", "config_dir", "webhook_url", "dry_run"):
        if f in data:
            setattr(cfg, f, data[f])
    for f in ("startup_repos", "watch_users", "watch_orgs", "saved_searches"):
        if f in data:
            val = data[f]
            if isinstance(val, str):
                val = [x.strip() for x in val.splitlines() if x.strip()]
            setattr(cfg, f, val)

    return cfg


def load_config(config_file: Optional[Path] = None) -> AppConfig:
    """
    Load and validate the application configuration.

    :param config_file: explicit path; if None the default location is used.
    :returns: Fully populated AppConfig.
    :raises ConfigError: if the file exists but is malformed.
    """
    path = Path(config_file) if config_file else DEFAULT_CONFIG_FILE

    raw: dict = {}

    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Cannot parse config file '{path}': {exc}", cause=exc)
        except OSError as exc:
            raise ConfigError(f"Cannot read config file '{path}': {exc}", cause=exc)
    else:
        log.debug("Config file '%s' not found; using defaults + env vars.", path)

    # merge env overrides on top
    env = _env_overrides()
    merged = _deep_merge(raw, env)

    cfg = _dict_to_config(merged)

    # resolve token from GITHUB_TOKEN / GH_TOKEN as fallback
    if not cfg.github.token:
        cfg.github.token = (
            os.environ.get("GH_AUTOFORK_GITHUB__TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or ""
        )

    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    """Raise ConfigError on invalid values."""
    if not cfg.github.token:
        raise ConfigError(
            "GitHub token is required. Set it via:\n"
            "  • config file: github.token\n"
            "  • env var:     GH_AUTOFORK_GITHUB__TOKEN or GITHUB_TOKEN"
        )
    if cfg.batch.concurrency < 1:
        raise ConfigError("batch.concurrency must be >= 1")
    if cfg.batch.max_attempts < 1:
        raise ConfigError("batch.max_attempts must be >= 1")
    if cfg.sync.strategy not in ("merge", "rebase"):
        raise ConfigError("sync.strategy must be 'merge' or 'rebase'")
    if cfg.logging.level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"logging.level '{cfg.logging.level}' is not a valid level")
    if cfg.filters.description_pattern:
        try:
            re.compile(cfg.filters.description_pattern)
        except re.error as exc:
            raise ConfigError(f"filters.description_pattern is not valid regex: {exc}", cause=exc)
    if cfg.filters.name_pattern:
        try:
            re.compile(cfg.filters.name_pattern)
        except re.error as exc:
            raise ConfigError(f"filters.name_pattern is not valid regex: {exc}", cause=exc)


def save_config(cfg: AppConfig, config_file: Optional[Path] = None) -> Path:
    """Serialize *cfg* back to YAML and write to disk."""
    path = Path(config_file) if config_file else DEFAULT_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    # convert to plain dict
    d = asdict(cfg)

    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(d, fh, default_flow_style=False, allow_unicode=True, sort_keys=True)

    return path


def init_config_dir(config_dir: Optional[Path] = None) -> None:
    """Create the config / data directories if they don't exist."""
    cfg_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    cfg_dir.mkdir(parents=True, exist_ok=True)
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
