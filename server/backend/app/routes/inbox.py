"""Inbox ingestion endpoint for the capture pipeline.

Producers (the iOS/macOS Shortcuts over the Cloudflare tunnel, the lios-sync
daemon, or anything else holding a per-user bearer) → POST /api/inbox/ingest

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
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.auth.cf_access import check_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox", tags=["inbox"])

# Optional `type` field still routes to /inbox/<type>/ when valid; otherwise
# everything lands in /inbox/incoming/ and a downstream worker classifies by
# extension or content sniffing.
KNOWN_TYPES = {"audio", "image", "text", "file"}
DEFAULT_BUCKET = "incoming"

# 50 MB cap — base64 payloads inflate ~33%, so request body up to ~67 MB.
MAX_DECODED_BYTES = 50 * 1024 * 1024

# lios#198/#191 — the Dictator ("record a voice memo, file in Comar?
# Yes/No") Shortcut packs the caller's answer into a `comar` key. It arrives
# as a plain **top-level** body key, not nested — `core/scripts/
# build_shortcuts.py`'s `KEY_RENAMES` comment names it (alongside `email`,
# `file_size`, `file_created`) as one of the keys the Shortcut posts
# unrenamed, and until this fix it was "simply ignored by routes/inbox.py":
# every capture was filed regardless of the answer, so answering "No" made
# no observable difference and two real work memos leaked into the inbox on
# 2026-09-10. A hypothetical producer nesting it under `metadata.comar`
# instead (the shape the *issue* describing this bug used informally) is
# honoured too, so `_wants_filed` checks both places.
#
# Liberal, documented falsy parsing: `False`, a numeric `0`, and any of
# these case-insensitive strings all mean "don't file — transcribe/describe
# it and email the result instead". A MISSING key — every producer that
# predates this field, and any future one that never asks the question —
# keeps today's default of filing, so nothing else changes behaviour.
_COMAR_FALSY_STRINGS = {"false", "no", "0", "off", "n"}


def _wants_filed(body: dict) -> bool:
    """Whether the caller's `comar` answer means "yes, file this" (the
    default).

    Checks the top-level `body["comar"]` key first — the real wire shape
    the Dictator Shortcut sends — then falls back to `body["metadata"]
    ["comar"]` for a producer that nests it. True unless a value is found
    and it's recognisably falsy (see `_COMAR_FALSY_STRINGS`); the key
    missing everywhere, present but not a recognised type, or `body`/
    `metadata` not being what's expected all default to True so a producer
    that doesn't send this key is unaffected.
    """
    value = body.get("comar")
    if value is None:
        metadata = body.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("comar")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in _COMAR_FALSY_STRINGS
    return True

# F-security: the ceiling on the *raw request body* the caller may send —
# base64 inflation (4/3) plus generous slack for the surrounding JSON
# (filename, metadata, quoting) that isn't part of `data` itself. This is
# checked BEFORE the body is parsed or even fully read (see `_read_body_capped`
# and the `Content-Length` check in `ingest()`); `MAX_DECODED_BYTES` above is
# still enforced afterwards, on the actual decoded bytes, as belt-and-braces.
MAX_REQUEST_BYTES = int(MAX_DECODED_BYTES * 4 / 3) + 4096


async def _read_body_capped(request: Request) -> bytes | None:
    """Read the request body in bounded chunks via `request.stream()`.

    `await request.json()` (or `.body()`) buffers the *entire* body into
    memory before anything gets a chance to look at its size — so a caller
    with no (or a lying) `Content-Length` could force a multi-hundred-MB
    allocation before `MAX_DECODED_BYTES` was ever checked. Streaming with a
    running cap means a request over the limit is aborted as soon as the
    cap is crossed, never fully buffered. Returns `None` if the cap is
    exceeded partway through.
    """
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_REQUEST_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _resolve_caller_user_id(request: Request) -> int | None:
    """Resolve the bearer to an owning user_id, or None if unauthenticated.

    One bearer kind, one identity strength: a per-user `client_tokens` row
    (`app/auth/client_token.py`), resolved to whichever user it belongs to.

    **2026-09-06 — one credential: the per-user bearer.** Until this date the
    route also accepted two *shared* secrets — the inbox integration's
    `inbox_token` (the retired Tines relay's webhook secret) and the dashboard
    `HOME_UI_TOKEN` — and attributed anything they ingested to user 1, because
    neither was ever minted per person. Nothing live presented either: the
    Tines story was deleted 2026-08-29, and every capture Shortcut and the
    lios-sync daemon carry a per-user bearer (`scripts/build_shortcuts.py`,
    `server/docs/capture-clients.md`). A secret that authenticates a *request*
    rather than a *person* has no place on a per-user inbox, so both branches
    are gone: an unknown bearer is a 401, never "probably Alex".
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        return None

    from fastapi import HTTPException

    from app.auth.client_token import readonly_http_refusal, resolve_token_to_user

    user = resolve_token_to_user(token)
    if user is None:
        return None
    # Scope (2026-09-07): ingest is a write. This route does not go through
    # `get_current_user`, so the same refusal is applied here by hand — one
    # message, one rule (`readonly_http_refusal`), two call sites.
    refusal = readonly_http_refusal(user, request.method, request.url.path)
    if refusal is not None:
        raise HTTPException(status_code=403, detail=refusal)
    return user.id


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
      - `comar`: a top-level key, also special (lios#198/#191) — NOT nested
        under `metadata` (`metadata.comar` is honoured too, as a fallback,
        but the Dictator Shortcut sends this flat, alongside `email`). A
        falsy value — `False`, `0`, or the case-insensitive strings
        "false"/"no"/"0"/"off"/"n" (see `_wants_filed`) — means "don't file
        this". The Dictator Shortcut asks "file in Comar? Yes/No" and packs
        the answer here. A falsy answer transcribes/describes the capture
        and emails the result to the caller in the background, then
        discards it: no
        `InboxItem` row, no sidecar, no push notification, nothing left in
        the inbox tree at all (see `InboxFacade.deliver_by_email_only`).
        Missing, absent, or any other value means "yes, file it" — today's
        unchanged default, so a producer that never sends this key sees no
        behaviour change.

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
    # lios#139: a `CF-Access-Client-Id` allow-list was added here and reverted
    # the same evening (PR #64/#65) — Cloudflare Access does NOT forward the
    # service token's client id to the origin. What it does forward is a
    # signed JWT in `Cf-Access-Jwt-Assertion` (iss = the team domain, aud =
    # the app's AUD tag, keys at <team>.cloudflareaccess.com/cdn-cgi/access/
    # certs), which `app/auth/cf_access.py` verifies instead. Both
    # `HOME_CF_ACCESS_TEAM_DOMAIN`/`HOME_CF_ACCESS_AUD` unset (the default) is
    # a no-op; set, this runs BEFORE the route's own per-user bearer check
    # below, as an additional gate rather than a replacement for it.
    cf_access_error = check_request(request.headers)
    if cf_access_error is not None:
        return Response(
            content=f'{{"error": "{cf_access_error}"}}',
            status_code=401,
            media_type="application/json",
        )

    caller_user_id = _resolve_caller_user_id(request)
    if caller_user_id is None:
        return Response(
            content='{"error": "Unauthorized"}',
            status_code=401,
            media_type="application/json",
        )

    # F-security: reject an oversized request BEFORE buffering it. A
    # declared Content-Length over the ceiling never gets read at all; a
    # missing/lying one is caught by the streamed, capped read below —
    # either way `request.json()` never gets to allocate the whole body
    # first and check its size second.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > MAX_REQUEST_BYTES:
            return Response(
                content=(
                    f'{{"error": "Request too large ({declared_length} > '
                    f'{MAX_REQUEST_BYTES} bytes)"}}'
                ),
                status_code=413,
                media_type="application/json",
            )

    raw_body = await _read_body_capped(request)
    if raw_body is None:
        return Response(
            content=f'{{"error": "Request too large (> {MAX_REQUEST_BYTES} bytes)"}}',
            status_code=413,
            media_type="application/json",
        )

    try:
        body = json.loads(raw_body)
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

    # lios#198/#191 — "file in Comar?" answered No (a falsy `comar` key).
    # This branch never touches the inbox tree at all: no `InboxItem` row, no
    # sidecar, no dedup entry, no push. The file is spooled to a scratch
    # location OUTSIDE the inbox tree (`scan.spool_capture`, reached only via
    # the facade below), transcribed/described and emailed to the caller as a
    # background task, then deleted — see `InboxFacade.deliver_by_email_only`
    # and `scan.deliver_capture_by_email` for the full contract. Must run
    # before the dedup/filing logic below, which this path skips entirely.
    if not _wants_filed(body):
        raw_filename = (body.get("filename") or "").strip()
        safe_basename = Path(raw_filename).name if raw_filename else ""

        from app.integrations.inbox.facade import FACADE as inbox_facade

        kind, summary = inbox_facade.deliver_by_email_only(
            background_tasks, file_bytes,
            owner_user_id=caller_user_id, original_filename=safe_basename or None,
        )
        logger.info(
            f"Inbox: email-only capture ({len(file_bytes) / 1024:.1f} KB, "
            f"kind={kind}) for user {caller_user_id} — not filed (comar=false)"
        )
        return {
            "ok": True,
            "filed": False,
            "bucket": None,
            "filename": None,
            "original_filename": safe_basename or None,
            "size_bytes": len(file_bytes),
            "path": None,
            "summary": summary,
            "kind": kind,
            "preview": "",
            "preview_meta": {},
            "note": None,
        }

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
    # (`json` is now a module-level import — a local `import json` here used
    # to make the name local for the WHOLE function, including the early
    # `json.loads(raw_body)` above, which raised UnboundLocalError and was
    # swallowed by that call's broad `except Exception`.)
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
        "filed": True,
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
