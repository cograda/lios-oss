"""Client auto-updater — download new wheel from server, install via pipx, restart daemon.

Called from the heartbeat loop when the server reports a newer version.
The server serves wheels at /api/client/download/latest.
"""

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from packaging.version import Version

logger = logging.getLogger(__name__)


def check_for_update(current_version: str, latest_version: str) -> bool:
    """Return True if the server has a newer version than what's running."""
    if not latest_version or not current_version:
        return False
    try:
        return Version(latest_version) > Version(current_version)
    except Exception:
        # Unparseable version — compare as strings
        return latest_version != current_version


def _fetch_wheel_filename(base: str) -> str | None:
    """Ask the server for the latest wheel's real filename.

    pip refuses wheels whose filename isn't PEP 427 (it parses
    name/version from it), so the download must be saved under the
    server-reported name — not a generic 'comar_client.whl'.
    """
    try:
        result = subprocess.run(
            ["curl", "-fsSL", f"{base}/api/client/version"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        name = json.loads(result.stdout).get("wheel", "")
    except (json.JSONDecodeError, AttributeError):
        return None
    # The name lands in a filesystem path — accept plain wheel names only.
    if not name or "/" in name or not name.endswith(".whl"):
        return None
    return name


def download_and_install(server_url: str, expected_checksum: str = "") -> bool:
    """Download the latest wheel from the server and install via pipx.

    `server_url` is the V3 base URL (e.g. `http://192.168.1.50:8400` or
    `https://comar.lab`). The wheel endpoint is appended directly.
    Verifies SHA256 if `expected_checksum` is provided.
    """
    base = server_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    download_url = f"{base}/api/client/download/latest"

    wheel_name = _fetch_wheel_filename(base)
    if not wheel_name:
        logger.error("Could not determine wheel filename from server — aborting update")
        return False

    tmpdir = tempfile.mkdtemp(prefix="comar-update-")
    wheel_path = Path(tmpdir) / wheel_name

    try:
        # Download wheel
        logger.info(f"Downloading client update from {download_url}")
        result = subprocess.run(
            ["curl", "-fsSL", download_url, "-o", str(wheel_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error(f"Download failed: {result.stderr}")
            return False

        if not wheel_path.is_file() or wheel_path.stat().st_size < 1024:
            logger.error("Downloaded file is too small or missing")
            return False

        # Verify SHA256 checksum if provided
        if expected_checksum:
            actual = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
            if actual != expected_checksum:
                logger.error(
                    f"Checksum mismatch! Expected {expected_checksum[:16]}..., "
                    f"got {actual[:16]}... — aborting update"
                )
                return False
            logger.info("Wheel checksum verified")

        # Install via pipx
        logger.info("Installing update via pipx...")
        result = subprocess.run(
            ["pipx", "install", str(wheel_path), "--force"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error(f"pipx install failed: {result.stderr}")
            return False

        logger.info("Update installed successfully")
        return True

    except subprocess.TimeoutExpired:
        logger.error("Update timed out")
        return False
    except Exception:
        logger.exception("Update failed")
        return False
    finally:
        # Cleanup temp dir
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def restart_daemon() -> None:
    """Restart the daemon via launchctl kickstart.

    This kills the current process and launchd re-launches it with the new binary.
    """
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    label = "com.cograda.comar"

    logger.info("Restarting daemon via launchctl kickstart...")
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
        capture_output=True,
    )
    # If kickstart works, this process gets killed — we won't reach here.
    # If it doesn't (e.g. not running under launchd), log and continue.
    logger.warning("kickstart returned — daemon may not be running under launchd")
