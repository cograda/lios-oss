"""Client distribution endpoints — version check, wheel download, bootstrap script.

Serves client wheels from the client-dist/ directory.

V4 chunk 2.5 — auth stance, chosen deliberately after tracing both
new-machine onboarding paths end to end (see routes/install.py and the
INSTALL_SCRIPT_TEMPLATE there):

  - `GET /version` and `GET /bootstrap.sh` stay fully open. Both are static
    and non-sensitive (a version string, and a generic shell script with no
    embedded secret — the *rendered* per-machine installer lives at
    `/api/install/{code}` instead, which is already code-gated). Closing
    these would only make a brand-new Mac's first `curl` fail with no
    upside.
  - `GET /download/latest` and `GET /download/{filename}` now require a
    valid bearer (`client_tokens`, via `resolve_token_to_user`) OR a live
    (unexpired) install code passed as `?code=`. This is safe for BOTH real
    onboarding flows:
      1. `/api/install/{code}` (the current, documented flow) already bakes
         `Authorization: Bearer ${TOKEN}` into its wheel-download curl
         (`step_install_daemon` in install.py) — nothing to change there.
      2. The legacy `bootstrap.sh` template below did NOT send that header
         (it only used TOKEN for `lios-sync setup --token`) — fixed in this
         chunk to add the same header, since the token is already available
         as an argv the caller was handed out-of-band. Without that fix,
         gating the download endpoint would have broken this path.
    The wheel itself carries no secrets, so this is about not handing out
    free bandwidth/recon to an unauthenticated prober, not about protecting
    sensitive content.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from packaging.version import InvalidVersion, Version
from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/client", tags=["client"])


def _install_code_is_live(code: str) -> bool:
    """True if `code` exists in `install_codes` and hasn't expired yet.

    Deliberately does NOT require `redeemed_at is None` — by the time a
    device calls a download endpoint it may already have redeemed its
    install code (that's the normal `/api/install/{code}` flow, which
    authenticates via the bearer branch instead). This check exists for the
    "install code, no bearer yet" case the spec calls out explicitly.
    """
    if not code:
        return False
    from app.db import get_db
    from app.models.clients import InstallCode

    db = get_db()
    with db.session() as session:
        ic = session.execute(
            select(InstallCode).where(InstallCode.code == code)
        ).scalar_one_or_none()
        return ic is not None and ic.expires_at > datetime.now(timezone.utc)


def require_download_auth(
    request: Request, authorization: str = Header(default=""),
) -> None:
    """Gate wheel downloads: a valid client bearer OR a live install code.

    Raises 401 (and records an `auth_events` row) if neither is present.
    """
    from app.auth.client_token import TokenExpiredError, resolve_token_to_user
    from app.auth.hashing import token_last4
    from app.services.auth_events import record_auth_event

    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    client_ip = request.client.host if request.client else None

    if token:
        try:
            user = resolve_token_to_user(token)
        except TokenExpiredError:
            user = None
        if user is not None:
            return

    code = request.query_params.get("code", "")
    if _install_code_is_live(code):
        return

    record_auth_event(
        outcome="401",
        token_last4=token_last4(token) if token else None,
        source_ip=client_ip,
        transport="http",
    )
    raise HTTPException(
        status_code=401,
        detail="Wheel download requires a valid client bearer token or install code",
        headers={"WWW-Authenticate": "Bearer"},
    )

# Where wheels are stored — Docker path first, then relative to backend
_DIST_DIRS = [
    Path("/app/client-dist"),
    Path(__file__).resolve().parents[3] / "client-dist",
]

# Wheel filenames look like lios_sync-<version>-py3-none-any.whl (comar_client-*
# until 2026-09-02; those wheels are no longer built or served — see the
# heartbeat freeze in api/v1.py for how 2.x daemons are kept from updating).
VERSION_RE = re.compile(r"lios_sync-([^-]+)-")


def parse_wheel_version(path: Path) -> Version | None:
    """Extract the PEP 440 version from a wheel filename, or None if unparseable."""
    m = VERSION_RE.search(path.name)
    if not m:
        return None
    try:
        return Version(m.group(1))
    except InvalidVersion:
        return None


def pick_latest_wheel(wheels: Iterable[Path]) -> Path | None:
    """Pick the highest-version wheel from a set of wheel file paths.

    Compares parsed `packaging.version.Version` values rather than sorting
    filenames lexically — a lexical sort ranks "10.0.0" below "2.3.2" and
    will happily pick a stray/unrelated file. Filenames that don't parse as
    a version are ignored (never picked as "latest").
    """
    versioned = [(v, w) for w in wheels if (v := parse_wheel_version(w)) is not None]
    if not versioned:
        return None
    return max(versioned, key=lambda pair: pair[0])[1]

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
curl -fsSL -H "Authorization: Bearer $TOKEN" "https://comar.lab/api/client/download/latest" -o "$TMPDIR/lios_sync.whl"
pipx install "$TMPDIR/lios_sync.whl" --force

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

    wheel_path = pick_latest_wheel(dist_dir.glob("lios_sync-*.whl"))
    if wheel_path is None:
        return None

    m = VERSION_RE.search(wheel_path.name)
    if not m:
        return None

    return wheel_path, m.group(1), _compute_sha256(wheel_path)


@router.get("/version")
async def client_version():
    """Return the latest available client version."""
    result = _get_latest_wheel()
    if not result:
        return {"version": None, "wheel": None, "sha256": None}

    wheel_path, version, sha256 = result
    return {"version": version, "wheel": wheel_path.name, "sha256": sha256}


@router.get("/download/latest")
async def download_latest(_auth: None = Depends(require_download_auth)):
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
async def download_wheel(filename: str, _auth: None = Depends(require_download_auth)):
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
