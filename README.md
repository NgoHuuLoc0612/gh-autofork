# gh-autofork

A production-grade Python library and CLI for **automatically forking GitHub repositories in batch**, with SQLite caching, advanced filtering, fork synchronisation, and cross-platform OS startup integration.

---

## Features

| Feature | Details |
|---|---|
| **Batch forking** | Concurrent fork workers, configurable queue, retry with exponential backoff |
| **SQLite caching** | Full repo metadata, fork queue, sync history, job tracking, KV cache |
| **Advanced filters** | Stars, forks, language, topics, size, push date, name/description regex |
| **Fork sync** | Keeps forks up-to-date with upstream via GitHub's merge-upstream API |
| **Auto-discover** | Watch users, orgs, saved search queries on a cron schedule |
| **Daemon / scheduler** | Background process with cron expressions via `croniter` |
| **OS startup service** | systemd (Linux), launchd (macOS), Task Scheduler (Windows) |
| **Dry-run mode** | Simulate everything without touching the GitHub API |
| **Rich CLI** | Progress bars, tables, colour output via `rich` + `click` |
| **Webhooks** | POST notification payload after each batch completes |
| **Rate-limit aware** | Automatic wait on primary + secondary GitHub rate limits |

---

## Installation

```bash
pip install gh-autofork
```

Or from source:

```bash
git clone https://github.com/yourorg/gh-autofork
cd gh-autofork
pip install -e ".[dev]"
```

**Requirements:** Python ≥ 3.10, a GitHub [Personal Access Token](https://github.com/settings/tokens) with the `repo` scope.

---

## Quick Start

### 1. Create a config file

```bash
gh-autofork config init
# → prompts for your GitHub token
# → writes ~/.config/gh-autofork/config.yaml
```

Or set an environment variable:

```bash
export GITHUB_TOKEN=ghp_your_token_here
```

### 2. Fork a repo immediately

```bash
gh-autofork fork torvalds/linux
gh-autofork fork microsoft/vscode facebook/react
```

### 3. Fork from a file

```bash
cat repos.txt
# torvalds/linux
# https://github.com/microsoft/vscode
# git@github.com:antirez/redis.git

gh-autofork batch repos.txt --min-stars 100 --language python
```

### 4. Search and fork

```bash
gh-autofork search "topic:machine-learning stars:>=500" --limit 50
```

### 5. Install the startup daemon

```bash
gh-autofork service install
# Linux:   installs systemd user service
# macOS:   installs launchd user agent
# Windows: creates Task Scheduler task
```

---

## CLI Reference

```
gh-autofork [OPTIONS] COMMAND

Global options:
  -c, --config FILE   Path to config YAML  (default: ~/.config/gh-autofork/config.yaml)
  --dry-run           Simulate; no real API calls
  --debug             Enable debug logging

Commands:
  fork        Fork one or more repos immediately
  batch       Enqueue repos from a file or stdin
  search      Discover and enqueue repos via GitHub search
  watch       Manage the watchlist (users / orgs / saved searches)
  queue       Inspect and manage the fork queue
  run         Process pending/failed forks in the queue
  sync        Sync all forks with their upstreams
  daemon      Start the background scheduler (blocking)
  service     Install / uninstall the OS startup service
  stats       Display statistics
  config      Show or initialise configuration
  db          Database management (vacuum, integrity check, etc.)
  rate-limit  Show current GitHub API rate limit
```

### `fork`

```bash
gh-autofork fork torvalds/linux
gh-autofork fork microsoft/vscode --org my-forks-org
gh-autofork fork --no-run alice/repo   # enqueue only, don't run yet

# from stdin
echo "bob/project" | gh-autofork fork
```

### `batch`

```bash
gh-autofork batch repos.txt
gh-autofork batch repos.txt --min-stars 200 --language python --language go
gh-autofork batch repos.txt --no-archived --no-forks

# from stdin
cat urls.txt | gh-autofork batch
```

### `search`

```bash
gh-autofork search "topic:rust stars:>=1000"
gh-autofork search "language:python pushed:>=2024-01-01" --limit 200
gh-autofork search "topic:cli" --org my-org --no-run
```

### `watch`

```bash
gh-autofork watch add user torvalds
gh-autofork watch add org  microsoft
gh-autofork watch add search "topic:kubernetes stars:>200"
gh-autofork watch list
gh-autofork watch remove user torvalds
```

### `queue`

```bash
gh-autofork queue list
gh-autofork queue list --status failed
gh-autofork queue retry          # reset failed → pending
gh-autofork queue clear --status skipped --yes
```

### `sync`

```bash
gh-autofork sync                 # sync all forks
gh-autofork sync --force         # ignore recent-sync cache
gh-autofork sync --repo me/linux # sync one fork
```

### `stats`

```bash
gh-autofork stats
gh-autofork stats --json | jq .queue
gh-autofork stats --no-api       # skip GitHub API rate-limit call
```

### `service`

```bash
gh-autofork service install
gh-autofork service status
gh-autofork service uninstall
```

---

## Configuration Reference

Copy `config.example.yaml` to `~/.config/gh-autofork/config.yaml` and edit.

All settings can be overridden with environment variables:

```
GH_AUTOFORK_GITHUB__TOKEN=ghp_...
GH_AUTOFORK_BATCH__CONCURRENCY=8
GH_AUTOFORK_DRY_RUN=true
GH_AUTOFORK_FILTERS__MIN_STARS=100
```

Key settings:

| Key | Default | Description |
|---|---|---|
| `github.token` | `""` | GitHub PAT with `repo` scope |
| `github.default_org` | `""` | Fork into this org (blank = your account) |
| `batch.concurrency` | `4` | Parallel fork workers |
| `batch.fork_delay` | `0.5` | Seconds between forks per worker |
| `batch.max_attempts` | `3` | Retry failed forks |
| `filters.min_stars` | `0` | Minimum star count |
| `filters.exclude_archived` | `true` | Skip archived repos |
| `filters.languages` | `[]` | Allowlist (empty = all) |
| `filters.topics` | `[]` | Must match ≥1 topic |
| `filters.max_days_since_push` | `0` | 0 = no limit |
| `filters.description_pattern` | `""` | Regex on description |
| `sync.enabled` | `true` | Enable fork sync |
| `sync.min_commits_behind` | `1` | Sync threshold |
| `scheduler.fork_cron` | `"0 3 * * *"` | Fork schedule (cron) |
| `scheduler.sync_cron` | `"0 4 * * *"` | Sync schedule (cron) |
| `startup_repos` | `[]` | Fork these on daemon start |
| `watch_users` | `[]` | Users to auto-watch |
| `watch_orgs` | `[]` | Orgs to auto-watch |
| `saved_searches` | `[]` | Search queries to run each cycle |
| `webhook_url` | `""` | POST notifications here |
| `dry_run` | `false` | Simulate without API calls |

---

## Python Library API

```python
from gh_autofork import (
    AppConfig, load_config,
    Database,
    GitHubClient,
    BatchForker, BatchProgress, ForkResult,
    FilterChain,
    SyncEngine,
    Scheduler,
    collect_stats,
)

# Load config (reads ~/.config/gh-autofork/config.yaml + env vars)
cfg    = load_config()
db     = Database(cfg.db_path)
client = GitHubClient(cfg)

# Build a filter chain
from gh_autofork.filters import min_stars, exclude_archived, language_allowlist
chain = (FilterChain()
         .add(min_stars(100))
         .add(exclude_archived())
         .add(language_allowlist(["Python", "Go"])))

# Or load from config
chain = FilterChain.from_config(cfg.filters, auth_login=client.auth_login)

# Or parse a DSL expression
chain = FilterChain.from_expression("""
    stars >= 100
    language python
    no archived archived
    pushed_within 365
""")

# Create a forker with callbacks
forker = BatchForker(
    client, db, cfg,
    dry_run=False,
    on_progress=lambda p: print(f"{p.pct:.0f}% ({p.done}/{p.total})"),
    on_result=lambda r: print("✓" if r.success else "✗", r.repo_full_name),
)

# Enqueue repos
forker.enqueue("torvalds/linux")
forker.enqueue_many(["microsoft/vscode", "facebook/react"])
forker.enqueue_from_file("repos.txt", filter_chain=chain)
forker.enqueue_from_user("antirez", filter_chain=chain)
forker.enqueue_from_org("kubernetes", filter_chain=chain)
forker.enqueue_from_search("topic:rust stars:>200", limit=50)

# Run the queue
results: list[ForkResult] = forker.run()
for r in results:
    if r.success:
        print(f"Forked: {r.repo_full_name} → {r.fork_full_name}")

# Sync all forks
engine = SyncEngine(client, db, cfg)
sync_results = engine.run(force=False)

# Stats
from gh_autofork.stats import collect_stats, format_stats_text
stats = collect_stats(db, client)
print(format_stats_text(stats))

# Run the daemon
scheduler = Scheduler(cfg)
scheduler.start()   # blocks; Ctrl-C or SIGTERM to stop
```

### Custom filter functions

```python
from gh_autofork.filters import FilterChain, FilterFn
from gh_autofork.github_client import RepoInfo

def has_ci(repo: RepoInfo) -> bool:
    """Keep repos whose description mentions CI/CD."""
    return "ci" in (repo.description or "").lower()

has_ci.__name__ = "has_ci"

chain = FilterChain([has_ci])
```

---

## Database Schema

The SQLite database (`~/.local/share/gh-autofork/autofork.db`) has these tables:

| Table | Purpose |
|---|---|
| `repos` | Cached GitHub repo metadata |
| `forks` | Fork queue (pending → running → success/failed/skipped) |
| `sync_history` | Record of every sync attempt |
| `jobs` | Batch job tracking with progress |
| `rate_limit_log` | GitHub rate-limit snapshots |
| `kv_cache` | Generic TTL key-value cache |
| `watchlist` | Users / orgs / searches to check periodically |
| `schema_versions` | Migration version tracking |

```bash
# CLI db tools
gh-autofork db path
gh-autofork db check
gh-autofork db vacuum
gh-autofork db purge-cache
```

---

## Startup Service Details

### Linux (systemd)

```bash
gh-autofork service install
systemctl --user status gh-autofork
journalctl --user -u gh-autofork -f
```

The unit file is written to `~/.config/systemd/user/gh-autofork.service`.

### macOS (launchd)

```bash
gh-autofork service install
launchctl list com.gh-autofork.daemon
```

The plist is written to `~/Library/LaunchAgents/com.gh-autofork.daemon.plist`.
Logs go to `~/.local/share/gh-autofork/daemon.stdout.log`.

### Windows (Task Scheduler)

```powershell
gh-autofork service install
schtasks /Query /TN GHAutoFork /FO LIST
```

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# With coverage
pytest --cov=gh_autofork --cov-report=term-missing

# Lint
ruff check gh_autofork/
black gh_autofork/ tests/

# Type check
mypy gh_autofork/
```

---

## License

MIT
