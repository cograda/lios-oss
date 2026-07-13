"""Inbox ingestion endpoint for the automation webhook pipeline.

Automation platform (Cloudflare tunnel) → POST /api/inbox/ingest

Minimal contract: a `filename` and a base64-encoded `data` body. Anything else
the caller wants to send goes in `metadata` and gets dumped to a sidecar JSON
file. Files land in `/inbox/incoming/` (or `/inbox/<type>/` if the caller
supplies a known type hint), then a downstream worker classifies and routes
them onward (corpus ingest, vault drop, finance import, etc.)."""

import base64
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, Response

from app.config import HomeSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox", tags=["inbox"])

settings = HomeSettings()

# Optional `type` field still routes to /inbox/<type>/ when valid; otherwise
# everything lands in /inbox/incoming/ and a downstream worker classifies by
# extension or content sniffing.
KNOWN_TYPES = {"audio", "image", "text", "file"}
DEFAULT_BUCKET = "incoming"

# 50 MB cap — base64 payloads inflate ~33%, so request body up to ~67 MB.
MAX_DECODED_BYTES = 50 * 1024 * 1024


def _check_auth(request: Request) -> bool:
    """Verify bearer token against inbox token or UI token."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        return False
    from app.auth.utils import safe_token_check

    if settings.inbox_token and safe_token_check(token, settings.inbox_token):
        return True
    if settings.ui_token and safe_token_check(token, settings.ui_token):
        return True
    return False


@router.post("/ingest")
async def ingest(request: Request):
    """Receive a base64-encoded file and write it to the inbox.

    Required:
      - `data` (or `body_base64`): base64-encoded file bytes

    Optional:
      - `filename`: original filename (extension preserved for the worker)
      - `type`: routing hint (audio|image|text|file). Unknown values fall
        through to /inbox/incoming/ rather than 400-ing.
      - `metadata`: arbitrary JSON; dumped to a `.meta.json` sidecar.
    """
    if not _check_auth(request):
        return Response(
            content='{"error": "Unauthorized"}',
            status_code=401,
            media_type="application/json",
        )

    try:
        body = await request.json()
    except Exception:
        return Response(
            content='{"error": "Invalid JSON"}',
            status_code=400,
            media_type="application/json",
        )

    # A bare JSON scalar (`null`, a string, a number) parses fine but isn't a
    # dict — guard before `.get()` so a malformed webhook payload returns a clear
    # 400 instead of an AttributeError 500.
    if not isinstance(body, dict):
        return Response(
            content='{"error": "Body must be a JSON object with a `data` field"}',
            status_code=400,
            media_type="application/json",
        )

    # `data` (preferred) or legacy `data` field. `body_base64` accepted as an
    # alias so workflow authors don't have to remember which key it was.
    data_b64 = body.get("data") or body.get("body_base64")
    if not data_b64:
        return Response(
            content='{"error": "Missing data field (expected `data` or `body_base64`)"}',
            status_code=400,
            media_type="application/json",
        )

    try:
        file_bytes = base64.b64decode(data_b64, validate=False)
    except Exception:
        return Response(
            content='{"error": "Invalid base64 data"}',
            status_code=400,
            media_type="application/json",
        )

    if len(file_bytes) > MAX_DECODED_BYTES:
        return Response(
            content=f'{{"error": "File too large ({len(file_bytes)} > {MAX_DECODED_BYTES} bytes)"}}',
            status_code=413,
            media_type="application/json",
        )

    # Filename → strip any directory component so a malicious basename can't
    # escape the inbox dir, and keep the extension for downstream dispatch.
    raw_filename = (body.get("filename") or "").strip()
    safe_basename = Path(raw_filename).name if raw_filename else ""
    ext = Path(safe_basename).suffix if safe_basename else ""

    # Optional `type` is kept as a hint only; unknown values fall through to
    # the default bucket rather than 400-ing.
    file_type = (body.get("type") or "").lower()
    bucket = file_type if file_type in KNOWN_TYPES else DEFAULT_BUCKET

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    short_id = uuid.uuid4().hex[:8]
    dest_name = f"{ts}-{short_id}{ext}" if ext else f"{ts}-{short_id}"

    inbox_dir = Path(settings.inbox_path) / bucket
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest_path = inbox_dir / dest_name

    dest_path.write_bytes(file_bytes)

    # Always write a sidecar so the worker can recover the original filename
    # (which the uuid-rewrite throws away) and any provenance hints the sender included.
    import json

    metadata = body.get("metadata") or {}
    meta_path = dest_path.with_suffix(dest_path.suffix + ".meta.json")
    meta_path.write_text(json.dumps({
        "original_filename": safe_basename or None,
        "type_hint": file_type or None,
        "source": metadata.get("source") if isinstance(metadata, dict) else None,
        "note": metadata.get("note") if isinstance(metadata, dict) else None,
        "extra": metadata if isinstance(metadata, dict) else metadata,
        "ingested_at": datetime.now().isoformat(),
    }, indent=2, default=str))

    size_kb = len(file_bytes) / 1024
    logger.info(f"Inbox: ingested {bucket}/{dest_name} ({size_kb:.1f} KB)")

    return {
        "ok": True,
        "bucket": bucket,
        "filename": dest_name,
        "original_filename": safe_basename or None,
        "size_bytes": len(file_bytes),
        "path": str(dest_path),
    }
