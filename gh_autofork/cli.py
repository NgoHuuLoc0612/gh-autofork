"""
gh-autofork command-line interface.

Commands
────────
  fork        Fork one or more repositories immediately.
  batch       Enqueue repos from a file or stdin.
  search      Discover and enqueue repos via GitHub search.
  watch       Manage the watchlist (users / orgs / searches).
  queue       Inspect and manage the fork queue.
  run         Process the fork queue (run pending/retryable forks).
  sync        Sync all forks with their upstreams.
  daemon      Start the background scheduler daemon.
  service     Install / uninstall the OS startup service.
  stats       Display statistics.
  config      Show or initialise configuration.
  db          Database management utilities.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich import print as rprint

from .config import AppConfig, init_config_dir, load_config, save_config
from .database import Database
from .exceptions import GHAutoForkError
from .filters import FilterChain
from .forker import BatchForker, BatchProgress, ForkResult
from .github_client import GitHubClient
from .service import install_service, service_status, uninstall_service
from .stats import collect_stats, format_stats_text, stats_to_json
from .sync import SyncEngine
from .utils import ensure_dirs, parse_repo_file, setup_logging

console = Console()
err_console = Console(stderr=True, style="bold red")


# ---------------------------------------------------------------------------
# Global options / group
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config", "-c",
    "config_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Path to config YAML file. Default: ~/.config/gh-autofork/config.yaml",
)
@click.option("--dry-run", is_flag=True, default=False, help="Simulate operations without making API calls.")
@click.option("--debug",   is_flag=True, default=False, help="Enable debug-level logging.")
@click.pass_context
def cli(ctx: click.Context, config_file: Optional[str], dry_run: bool, debug: bool) -> None:
    """gh-autofork – Automatic GitHub repository fork manager."""
    ctx.ensure_object(dict)
    try:
        cfg = load_config(Path(config_file) if config_file else None)
    except GHAutoForkError as exc:
        err_console.print(f"[bold]Config error:[/bold] {exc}")
        sys.exit(1)

    if dry_run:
        cfg.dry_run = True
    if debug:
        cfg.logging.level = "DEBUG"

    setup_logging(cfg)
    ensure_dirs(cfg)

    ctx.obj["cfg"]         = cfg
    ctx.obj["config_file"] = Path(config_file) if config_file else None


def _cfg(ctx: click.Context) -> AppConfig:
    return ctx.obj["cfg"]


def _db(ctx: click.Context) -> Database:
    cfg = _cfg(ctx)
    return Database(cfg.db_path)


def _client(ctx: click.Context) -> GitHubClient:
    return GitHubClient(_cfg(ctx))


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------

@cli.command("fork")
@click.argument("repos", nargs=-1, required=False)
@click.option("-o", "--org", "organization", default="", help="Fork into this GitHub organisation.")
@click.option("-p", "--priority", default=0, type=int, help="Queue priority (higher = processed first).")
@click.option("--no-run", is_flag=True, default=False, help="Enqueue only; do not process the queue.")
@click.pass_context
def cmd_fork(
    ctx: click.Context,
    repos: tuple,
    organization: str,
    priority: int,
    no_run: bool,
) -> None:
    """Fork one or more repositories.

    REPOS can be: owner/name  |  https://github.com/owner/name  |  git@github.com:owner/name.git

    \b
    Examples:
        gh-autofork fork torvalds/linux
        gh-autofork fork microsoft/vscode facebook/react --org myfork-org
    """
    cfg = _cfg(ctx)
    db  = _db(ctx)
    client = _client(ctx)

    if not repos:
        # Read from stdin
        repos = tuple(
            line.strip() for line in sys.stdin if line.strip() and not line.startswith("#")
        )
        if not repos:
            console.print("[yellow]No repos provided.[/yellow]")
            return

    forker = BatchForker(client, db, cfg, dry_run=cfg.dry_run)
    queued = 0
    for repo in repos:
        fid = forker.enqueue(repo.strip(), organization=organization,
                             source="manual", priority=priority)
        if fid is not None:
            console.print(f"  [green]Queued[/green] [bold]{repo}[/bold]")
            queued += 1
        else:
            console.print(f"  [dim]Skipped[/dim] {repo} (already forked)")

    console.print(f"\n[bold]{queued}[/bold] repo(s) added to queue.")

    if not no_run and queued > 0:
        _run_queue(ctx, forker)


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------

@cli.command("batch")
@click.argument("file", type=click.Path(exists=True, dir_okay=False), required=False)
@click.option("-o", "--org", "organization", default="")
@click.option("--min-stars", default=0, type=int)
@click.option("--language", "languages", multiple=True)
@click.option("--no-forks", "exclude_forks", is_flag=True, default=False)
@click.option("--no-archived", "exclude_archived", is_flag=True, default=False)
@click.pass_context
def cmd_batch(
    ctx: click.Context,
    file: Optional[str],
    organization: str,
    languages: tuple,
    min_stars: int,
    exclude_forks: bool,
    exclude_archived: bool,
) -> None:
    """Enqueue repos from a file (one per line) and process the queue.

    If FILE is omitted, repos are read from stdin.

    \b
    Example:
        gh-autofork batch repos.txt --min-stars 100 --language python
    """
    cfg    = _cfg(ctx)
    db     = _db(ctx)
    client = _client(ctx)

    # Build inline filter overrides
    filter_cfg = cfg.filters
    if min_stars:
        filter_cfg.min_stars = min_stars
    if languages:
        filter_cfg.languages = list(languages)
    if exclude_forks:
        filter_cfg.exclude_forks = True
    if exclude_archived:
        filter_cfg.exclude_archived = True

    filter_chain = FilterChain.from_config(filter_cfg, auth_login=client.auth_login)

    forker = BatchForker(client, db, cfg, dry_run=cfg.dry_run)

    if file:
        try:
            result = forker.enqueue_from_file(file, organization=organization, filter_chain=filter_chain)
        except ValueError as exc:
            err_console.print(str(exc))
            sys.exit(1)
    else:
        lines = [l.strip() for l in sys.stdin if l.strip() and not l.startswith("#")]
        result = forker.enqueue_many(lines, organization=organization, source="batch_stdin",
                                     filter_chain=filter_chain)

    queued = sum(1 for v in result.values() if v is not None)
    skipped = len(result) - queued
    console.print(f"[bold]{queued}[/bold] repos queued, [dim]{skipped}[/dim] skipped/already done.")

    if queued > 0:
        _run_queue(ctx, forker)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@cli.command("search")
@click.argument("query")
@click.option("--limit", default=0, type=int, show_default=True, help="Max repos to enqueue (0 = unlimited, GitHub caps at 1000).")
@click.option("-o", "--org", "organization", default="")
@click.option("--min-stars", default=0, type=int)
@click.option("--language", "languages", multiple=True)
@click.option("--no-run", is_flag=True, default=False)
@click.pass_context
def cmd_search(
    ctx: click.Context,
    query: str,
    limit: int,
    organization: str,
    languages: tuple,
    min_stars: int,
    no_run: bool,
) -> None:
    """Search GitHub and enqueue matching repos.

    \b
    Example:
        gh-autofork search "topic:machine-learning stars:>=500"
        gh-autofork search "topic:machine-learning stars:>=500" --limit 100
    """
    cfg    = _cfg(ctx)
    db     = _db(ctx)
    client = _client(ctx)

    filter_cfg = cfg.filters
    if min_stars:
        filter_cfg.min_stars = min_stars
    if languages:
        filter_cfg.languages = list(languages)

    # 0 means unlimited — GitHub search API caps at 1000 results total
    effective_limit = limit if limit > 0 else 1000

    filter_chain = FilterChain.from_config(filter_cfg, auth_login=client.auth_login)
    forker = BatchForker(client, db, cfg, dry_run=cfg.dry_run)

    with console.status(f"[bold]Searching:[/bold] {query} …"):
        try:
            n = forker.enqueue_from_search(query, organization=organization,
                                            limit=effective_limit, filter_chain=filter_chain)
        except Exception as exc:
            err_console.print(f"\n[bold red]Search error:[/bold red] {exc}")
            err_console.print(
                "\n[yellow]Tip:[/yellow] Try a simpler query, e.g.:\n"
                "  gh-autofork search \"stars:>1000 language:python\" --limit 10\n"
                "  gh-autofork search \"tensorflow\" --limit 10"
            )
            return

    console.print(f"[bold]{n}[/bold] repos enqueued from search.")

    if not no_run and n > 0:
        _run_queue(ctx, forker)


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------

@cli.group("watch")
def cmd_watch() -> None:
    """Manage the watchlist (users, orgs, searches)."""


@cmd_watch.command("add")
@click.argument("kind", type=click.Choice(["user", "org", "search"]))
@click.argument("target")
@click.pass_context
def watch_add(ctx: click.Context, kind: str, target: str) -> None:
    """Add a user, org, or search query to the watchlist."""
    db = _db(ctx)
    db.upsert_watch(kind, target, enabled=True)
    console.print(f"[green]Added[/green] {kind} watchlist entry: [bold]{target}[/bold]")


@cmd_watch.command("remove")
@click.argument("kind", type=click.Choice(["user", "org", "search"]))
@click.argument("target")
@click.pass_context
def watch_remove(ctx: click.Context, kind: str, target: str) -> None:
    """Remove a watchlist entry."""
    db = _db(ctx)
    n = db.delete_watch(kind, target)
    if n:
        console.print(f"[red]Removed[/red] {kind} watchlist entry: {target}")
    else:
        console.print(f"[yellow]Not found:[/yellow] {kind} / {target}")


@cmd_watch.command("list")
@click.pass_context
def watch_list(ctx: click.Context) -> None:
    """List all watchlist entries."""
    db = _db(ctx)
    watches = db.list_watches(enabled_only=False)
    if not watches:
        console.print("[dim]Watchlist is empty.[/dim]")
        return
    t = Table(title="Watchlist", show_header=True)
    t.add_column("ID",      style="dim",   width=5)
    t.add_column("Kind",    style="cyan",  width=8)
    t.add_column("Target",  style="bold",  width=40)
    t.add_column("Enabled", width=8)
    t.add_column("Last run", width=20)
    t.add_column("Runs",    width=6)
    for w in watches:
        enabled_str = "[green]yes[/green]" if w["enabled"] else "[red]no[/red]"
        last = (w.get("last_run_at") or "never")[:19]
        t.add_row(str(w["id"]), w["kind"], w["target"], enabled_str, last, str(w.get("run_count", 0)))
    console.print(t)


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

@cli.group("queue")
def cmd_queue() -> None:
    """Inspect and manage the fork queue."""


@cmd_queue.command("list")
@click.option("--status", type=click.Choice(["pending","running","success","failed","skipped"]), default=None)
@click.option("--limit", default=50, type=int)
@click.pass_context
def queue_list(ctx: click.Context, status: Optional[str], limit: int) -> None:
    """List fork queue entries."""
    db = _db(ctx)
    forks = db.list_forks(status=status, limit=limit)
    if not forks:
        console.print("[dim]Queue is empty.[/dim]")
        return

    t = Table(title=f"Fork Queue ({len(forks)} shown)", show_header=True)
    t.add_column("ID",    style="dim",  width=6)
    t.add_column("Repo",  style="bold", width=45)
    t.add_column("Status",width=10)
    t.add_column("Fork",  width=35)
    t.add_column("Queued at", width=20)

    status_colors = {
        "pending":  "yellow",
        "running":  "blue",
        "success":  "green",
        "failed":   "red",
        "skipped":  "dim",
        "cancelled":"dim",
    }
    for f in forks:
        st = f["status"]
        color = status_colors.get(st, "white")
        t.add_row(
            str(f["id"]),
            f["repo_full_name"],
            f"[{color}]{st}[/{color}]",
            f.get("fork_full_name") or "—",
            (f.get("queued_at") or "")[:19],
        )
    console.print(t)


@cmd_queue.command("clear")
@click.option("--status", type=click.Choice(["failed","skipped","pending"]), required=True)
@click.option("--yes", is_flag=True)
@click.pass_context
def queue_clear(ctx: click.Context, status: str, yes: bool) -> None:
    """Delete all queue entries with a given status."""
    if not yes:
        click.confirm(f"Delete all '{status}' fork queue entries?", abort=True)
    db = _db(ctx)
    with db.transaction():
        cur = db.conn.execute("DELETE FROM forks WHERE status=?", (status,))
    console.print(f"[red]Deleted[/red] {cur.rowcount} '{status}' entries.")


@cmd_queue.command("retry")
@click.pass_context
def queue_retry(ctx: click.Context) -> None:
    """Reset all 'failed' entries to 'pending' for retry."""
    db = _db(ctx)
    with db.transaction():
        cur = db.conn.execute(
            "UPDATE forks SET status='pending', error_message=NULL, attempt_count=0 WHERE status='failed'"
        )
    console.print(f"[green]Reset[/green] {cur.rowcount} failed entries to pending.")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

@cli.command("run")
@click.option("--workers", default=None, type=int, help="Override batch.concurrency from config.")
@click.option("--no-retry", is_flag=True, default=False, help="Skip previously failed repos.")
@click.pass_context
def cmd_run(ctx: click.Context, workers: Optional[int], no_retry: bool) -> None:
    """Process all pending (and failed) forks in the queue."""
    cfg    = _cfg(ctx)
    db     = _db(ctx)
    client = _client(ctx)
    forker = BatchForker(client, db, cfg, dry_run=cfg.dry_run)
    _run_queue(ctx, forker, max_workers=workers, include_retries=not no_retry)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

@cli.command("sync")
@click.option("--force", is_flag=True, default=False, help="Sync even if recently synced.")
@click.option("--repo", "single_repo", default=None, help="Sync a single fork (e.g. myuser/linux).")
@click.pass_context
def cmd_sync(ctx: click.Context, force: bool, single_repo: Optional[str]) -> None:
    """Sync all forks (or a single one) with their upstreams."""
    cfg    = _cfg(ctx)
    db     = _db(ctx)
    client = _client(ctx)

    engine = SyncEngine(
        client, db, cfg,
        on_progress=lambda done, total, repo: console.print(
            f"  [{done}/{total}] {repo}", end="\r"
        ),
    )

    if single_repo:
        result = engine.sync_single(single_repo, force=force)
        status_color = {"synced": "green", "up_to_date": "dim", "failed": "red"}.get(result.status, "white")
        console.print(f"  [{status_color}]{result.status}[/{status_color}] {single_repo}")
    else:
        results = engine.run(force=force)
        synced = sum(1 for r in results if r.status == "synced")
        up     = sum(1 for r in results if r.status == "up_to_date")
        failed = sum(1 for r in results if r.status == "failed")
        console.print(f"\n[bold]Sync complete:[/bold] {synced} synced, {up} up-to-date, {failed} failed.")


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------

@cli.command("daemon")
@click.pass_context
def cmd_daemon(ctx: click.Context) -> None:
    """Start the background scheduler daemon (blocking)."""
    from .scheduler import run_daemon
    cfg_file = ctx.obj.get("config_file")
    console.print("[bold green]gh-autofork daemon starting…[/bold green]")
    run_daemon(config_file=cfg_file)


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

@cli.group("service")
def cmd_service() -> None:
    """Install or manage the OS startup service."""


@cmd_service.command("install")
@click.pass_context
def service_install(ctx: click.Context) -> None:
    """Install the OS startup service (systemd / launchd / Task Scheduler)."""
    cfg_file = ctx.obj.get("config_file")
    try:
        msg = install_service(config_file=str(cfg_file) if cfg_file else None)
        console.print(f"[green]{msg}[/green]")
    except GHAutoForkError as exc:
        err_console.print(str(exc))
        sys.exit(1)


@cmd_service.command("uninstall")
def service_uninstall() -> None:
    """Remove the OS startup service."""
    try:
        msg = uninstall_service()
        console.print(f"[yellow]{msg}[/yellow]")
    except GHAutoForkError as exc:
        err_console.print(str(exc))
        sys.exit(1)


@cmd_service.command("status")
def service_status_cmd() -> None:
    """Show the status of the startup service."""
    output = service_status()
    console.print(output)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@cli.command("stats")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@click.option("--no-api", is_flag=True, default=False, help="Skip GitHub API call for rate limit.")
@click.pass_context
def cmd_stats(ctx: click.Context, output_json: bool, no_api: bool) -> None:
    """Display fork statistics."""
    cfg    = _cfg(ctx)
    db     = _db(ctx)
    client = None if no_api else _client(ctx)

    stats = collect_stats(db, client)
    if output_json:
        print(stats_to_json(stats))
    else:
        console.print(format_stats_text(stats))


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@cli.group("config")
def cmd_config() -> None:
    """Show or manage configuration."""


@cmd_config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Display current configuration (token is masked)."""
    cfg = _cfg(ctx)
    import dataclasses, yaml
    d = dataclasses.asdict(cfg)
    # Mask token
    if d.get("github", {}).get("token"):
        tok = d["github"]["token"]
        d["github"]["token"] = tok[:8] + "…" + tok[-4:] if len(tok) > 12 else "***"
    console.print(yaml.dump(d, default_flow_style=False))


@cmd_config.command("init")
@click.option("--token", prompt="GitHub Personal Access Token", hide_input=True)
@click.pass_context
def config_init(ctx: click.Context, token: str) -> None:
    """Interactively create a default config file."""
    from .config import DEFAULT_CONFIG_FILE, AppConfig, GithubConfig
    cfg = AppConfig()
    cfg.github = GithubConfig(token=token)
    init_config_dir()
    path = save_config(cfg)
    console.print(f"[green]Config written to:[/green] {path}")
    console.print("[dim]Edit the file to customise filters, watchlist, schedule, etc.[/dim]")


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------

@cli.group("db")
def cmd_db() -> None:
    """Database management utilities."""


@cmd_db.command("vacuum")
@click.pass_context
def db_vacuum(ctx: click.Context) -> None:
    """Run VACUUM on the SQLite database to reclaim space."""
    db = _db(ctx)
    db.vacuum()
    console.print("[green]VACUUM complete.[/green]")


@cmd_db.command("check")
@click.pass_context
def db_check(ctx: click.Context) -> None:
    """Run SQLite integrity check."""
    db = _db(ctx)
    ok = db.integrity_check()
    if ok:
        console.print("[green]Database integrity OK.[/green]")
    else:
        err_console.print("Database integrity check FAILED.")
        sys.exit(1)


@cmd_db.command("path")
@click.pass_context
def db_path(ctx: click.Context) -> None:
    """Print the path to the SQLite database file."""
    console.print(_cfg(ctx).db_path)


@cmd_db.command("purge-cache")
@click.pass_context
def db_purge_cache(ctx: click.Context) -> None:
    """Remove expired entries from the key-value cache."""
    db = _db(ctx)
    n = db.cache_purge_expired()
    console.print(f"[green]Purged[/green] {n} expired cache entries.")


# ---------------------------------------------------------------------------
# rate-limit
# ---------------------------------------------------------------------------

@cli.command("rate-limit")
@click.pass_context
def cmd_rate_limit(ctx: click.Context) -> None:
    """Show current GitHub API rate limit status."""
    client = _client(ctx)
    rl = client.get_rate_limit()
    pct_used = (rl.used / rl.limit * 100) if rl.limit else 0
    console.print(
        f"  Remaining : [bold]{rl.remaining:,}[/bold] / {rl.limit:,}"
    )
    console.print(f"  Used      : {rl.used:,}  ({pct_used:.0f}%)")
    console.print(f"  Resets at : {rl.reset_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    console.print(f"  In        : {int(rl.seconds_until_reset)}s")


# ---------------------------------------------------------------------------
# Internal: run queue with progress bar
# ---------------------------------------------------------------------------

def _run_queue(
    ctx: click.Context,
    forker: BatchForker,
    max_workers: Optional[int] = None,
    include_retries: bool = True,
) -> None:
    cfg = _cfg(ctx)
    db  = _db(ctx).conn  # just to check pending count

    # Count pending
    pending_count = len(forker._db.get_pending_forks(limit=100_000))
    if pending_count == 0:
        console.print("[dim]Queue is empty – nothing to do.[/dim]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task_id = progress.add_task("Forking…", total=pending_count)

        def on_progress(p: BatchProgress) -> None:
            progress.update(
                task_id,
                completed=p.done,
                total=p.total,
                description=(
                    f"[bold]{p.current_repo}[/bold]"
                    if not p.is_done
                    else f"Done ({p.success} ok, {p.failed} failed, {p.skipped} skipped)"
                ),
            )

        def on_result(r: ForkResult) -> None:
            if r.success and not r.dry_run:
                console.log(f"  [green]✓[/green] {r.repo_full_name} → {r.fork_full_name}")
            elif r.success and r.dry_run:
                console.log(f"  [blue]~[/blue] [DRY-RUN] {r.repo_full_name}")
            elif r.skipped:
                console.log(f"  [dim]–[/dim] {r.repo_full_name} (skipped)")
            else:
                console.log(f"  [red]✗[/red] {r.repo_full_name}: {r.error}")

        forker._on_progress = on_progress
        forker._on_result   = on_result

        results = forker.run(
            include_retries=include_retries,
            max_workers=max_workers,
        )

    success = sum(1 for r in results if r.success)
    failed  = sum(1 for r in results if not r.success and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    console.print(
        f"\n[bold]Batch complete:[/bold] "
        f"[green]{success} success[/green], "
        f"[red]{failed} failed[/red], "
        f"[dim]{skipped} skipped[/dim]"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
