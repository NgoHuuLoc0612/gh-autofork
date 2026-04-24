"""
Shared utility functions for gh-autofork.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .config import AppConfig


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(cfg: AppConfig) -> None:
    """Configure root logger based on AppConfig."""
    log_cfg = cfg.logging
    level   = getattr(logging, log_cfg.level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)-30s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # File handler
    log_path = Path(log_cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Stderr handler
    if log_cfg.stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(level)
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ---------------------------------------------------------------------------
# Repo name parsing
# ---------------------------------------------------------------------------

_REPO_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$")
_GITHUB_URL_RE = re.compile(r"github\.com/([^/]+/[^/?\s#]+)")


def parse_repo_name(raw: str) -> Optional[str]:
    """
    Convert any of the following formats to 'owner/repo':

      - owner/repo
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git

    Returns None if the string is not recognisable.
    """
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None

    # git@ SSH URLs
    ssh_match = re.match(r"git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", raw)
    if ssh_match:
        return ssh_match.group(1)

    # HTTPS GitHub URLs
    url_match = _GITHUB_URL_RE.search(raw)
    if url_match:
        candidate = url_match.group(1).rstrip("/").removesuffix(".git")
        if "/" in candidate:
            return candidate

    # Plain owner/repo
    plain_match = _REPO_RE.match(raw)
    if plain_match:
        return plain_match.group(1)

    return None


def parse_repo_file(path: str) -> List[str]:
    """
    Read a file containing one repo per line (owner/repo or GitHub URL).
    Lines starting with '#' are treated as comments.
    Returns a list of 'owner/repo' strings.
    Raises ValueError for lines that cannot be parsed.
    """
    results: List[str] = []
    errors:  List[Tuple[int, str]] = []

    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            parsed = parse_repo_name(raw)
            if parsed:
                results.append(parsed)
            else:
                errors.append((lineno, raw))

    if errors:
        bad = ", ".join(f"line {n}: {r!r}" for n, r in errors[:5])
        extra = f" (and {len(errors)-5} more)" if len(errors) > 5 else ""
        raise ValueError(f"Could not parse {len(errors)} repo name(s): {bad}{extra}")

    return results


# ---------------------------------------------------------------------------
# Text / display helpers
# ---------------------------------------------------------------------------

def human_size(bytes_or_kb: int, is_kb: bool = True) -> str:
    """Return a human-readable size string."""
    n = bytes_or_kb * 1024 if is_kb else bytes_or_kb
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def truncate(s: str, max_len: int = 60, suffix: str = "…") -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len - len(suffix)] + suffix


def pluralise(count: int, singular: str, plural: Optional[str] = None) -> str:
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural or singular + 's'}"


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def chunked(iterable: Iterable, size: int) -> Iterable:
    """Yield successive *size*-sized chunks from *iterable*."""
    chunk: list = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


# ---------------------------------------------------------------------------
# Config directory bootstrap
# ---------------------------------------------------------------------------

def ensure_dirs(cfg: AppConfig) -> None:
    """Create all required directories from the config."""
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.config_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)
