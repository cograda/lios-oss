"""launchd agent management — install/uninstall the comar daemon as a macOS service.

Generates and manages `~/Library/LaunchAgents/com.cograda.comar.plist`.
"""

import logging
import plistlib
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PLIST_LABEL = "com.cograda.comar"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{PLIST_LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "comar"


def install_agent() -> None:
    """Install and start the comar daemon as a launchd agent."""
    comar_bin = shutil.which("comar")
    if not comar_bin:
        comar_bin = str(Path.home() / ".local" / "bin" / "comar")
        if not Path(comar_bin).is_file():
            raise FileNotFoundError(
                "Cannot find `comar` binary. Install with: pipx install comar-client"
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

    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    _bootstrap()
    logger.info(f"LaunchAgent {PLIST_LABEL} installed and started")
    print(f"  LaunchAgent {PLIST_LABEL} installed and started ✓")
    print(f"  Logs: {LOG_DIR}/daemon.log")


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
