"""launchd agent management — install/uninstall the lios-sync daemon as a macOS service.

Generates and manages `~/Library/LaunchAgents/com.lios.sync.plist`.
"""

import logging
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PLIST_LABEL = "com.lios.sync"
# The label this daemon had until 2026-09-02 (lios W10). `install_agent` boots
# it out and removes its plist so the two never fight over :9400 — an old
# daemon left running beside the new one would answer health checks and win.
LEGACY_LABEL = "com.cograda.comar"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{PLIST_LABEL}.plist"
LEGACY_PLIST_PATH = PLIST_DIR / f"{LEGACY_LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "lios"


def install_agent() -> None:
    """Install and start the comar daemon as a launchd agent."""
    comar_bin = shutil.which("lios-sync")
    if not comar_bin:
        comar_bin = str(Path.home() / ".local" / "bin" / "lios-sync")
        if not Path(comar_bin).is_file():
            raise FileNotFoundError(
                "Cannot find `lios-sync` binary. Install with: pipx install lios-sync"
            )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_DIR.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": PLIST_LABEL,
        "ProgramArguments": [comar_bin, "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_DIR / "daemon.log"),
        "StandardErrorPath": str(LOG_DIR / "daemon.err"),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        },
    }

    if PLIST_PATH.is_file():
        _bootout()
    _retire_legacy_agent()

    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    _bootstrap()
    logger.info(f"LaunchAgent {PLIST_LABEL} installed and started")
    print(f"  LaunchAgent {PLIST_LABEL} installed and started ✓")
    print(f"  Logs: {LOG_DIR}/daemon.log")


def _retire_legacy_agent() -> None:
    """Stop and remove the pre-rename `com.cograda.comar` agent if it is present."""
    if not LEGACY_PLIST_PATH.is_file():
        return
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LEGACY_LABEL}"],
        capture_output=True, check=False,
    )
    LEGACY_PLIST_PATH.unlink()
    logger.info(f"Retired legacy LaunchAgent {LEGACY_LABEL}")
    print(f"  Retired legacy LaunchAgent {LEGACY_LABEL} ✓")


def uninstall_agent() -> None:
    """Stop and remove the comar launchd agent."""
    if not PLIST_PATH.is_file():
        print(f"  {PLIST_LABEL} is not installed")
        return

    _bootout()
    PLIST_PATH.unlink()
    logger.info(f"LaunchAgent {PLIST_LABEL} uninstalled")
    print(f"  LaunchAgent {PLIST_LABEL} uninstalled ✓")


def is_installed() -> bool:
    return PLIST_PATH.is_file()


def is_running() -> bool:
    """Check if the daemon is running via launchctl."""
    try:
        r = subprocess.run(
            ["launchctl", "list", PLIST_LABEL],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def get_pid() -> int | None:
    """Get the daemon PID from launchctl, or None if not running."""
    try:
        r = subprocess.run(
            ["launchctl", "list", PLIST_LABEL],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        for line in r.stdout.strip().splitlines():
            if '"PID"' in line:
                parts = line.split("=")
                if len(parts) >= 2:
                    pid_str = parts[1].strip().rstrip(";").strip()
                    return int(pid_str)
    except Exception:
        pass
    return None


def _bootstrap():
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True,
    )


def _bootout():
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{PLIST_LABEL}"],
        capture_output=True,
    )
