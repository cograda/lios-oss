"""Download media bytes from the Baileys bridge into the on-disk store.

Layout: MEDIA_ROOT/<source>/<YYYY>/<MM>/<YYYYMMDD-HHMMSS>_<chat-slug>_<ref8>.<ext>
Postgres (media_items) is the index; the directory layout is just for humans
poking around the volume. All lookups should go through the table.

The bridge endpoint handles CDN expiry itself (asks WhatsApp to re-upload and
retries once), so a plain GET is all we need here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.media.models import MediaItem

BRIDGE_URL = os.environ.get("WA_BRIDGE_URL", "http://lios-whatsapp:3100")

# Auto-download window for the scheduled sync. Anything older is 'expired' at
# scan time anyway (see scan.py); this is a second guard so a stalled scheduler
# doesn't wake up and hammer the bridge with hundreds of doomed requests.
AUTO_DOWNLOAD_WINDOW_DAYS = 30

# Per-sync-cycle cap so one backfill can't monopolise the bridge; the next
# cycle picks up where this one left off.
AUTO_DOWNLOAD_BATCH = 100

EXT_FROM_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/3gpp": "3gp",
    "audio/ogg; codecs=opus": "ogg",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
}
FALLBACK_EXT = {"image": "jpg", "video": "mp4", "audio": "ogg"}

logger = logging.getLogger(__name__)


def media_root() -> Path:
    return Path(settings.media_root)


def slugify(text: str | None, max_len: int = 60) -> str:
    """Filesystem-safe slug: ascii, lowercase, hyphen-separated."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-")


def _ext_for(item: MediaItem) -> str:
    mime = (item.mime_type or "").split(";")[0].strip()
    return EXT_FROM_MIME.get(item.mime_type or "") or EXT_FROM_MIME.get(mime) \
        or FALLBACK_EXT.get(item.media_type, "bin")


def storage_filename(item: MediaItem) -> str:
    ts = item.message_ts.strftime("%Y%m%d-%H%M%S") if item.message_ts else "unknown"
    chat = slugify(item.chat_or_thread or item.sender_name, 30) or "chat"
    return f"{ts}_{chat}_{item.message_ref[:8]}.{_ext_for(item)}"


def download_item(session: Session, item: MediaItem) -> bool:
    """Fetch one item's bytes from the bridge and store them. Returns success.
    Mutates the row (status/storage_path/sha256/skip_reason) but does not commit."""
    dest_dir = media_root() / item.source
    if item.message_ts:
        dest_dir = dest_dir / item.message_ts.strftime("%Y") / item.message_ts.strftime("%m")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / storage_filename(item)

    try:
        with httpx.stream(
            "GET", f"{BRIDGE_URL}/download/{item.message_ref}", timeout=120.0
        ) as r:
            r.raise_for_status()
            sha = hashlib.sha256()
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
                    sha.update(chunk)
    except Exception as e:
        dest.unlink(missing_ok=True)
        detail = str(e)
        if isinstance(e, httpx.HTTPStatusError):
            try:
                detail = f"{e.response.status_code}: {e.response.read().decode()[:200]}"
            except Exception:
                detail = str(e.response.status_code)
        item.status = "failed"
        item.skip_reason = f"download failed: {detail}"
        logger.warning(f"[media] download failed id={item.id} ref={item.message_ref}: {detail}")
        return False

    item.status = "stored"
    item.storage_path = str(dest)
    item.sha256 = sha.hexdigest()
    item.size_bytes = dest.stat().st_size
    item.downloaded_at = datetime.now(timezone.utc)
    item.skip_reason = None
    return True


def download_pending(
    session: Session, limit: int = AUTO_DOWNLOAD_BATCH, *, user_id: int | None = None,
) -> dict:
    """Auto-download recent 'indexed' items (scheduled-sync path). Commits per
    item so a crash mid-batch keeps what it got.

    `user_id` restricts the batch to one user's items (the `media_sync` tool
    passes the caller's — 2026-09-06 scoping audit). `None` is the unbound
    scheduled sync, which downloads every user's recent window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUTO_DOWNLOAD_WINDOW_DAYS)
    q = session.query(MediaItem).filter(
        MediaItem.status == "indexed",
        MediaItem.message_ts >= cutoff,
    )
    if user_id is not None:
        q = q.filter(MediaItem.user_id == user_id)
    items = (
        q.order_by(MediaItem.message_ts.desc())
        .limit(limit)
        .all()
    )
    ok = failed = 0
    for item in items:
        if download_item(session, item):
            ok += 1
        else:
            failed += 1
        session.commit()
    return {"attempted": len(items), "stored": ok, "failed": failed}


def export_to_vault(item: MediaItem, vault_dir: Path, name_hint: str | None = None) -> Path:
    """Copy a stored item into the vault with a readable filename. Returns the
    destination path. Caller resolves vault_dir via vault_paths and commits."""
    if item.status != "stored" or not item.storage_path:
        raise ValueError(f"media item {item.id} has no stored bytes (status={item.status})")
    src = Path(item.storage_path)
    if not src.exists():
        raise FileNotFoundError(f"media item {item.id} storage_path missing: {src}")

    ts = item.message_ts.strftime("%H%M") if item.message_ts else "0000"
    hint = slugify(name_hint or item.caption or item.sender_name, 80) or item.message_ref[:8]
    dest = vault_dir / f"{ts}-{hint}.{_ext_for(item)}"
    # Never overwrite — suffix on collision (two snags same minute + caption)
    n = 2
    while dest.exists():
        dest = vault_dir / f"{ts}-{hint}-{n}.{_ext_for(item)}"
        n += 1
    vault_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest
