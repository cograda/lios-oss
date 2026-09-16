"""Client auto-updater — download new wheel from server, install via pipx, restart daemon.

Called from the heartbeat loop when the server reports a newer version.
The server serves wheels at /api/client/download/latest — which requires the
per-user bearer since V4 chunk 2.5 (`require_download_auth`), so the download
must be authenticated. Downloads go via httpx rather than a `curl` subprocess:
same behaviour, and the token never appears in a process argv.
"""

import hashlib
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
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
    (`/api/client/version` is deliberately unauthenticated.)
    """
    try:
        resp = httpx.get(f"{base}/api/client/version", timeout=30)
        resp.raise_for_status()
        name = resp.json().get("wheel", "")
    except Exception:  # noqa: BLE001
        return None
    # The name lands in a filesystem path — accept plain wheel names only.
    if not name or not isinstance(name, str) or "/" in name or not name.endswith(".whl"):
        return None
    return name


def install_command(wheel_path: Path) -> list[str]:
    """The command that installs `wheel_path` into the environment THIS daemon
    is running from — not into whatever tool happens to be on PATH.

    Two install layouts exist: the pre-September pipx package (Alex's Mac,
    `~/.local/pipx/venvs/lios-sync`) and the from-scratch installer's plain
    venv under `~/Library/Application Support/lios/venv` (Sam's Mac,
    2026-09-06). Until 3.0.3 this always ran `pipx install`, which on a venv
    install raised FileNotFoundError — pipx is not on that Mac at all — so
    every update there was downloaded, verified and then never installed,
    while the pipx Mac updated fine. Detect from `sys.prefix`, because the
    interpreter running this code is by definition the one to upgrade.
    """
    if "/pipx/venvs/" in sys.prefix.replace(os.sep, "/") + "/":
        return ["pipx", "install", str(wheel_path), "--force"]
    return [sys.executable, "-m", "pip", "install", "--quiet", "--force-reinstall", str(wheel_path)]


def download_and_install(
    server_url: str, expected_checksum: str = "", token: str = "",
) -> bool:
    """Download the latest wheel from the server and install it into the running environment.

    `server_url` is the V3 base URL (e.g. `http://192.168.1.50:8400` or
    `https://comar.lab`). The wheel endpoint is appended directly.
    `token` is the per-user bearer — the download endpoint 401s without it
    (V4 chunk 2.5 gated wheel downloads; the updater going without a bearer
    is what silently broke every daemon's auto-update until 2.6.1).
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

    tmpdir = tempfile.mkdtemp(prefix="lios-sync-update-")
    wheel_path = Path(tmpdir) / wheel_name

    try:
        # Download wheel (authenticated — see docstring)
        logger.info(f"Downloading client update from {download_url}")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with httpx.stream(
                "GET", download_url, headers=headers, timeout=60, follow_redirects=True,
            ) as resp:
                resp.raise_for_status()
                with open(wheel_path, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.error(
                    "Download failed: 401 — the server requires the client bearer "
                    "for wheel downloads and this token was rejected"
                )
            else:
                logger.error(f"Download failed: HTTP {e.response.status_code}")
            return False
        except httpx.HTTPError as e:
            logger.error(f"Download failed: {e}")
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

        cmd = install_command(wheel_path)
        logger.info("Installing update via %s...", " ".join(cmd[:3]))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error(f"install failed ({cmd[0]}): {result.stderr}")
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
    """Restart the daemon so the freshly installed wheel is what runs.

    Three attempts, each only reached if the one before did not kill us:

    1. `launchctl kickstart -k` on the current label (`launchd.PLIST_LABEL`).
    2. The same on the pre-rename label, for a Mac whose agent was never
       migrated.
    3. Exit. The agent is installed with `KeepAlive`, so launchd relaunches
       it — with the new binary — whatever the label is.

    ⚠️ Until 2026-09-06 step 1 used the literal `com.cograda.comar`, which the
    2026-09-02 rename to `com.lios.sync` orphaned. Every auto-update after the
    rename installed the wheel, logged "daemon may not be running under
    launchd", and left the OLD process running — the heartbeat kept reporting
    the previous version while `lios-sync --version` said the new one. The
    third step exists so a label mismatch can never do that again.
    """
    from lios_sync.launchd import LEGACY_LABEL, PLIST_LABEL

    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    for label in (PLIST_LABEL, LEGACY_LABEL):
        logger.info("Restarting daemon via launchctl kickstart (%s)...", label)
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True,
        )
        # If kickstart worked, this process is already dead.
    logger.warning(
        "kickstart on %s and %s both returned — exiting so launchd's KeepAlive "
        "relaunches the new binary",
        PLIST_LABEL, LEGACY_LABEL,
    )
    _exit_for_relaunch()


def _exit_for_relaunch() -> None:  # pragma: no cover — replaced in tests
    os._exit(0)
