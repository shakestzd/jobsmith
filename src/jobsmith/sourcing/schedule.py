"""jobsmith.sourcing.schedule — launchd schedule helpers (feat-80affa8a).

Provides:
  - render_plist(): render the macOS launchd plist template with absolute paths
  - install_schedule(): render + load via launchctl (tested with mocked launchctl)

Design decisions
----------------
- The label is com.jobsmith.sourcing — separate from the old com.shakes.morning-sourcing.
- JOBSMITH_REPO_ROOT is injected as an EnvironmentVariable so the runner
  finds the user's DB + sourcing.yaml regardless of launchd's minimal PATH.
- Schedule: 7:00 AM UTC daily (configurable via hour/minute params).
  Users in EDT (UTC-4) will see this as 3:00 AM; adjust as needed.
  Default kept at 7:00 UTC — same rough window as old plist (11:13 UTC).
- RunAtLoad=false: installing does NOT trigger an immediate run.
- The log paths are written under JOBSMITH_REPO_ROOT/logs/ by default.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

LAUNCHD_LABEL = "com.jobsmith.sourcing"

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  jobsmith sourcing schedule agent (feat-80affa8a).

  Template location: src/jobsmith/sourcing/schedule.py
  Install via:  jobsmith source install-schedule
  Uninstall:    launchctl bootout gui/$(id -u)/com.jobsmith.sourcing
                rm ~/Library/LaunchAgents/com.jobsmith.sourcing.plist

  Manual trigger:
    launchctl kickstart -k gui/$(id -u)/com.jobsmith.sourcing
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{binary_path}</string>
        <string>source</string>
        <string>run</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{repo_root}</string>

    <key>StandardOutPath</key>
    <string>{log_dir}/sourcing-launchd.out</string>

    <key>StandardErrorPath</key>
    <string>{log_dir}/sourcing-launchd.err</string>

    <key>RunAtLoad</key>
    <false/>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>{minute}</integer>
    </dict>

    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{home}</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>JOBSMITH_REPO_ROOT</key>
        <string>{repo_root}</string>
    </dict>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def render_plist(
    *,
    binary_path: Path,
    repo_root: Path,
    log_dir: Path,
    hour: int = 11,
    minute: int = 13,
    label: str = LAUNCHD_LABEL,
    home: str | None = None,
) -> str:
    """Render the launchd plist with absolute paths.

    Parameters
    ----------
    binary_path:
        Absolute path to the installed ``jobsmith`` binary.
    repo_root:
        Absolute path to the user's repo root (JOBSMITH_REPO_ROOT value).
    log_dir:
        Directory for stdout/stderr log files (will be created by launchd).
    hour:
        UTC hour for StartCalendarInterval (default: 11 → ~7 AM ET EDT).
    minute:
        Minute for StartCalendarInterval (default: 13).
    label:
        launchd label (default: com.jobsmith.sourcing).
    home:
        HOME env var value; defaults to str(Path.home()).
    """
    if home is None:
        home = str(Path.home())
    return _PLIST_TEMPLATE.format(
        label=label,
        binary_path=str(binary_path),
        repo_root=str(repo_root),
        log_dir=str(log_dir),
        hour=hour,
        minute=minute,
        home=home,
    )


def install_schedule(
    *,
    repo_root: Path,
    log_dir: Path | None = None,
    hour: int = 11,
    minute: int = 13,
    plist_dest: Path | None = None,
) -> Path:
    """Render the plist, write to ~/Library/LaunchAgents/, and load via launchctl.

    Parameters
    ----------
    repo_root:
        Absolute path to the user's repo root (JOBSMITH_REPO_ROOT).
    log_dir:
        Directory for log files (default: repo_root/logs).
    hour, minute:
        UTC schedule time.
    plist_dest:
        Override the plist destination path (default: ~/Library/LaunchAgents/).

    Returns the path where the plist was written.

    Notes
    -----
    - The installed ``jobsmith`` binary is located via ``shutil.which("jobsmith")``.
    - ``launchctl bootstrap gui/<uid> <plist>`` is used on macOS 10.11+ (Mojave+).
      Falls back to ``launchctl load`` for older systems.
    - Raises ``RuntimeError`` if ``jobsmith`` is not on PATH.
    """
    binary = shutil.which("jobsmith")
    if binary is None:
        raise RuntimeError(
            "jobsmith binary not found on PATH — install with: uv tool install jobsmith"
        )

    if log_dir is None:
        log_dir = repo_root / "logs"

    agents_dir = Path.home() / "Library" / "LaunchAgents"
    if plist_dest is None:
        plist_dest = agents_dir / f"{LAUNCHD_LABEL}.plist"

    plist_content = render_plist(
        binary_path=Path(binary),
        repo_root=repo_root,
        log_dir=log_dir,
        hour=hour,
        minute=minute,
    )

    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_dest.write_text(plist_content, encoding="utf-8")

    # Load via launchctl. Prefer bootstrap (modern), fall back to load.
    import os

    uid = os.getuid()
    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_dest)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback for older macOS or if bootstrap fails (e.g. already loaded)
        subprocess.run(
            ["launchctl", "load", str(plist_dest)],
            capture_output=True,
            text=True,
        )

    return plist_dest


__all__ = [
    "LAUNCHD_LABEL",
    "render_plist",
    "install_schedule",
]
