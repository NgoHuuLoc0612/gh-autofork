"""
gh-autofork
===========

Automatic GitHub repository batch-forker with SQLite caching,
advanced filtering, fork synchronisation, and OS startup integration.

Quick start::

    from gh_autofork.config import load_config
    from gh_autofork.database import Database
    from gh_autofork.github_client import GitHubClient
    from gh_autofork.forker import BatchForker

    cfg    = load_config()                    # reads ~/.config/gh-autofork/config.yaml
    db     = Database(cfg.db_path)
    client = GitHubClient(cfg)
    forker = BatchForker(client, db, cfg)

    forker.enqueue("torvalds/linux")
    forker.enqueue_from_search("topic:kubernetes stars:>500", limit=50)
    results = forker.run()

For the CLI::

    $ gh-autofork --help
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("gh-autofork")
except PackageNotFoundError:
    __version__ = "0.0.0"

__author__ = "gh-autofork contributors"
__license__ = "MIT"

from .config import AppConfig, load_config, save_config
from .database import Database
from .exceptions import (
    AlreadyForkedError,
    AuthenticationError,
    ConfigError,
    DatabaseError,
    FilterError,
    ForkError,
    GHAutoForkError,
    RateLimitError,
    RepoNotFoundError,
    SchedulerError,
    ServiceError,
    SyncError,
)
from .filters import FilterChain
from .forker import BatchForker, BatchProgress, ForkResult
from .github_client import GitHubClient, RepoInfo, RateLimitInfo
from .scheduler import Scheduler
from .stats import collect_stats, format_stats_text
from .sync import SyncEngine, SyncResult

__all__ = [
    # Config
    "AppConfig",
    "load_config",
    "save_config",
    # Database
    "Database",
    # Exceptions
    "GHAutoForkError",
    "ConfigError",
    "DatabaseError",
    "AuthenticationError",
    "RateLimitError",
    "ForkError",
    "SyncError",
    "FilterError",
    "ServiceError",
    "SchedulerError",
    "RepoNotFoundError",
    "AlreadyForkedError",
    # Filters
    "FilterChain",
    # Forking
    "BatchForker",
    "BatchProgress",
    "ForkResult",
    # GitHub client
    "GitHubClient",
    "RepoInfo",
    "RateLimitInfo",
    # Scheduler
    "Scheduler",
    # Stats
    "collect_stats",
    "format_stats_text",
    # Sync
    "SyncEngine",
    "SyncResult",
]
