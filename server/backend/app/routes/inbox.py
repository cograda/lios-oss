"""Inbox ingestion endpoint for the Tines webhook pipeline.

Producers (Cloudflare/Tines tunnel from a phone, the Mac voice-memo watcher, or
anything else) → POST /api/inbox/ingest

Every producer is dumb transport: it moves bytes and nothing else. Classifying
the file, reading its duration, transcribing audio and routing the result are all
server-side. Minimal contract: a `filename` and a base64-encoded `data` body. Anything else
the caller wants to send goes in `metadata` and gets dumped to a sidecar JSON
file. Files land in `/inbox/u<user_id>/incoming/` (or `/inbox/u<user_id>/<type>/`
if the caller supplies a known type hint) — per-user since F6 (2026-08-08), see
`_resolve_caller_user_id` for how `<user_id>` is determined — then a downstream
worker classifies and routes them onward (corpus ingest, vault drop, finance
import, etc.)."""

import base64
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request, Response

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


def _resolve_caller_user_id(request: Request) -> int | None:
    """Resolve the bearer to an owning user_id, or None if unauthenticated.

    Three bearer kinds, two different identity strengths:

      - A per-user `client_tokens` bearer (comar-client daemon, added
        2026-07-31 for pushed voice memos) carries real per-user identity —
        resolved to whichever user the token belongs to.
      - The shared `inbox_token` (Tines webhook secret) and the UI token
        (`HOME_UI_TOKEN`) authenticate a *request*, not a *user* — neither
        was ever minted per-person. F6 (2026-08-08) made the inbox per-user,
        so something has to own files ingested through these two paths;
        both are attributed to `InboxFacade.default_user_id()`
        (`scan.LEGACY_OWNER_USER_ID`, i.e. Alex) — the same "existing/
        ambiguous ownership belongs to user 1" convention used everywhere
        else data got split (vault_chunks, whatsapp_contacts, ...). This
        route was already documented as staying request-shared rather than
        per-user; that's now true only for these two auth paths, not for the
        daemon's.
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        return None

    from app.auth.client_token import resolve_token_to_user

    user = resolve_token_to_user(token)
    if user is not None:
        return user.id

    from app.auth.utils import safe_token_check
    from app.integrations.inbox.facade import FACADE as inbox_facade
    from app.plugin.config_store import plugin_config

    inbox_token = plugin_config("inbox").inbox_token
    if inbox_token and safe_token_check(token, inbox_token):
        return inbox_facade.default_user_id()
    if settings.ui_token and safe_token_check(token, settings.ui_token):
        return inbox_facade.default_user_id()

    return None


@router.post("/ingest")
async def ingest(request: Request, background_tasks: BackgroundTasks):
    """Receive a base64-encoded file and write it to the inbox.

    Required:
      - `data` (or `body_base64`): base64-encoded file bytes

    Optional:
      - `filename`: original filename (extension preserved for the worker)
      - `type`: routing hint (audio|image|text|file). Unknown values fall
        through to /inbox/incoming/ rather than 400-ing.
      - `metadata`: arbitrary JSON; dumped to a `.meta.json` sidecar.
        `metadata.note` is special — it's treated as the human-meaningful
        description of the item and leads the rendered `summary`.

    **Callers do not transcribe.** Audio is transcribed server-side by the
    `transcription` integration — so a producer (an iOS Shortcut, a Tines
    relay, the Mac watcher) only has to move bytes. Setting `metadata.note`
    yourself is still honoured and takes precedence, but it isn't expected: it
    exists for a caller that happens to already have the text.

    **Transcription starts in the background before this returns** (Task C of
    the Tines retirement): an audio/video file gets a `BackgroundTasks` entry
    that kicks off `transcribe_file_task` for THIS file immediately, so a
    capture is usually readable within roughly the time the model call takes
    rather than up to five minutes later. This never delays or risks the
    response below — the file and its sidecar are already durably on disk,
    and a background-task failure (see `scan.transcribe_file_task`) is logged
    and otherwise invisible to the caller. The `*/5 * * * *` cron
    (`transcribe_pending`) keeps running unchanged as the sweeper/retry net —
    it still catches anything this background task missed (app restart mid-
    flight, transcription unconfigured at ingest time, a producer that writes
    straight to the filesystem and never calls this route) — and the two are
    safe to race on the same file: see `scan._acquire_transcription_lock`.

    Returns immediately with a `summary` describing what landed ("voice note,
    1m 47s") — the transcript necessarily arrives later, announced separately via
    `notify.push` (and, for audio, emailed too). Also returns
    `kind`/`preview`/`preview_meta` for callers that would rather compose
    their own message.
    """
    caller_user_id = _resolve_caller_user_id(request)
    if caller_user_id is None:
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
    # dict — guard before `.get()` so a malformed Tines payload returns a clear
    # 400 instead of an AttributeError 500.
    if not isinstance(body, dict):
        return Response(
            content='{"error": "Body must be a JSON object with a `data` field"}',
            status_code=400,
            media_type="application/json",
        )

    # `data` (preferred), with `body_base64` and `audio` accepted as aliases so
    # Tines authors don't have to remember which key it was. `audio` is what the
    # existing Dictator shortcut already posts, so honouring it means that
    # shortcut can be repointed here by changing only the URL and auth header.
    data_b64 = body.get("data") or body.get("body_base64") or body.get("audio")
    if not data_b64:
        return Response(
            content='{"error": "Missing data field (expected `data` or `body_base64`)"}',
            status_code=400,
            media_type="application/json",
        )

    try:
        # Strip whitespace before decoding: an iOS Shortcut's base64 output can
        # arrive line-wrapped, which `validate=False` tolerates but which the
        # existing Tines story had to REGEX_REPLACE out by hand.
        if isinstance(data_b64, str):
            data_b64 = "".join(data_b64.split())
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

    # Deduplicate by content BEFORE writing anything.
    #
    # Two independent producers reach this endpoint — a phone posting via the
    # Tines tunnel when out of the house, and the Mac watcher when it's running —
    # and neither knows what the other has sent. The same recording arriving
    # twice would otherwise be stored twice and, worse, transcribed twice at
    # cost. Returning the existing item makes a re-post harmless and idempotent,
    # so a producer that isn't sure whether it already sent something can just
    # send it.
    digest = ""
    try:
        from app.integrations.inbox.facade import FACADE as inbox_facade

        digest = inbox_facade.content_hash(file_bytes)
        existing = inbox_facade.find_by_hash(digest, caller_user_id)
        if existing is not None:
            logger.info(f"Inbox: duplicate of {existing.name} — not re-ingesting")
            return {
                "ok": True,
                "duplicate": True,
                "filename": existing.name,
                "path": str(existing),
                "size_bytes": len(file_bytes),
                "summary": inbox_facade.summary_for(existing, size_bytes=len(file_bytes)),
            }
    except Exception:  # noqa: BLE001
        # A failed dedup check must not reject a genuine capture — worst case is
        # a duplicate, which is recoverable; a dropped voice note is not.
        logger.exception("Inbox: dedup check failed, continuing with ingest")

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

    # F6: write into the caller's own subtree, not the flat inbox root —
    # reached via the facade, since this module is kernel code and
    # `tests/test_kernel_import_guard.py` permits kernel → integration
    # crossings only through `<pkg>.facade`.
    from app.integrations.inbox.facade import FACADE as inbox_facade

    inbox_dir = inbox_facade.bucket_dir_for(caller_user_id, bucket)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest_path = inbox_dir / dest_name

    dest_path.write_bytes(file_bytes)

    inbox_facade.record_ingest(caller_user_id, bucket, dest_name, sha256=digest or None)

    # Always write a sidecar so the worker can recover the original filename
    # (which the uuid-rewrite throws away) and any provenance hints Tines sent.
    import json

    metadata = body.get("metadata") or {}
    meta_path = dest_path.with_suffix(dest_path.suffix + ".meta.json")
    meta_path.write_text(json.dumps({
        "original_filename": safe_basename or None,
        # Recorded so `find_by_hash` can recognise a re-post from the other
        # producer later, including after this file is routed and archived.
        "sha256": digest or None,
        "type_hint": file_type or None,
        "source": metadata.get("source") if isinstance(metadata, dict) else None,
        "note": metadata.get("note") if isinstance(metadata, dict) else None,
        "extra": metadata if isinstance(metadata, dict) else metadata,
        "ingested_at": datetime.now().isoformat(),
    }, indent=2, default=str))

    # Enrich inline rather than waiting for the hourly `sync_inbox` cron.
    #
    # This response is the ONLY material the Tines flow has for its ntfy
    # confirmation, and it used to carry just a generated filename plus a byte
    # count — so every confirmation read identically whether you'd captured a
    # voice note, a screenshot or a PDF. Enrichment is cheap (a magic-byte
    # sniff, plus a header read for duration/dimensions or a first-page text
    # extract) and `enrich_one` is idempotent, so the cron re-running is a
    # no-op.
    #
    # Best-effort by design: the file is already safely on disk by this point,
    # so a preview failure must degrade the response, never fail the ingest and
    # make Tines retry a capture that actually succeeded.
    # Reached via the package facade, not `inbox.scan` directly: this module is
    # kernel code, and `tests/test_kernel_import_guard.py` permits kernel →
    # integration crossings only through `<pkg>.facade`.
    meta: dict = {}
    try:
        from app.integrations.inbox.facade import FACADE as inbox_facade

        meta, summary = inbox_facade.enrich_for_response(
            dest_path, size_bytes=len(file_bytes)
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"Inbox: enrichment failed for {dest_name} (file kept)")
        summary = f"file, {len(file_bytes) / 1024:.0f} KB"

    size_kb = len(file_bytes) / 1024
    logger.info(
        f"Inbox: ingested {bucket}/{dest_name} ({size_kb:.1f} KB) — {summary}"
    )

    # Announce the capture from here rather than leaving it to the caller.
    #
    # A relay (the Tines story) can push `summary` from the response, which is how
    # this has always worked — but a producer posting directly, like an iOS
    # Shortcut, can move bytes and nothing else. Doing it server-side is what lets
    # the relay be removed: transcription came in-house on 2026-07-31, and the
    # tunnel is replaceable by the tailnet address the phone already uses for
    # Health Auto Export, leaving this as the relay's last remaining job.
    #
    # Off unless `inbox_confirm_push` is set, so both paths pushing at once during
    # the transition can't double-announce every capture. Never raises — see the
    # facade method.
    inbox_facade.confirm_ingest(meta, dest_path)
    # …and the durable copy. Separate call rather than folded into
    # `confirm_ingest` because the two answer to different config and
    # different failure modes — see `scan.email_ingested_document`.
    inbox_facade.email_ingested_document(meta, dest_path)

    # Task C: kick off transcription now for audio/video rather than waiting
    # for the cron. `meta["kind"]` comes from `enrich_for_response` above (a
    # magic-byte sniff, already done), so this costs nothing extra to check.
    # See the route docstring and `scan.transcribe_file_task` for the
    # cron-race guard.
    if meta.get("kind") in ("audio", "video"):
        inbox_facade.transcribe_in_background(background_tasks, dest_path)

    return {
        "ok": True,
        "bucket": bucket,
        "filename": dest_name,
        "original_filename": safe_basename or None,
        "size_bytes": len(file_bytes),
        "path": str(dest_path),
        # Everything below is for the caller's confirmation message. `summary`
        # is pre-rendered and safe to use verbatim as an ntfy body; the rest is
        # there so a caller can compose its own line instead.
        "summary": summary,
        "kind": meta.get("kind"),
        "preview": meta.get("preview") or "",
        "preview_meta": meta.get("preview_meta") or {},
        "note": meta.get("note"),
    }
