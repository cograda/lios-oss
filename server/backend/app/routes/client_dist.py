"""Client distribution endpoints — version check, wheel download, bootstrap script.

Serves client wheels from the client-dist/ directory. No auth required
(LAN-only, wheel contains no secrets).
"""

import hashlib
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/client", tags=["client"])

# Where wheels are stored — Docker path first, then relative to backend
_DIST_DIRS = [
    Path("/app/client-dist"),
    Path(__file__).resolve().parents[3] / "client-dist",
]

BOOTSTRAP_TEMPLATE = """\
#!/bin/bash
# Comar client bootstrap — installs and configures the daemon.
# Usage: curl -fsSL https://comar.lab/api/client/bootstrap.sh | sh -s -- TOKEN [USER]
set -euo pipefail

TOKEN="${{1:?Usage: curl -fsSL https://comar.lab/api/client/bootstrap.sh | sh -s -- TOKEN [USER]}}"
USER="${{2:-sam}}"
SERVER="{server_url}"

echo "==> Installing comar client for $USER..."

# 1. Ensure pipx is available
if ! command -v pipx &>/dev/null; then
    echo "==> Installing pipx..."
    python3 -m pip install --user pipx 2>/dev/null || brew install pipx
    python3 -m pipx ensurepath 2>/dev/null || true
    export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Download and install latest client wheel
echo "==> Downloading latest client..."
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT
curl -fsSL "https://comar.lab/api/client/download/latest" -o "$TMPDIR/comar_client.whl"
pipx install "$TMPDIR/comar_client.whl" --force

# 3. Non-interactive setup
echo "==> Configuring..."
comar setup --token "$TOKEN" --user "$USER" --server "$SERVER"

# 4. Install launchd agent
echo "==> Installing daemon..."
comar install

echo ""
echo "Done! Comar daemon is running."
echo "Run 'comar status' to check."
"""


def _compute_sha256(path: Path) -> str:
    """Compute SHA256 hex digest of a file (chunked for large files)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_dist_dir() -> Path | None:
    for d in _DIST_DIRS:
        if d.is_dir():
            return d
    return None


def _get_latest_wheel() -> tuple[Path, str, str] | None:
    """Find the latest wheel file and extract its version and SHA256."""
    dist_dir = _find_dist_dir()
    if not dist_dir:
        return None

    wheels = sorted(dist_dir.glob("comar_client-*.whl"), reverse=True)
    if not wheels:
        return None

    m = re.search(r"comar_client-([^-]+)-", wheels[0].name)
    if not m:
        return None

    return wheels[0], m.group(1), _compute_sha256(wheels[0])


@router.get("/version")
async def client_version():
    """Return the latest available client version."""
    result = _get_latest_wheel()
    if not result:
        return {"version": None, "wheel": None, "sha256": None}

    wheel_path, version, sha256 = result
    return {"version": version, "wheel": wheel_path.name, "sha256": sha256}


@router.get("/download/latest")
async def download_latest():
    """Download the latest client wheel."""
    result = _get_latest_wheel()
    if not result:
        raise HTTPException(404, "No client wheel available")

    wheel_path, _, _ = result
    return FileResponse(
        str(wheel_path),
        media_type="application/octet-stream",
        filename=wheel_path.name,
    )


@router.get("/download/{filename}")
async def download_wheel(filename: str):
    """Download a specific client wheel by filename."""
    dist_dir = _find_dist_dir()
    if not dist_dir:
        raise HTTPException(404, "No client distribution directory")

    # Sanitise filename — only allow expected wheel pattern
    if not re.match(r"^comar_client-[\w.]+-[\w.]+-[\w.]+-[\w.]+\.whl$", filename):
        raise HTTPException(400, "Invalid filename")

    wheel_path = dist_dir / filename
    if not wheel_path.is_file():
        raise HTTPException(404, f"Wheel not found: {filename}")

    return FileResponse(
        str(wheel_path),
        media_type="application/octet-stream",
        filename=filename,
    )


@router.get("/bootstrap.sh")
async def bootstrap_script():
    """Serve the bootstrap shell script with the server URL templated in."""
    # V3: HTTP API only. Bootstrap points at the public Caddy URL.
    server_url = getattr(settings, "server_public_url", "https://comar.lab")
    script = BOOTSTRAP_TEMPLATE.format(server_url=server_url)
    return PlainTextResponse(script, media_type="text/x-shellscript")
