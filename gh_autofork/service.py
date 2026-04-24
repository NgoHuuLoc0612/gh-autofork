"""
Cross-platform startup service installer for gh-autofork.

Supports:
  - Linux  → systemd user service  (~/.config/systemd/user/)
  - macOS  → launchd user agent    (~/Library/LaunchAgents/)
  - Windows → Task Scheduler       (via schtasks.exe)

The service runs ``gh-autofork daemon`` on login / system start.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

from .exceptions import ServiceError

log = logging.getLogger(__name__)

_SYSTEM = platform.system()   # Linux | Darwin | Windows


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _gh_autofork_bin() -> str:
    """Return the absolute path to the gh-autofork executable."""
    exe = shutil.which("gh-autofork")
    if exe:
        return exe
    # fall back to running through the current interpreter
    return f"{sys.executable} -m gh_autofork"


def _assert_not_root() -> None:
    if _SYSTEM != "Windows" and os.getuid() == 0:
        raise ServiceError(
            "Running the startup service as root is not supported and unsafe. "
            "Install as a regular user."
        )


# ---------------------------------------------------------------------------
# Linux – systemd user service
# ---------------------------------------------------------------------------

_SYSTEMD_UNIT_TEMPLATE = textwrap.dedent("""\
    [Unit]
    Description=gh-autofork – automatic GitHub repository forker
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    ExecStart={exec_start}
    Restart=on-failure
    RestartSec=30
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=gh-autofork
    Environment=HOME={home}

    [Install]
    WantedBy=default.target
""")

_SYSTEMD_SERVICE_NAME = "gh-autofork.service"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_SERVICE_NAME


def install_systemd(config_file: Optional[str] = None) -> Path:
    _assert_not_root()
    if not shutil.which("systemctl"):
        raise ServiceError("systemctl not found; is systemd installed?")

    bin_path = _gh_autofork_bin()
    extra = f" --config {config_file}" if config_file else ""
    exec_start = f"{bin_path} daemon{extra}"

    unit_content = _SYSTEMD_UNIT_TEMPLATE.format(
        exec_start=exec_start,
        home=Path.home(),
    )
    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_content)

    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", _SYSTEMD_SERVICE_NAME])

    log.info("systemd service installed and started: %s", unit_path)
    return unit_path


def uninstall_systemd() -> None:
    _run(["systemctl", "--user", "disable", "--now", _SYSTEMD_SERVICE_NAME], check=False)
    unit_path = _systemd_unit_path()
    if unit_path.exists():
        unit_path.unlink()
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    log.info("systemd service uninstalled.")


def status_systemd() -> str:
    result = subprocess.run(
        ["systemctl", "--user", "status", _SYSTEMD_SERVICE_NAME],
        capture_output=True, text=True,
    )
    return result.stdout + result.stderr


# ---------------------------------------------------------------------------
# macOS – launchd user agent
# ---------------------------------------------------------------------------

_PLIST_TEMPLATE = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>Label</key>
        <string>com.gh-autofork.daemon</string>

        <key>ProgramArguments</key>
        <array>
    {program_args}
        </array>

        <key>RunAtLoad</key>
        <true/>

        <key>KeepAlive</key>
        <dict>
            <key>SuccessfulExit</key>
            <false/>
        </dict>

        <key>StandardOutPath</key>
        <string>{log_dir}/daemon.stdout.log</string>

        <key>StandardErrorPath</key>
        <string>{log_dir}/daemon.stderr.log</string>

        <key>EnvironmentVariables</key>
        <dict>
            <key>HOME</key>
            <string>{home}</string>
            <key>PATH</key>
            <string>{path}</string>
        </dict>

        <key>ThrottleInterval</key>
        <integer>30</integer>
    </dict>
    </plist>
""")

_PLIST_LABEL   = "com.gh-autofork.daemon"
_PLIST_FILENAME = f"{_PLIST_LABEL}.plist"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / _PLIST_FILENAME


def _format_plist_args(*args: str) -> str:
    return "\n".join(f"        <string>{a}</string>" for a in args)


def install_launchd(config_file: Optional[str] = None, log_dir: Optional[str] = None) -> Path:
    _assert_not_root()
    if not shutil.which("launchctl"):
        raise ServiceError("launchctl not found; not a macOS system?")

    bin_path = _gh_autofork_bin()
    args = bin_path.split() + ["daemon"]
    if config_file:
        args += ["--config", config_file]

    _log_dir = log_dir or str(Path.home() / ".local" / "share" / "gh-autofork")
    Path(_log_dir).mkdir(parents=True, exist_ok=True)

    plist_content = _PLIST_TEMPLATE.format(
        program_args=_format_plist_args(*args),
        log_dir=_log_dir,
        home=Path.home(),
        path=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    )
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist_content)

    # Unload first in case it was previously loaded
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
    )
    _run(["launchctl", "load", "-w", str(plist_path)])

    log.info("launchd agent installed: %s", plist_path)
    return plist_path


def uninstall_launchd() -> None:
    plist_path = _launchd_plist_path()
    if plist_path.exists():
        _run(["launchctl", "unload", "-w", str(plist_path)], check=False)
        plist_path.unlink()
    log.info("launchd agent removed.")


def status_launchd() -> str:
    result = subprocess.run(
        ["launchctl", "list", _PLIST_LABEL],
        capture_output=True, text=True,
    )
    return result.stdout or result.stderr


# ---------------------------------------------------------------------------
# Windows – Task Scheduler
# ---------------------------------------------------------------------------

_TASK_NAME = "GHAutoFork"


def install_windows(config_file: Optional[str] = None) -> None:
    if not shutil.which("schtasks"):
        raise ServiceError("schtasks.exe not found.")
    bin_path = _gh_autofork_bin()
    cmd_args = f'{bin_path} daemon'
    if config_file:
        cmd_args += f' --config "{config_file}"'

    # Delete existing task if present
    subprocess.run(
        ["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"],
        capture_output=True,
    )

    _run([
        "schtasks", "/Create",
        "/TN", _TASK_NAME,
        "/TR", cmd_args,
        "/SC", "ONLOGON",
        "/RU", os.environ.get("USERNAME", ""),
        "/RL", "HIGHEST",
        "/F",
    ])
    log.info("Windows scheduled task '%s' created.", _TASK_NAME)


def uninstall_windows() -> None:
    _run(["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"], check=False)
    log.info("Windows scheduled task '%s' removed.", _TASK_NAME)


def status_windows() -> str:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", _TASK_NAME, "/FO", "LIST"],
        capture_output=True, text=True,
    )
    return result.stdout or result.stderr


# ---------------------------------------------------------------------------
# Unified API
# ---------------------------------------------------------------------------

def install_service(config_file: Optional[str] = None) -> str:
    """Install the startup service for the current platform."""
    if _SYSTEM == "Linux":
        path = install_systemd(config_file)
        return f"systemd user service installed: {path}"
    elif _SYSTEM == "Darwin":
        path = install_launchd(config_file)
        return f"launchd agent installed: {path}"
    elif _SYSTEM == "Windows":
        install_windows(config_file)
        return f"Windows Task Scheduler task '{_TASK_NAME}' created."
    else:
        raise ServiceError(f"Unsupported platform: {_SYSTEM}")


def uninstall_service() -> str:
    """Remove the startup service for the current platform."""
    if _SYSTEM == "Linux":
        uninstall_systemd()
        return "systemd user service removed."
    elif _SYSTEM == "Darwin":
        uninstall_launchd()
        return "launchd agent removed."
    elif _SYSTEM == "Windows":
        uninstall_windows()
        return f"Windows Task Scheduler task '{_TASK_NAME}' removed."
    else:
        raise ServiceError(f"Unsupported platform: {_SYSTEM}")


def service_status() -> str:
    """Return a human-readable status string for the current platform."""
    if _SYSTEM == "Linux":
        return status_systemd()
    elif _SYSTEM == "Darwin":
        return status_launchd()
    elif _SYSTEM == "Windows":
        return status_windows()
    else:
        return f"Unsupported platform: {_SYSTEM}"


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("Running: %s", " ".join(str(x) for x in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ServiceError(
            f"Command failed ({result.returncode}): {' '.join(str(x) for x in cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result
