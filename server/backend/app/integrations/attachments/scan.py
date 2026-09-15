"""Scan Gmail + WhatsApp messages for unprocessed attachments.

Populates `message_attachments` rows. A row lands 'pending' only when its
source is one `ingest.py` can actually handle (`sources.SUPPORTED_INGEST_SOURCES`,
the single set both this module and `ingest.py` read) AND its mimetype has a
parser — anything else lands 'unsupported' with a reason. Both WhatsApp and
Gmail are in that set (Gmail since 2026-09-07); rows queued `unsupported`
for the source before that are flipped by `reevaluate_unsupported_gmail_rows`
below. No downloads happen here either way — that's user-gated in
`ingest.py`.

WhatsApp: everything we need (filename, mimetype, size, URL+keys) is already in
`whatsapp_messages.raw_json` — zero API calls.

Gmail: `google_mail/sync.py`'s regular sync fetches messages with
`format='metadata'`, which strips `payload.parts` — so the routine mail cache
has no attachment filename/mimetype info to scan. `scan_gmail` below does a
bounded, checkpointed full-format backfill instead of relying on that cache:
per connected account it lists `has:attachment` candidates (id-only, cheap),
skips ones already recorded, and refetches only the new ones with
`format='full'` (see `google_mail/client.py::fetch_messages_full` +
`_walk_attachment_parts`) to recover filename/mimetype/size/attachmentId. Two
passes per call, both against the SAME `has:attachment` query so pagination
stays a single Gmail result set:

  - catch-up: always re-checks page 1 (newest first) so attachments arriving
    since the last scan are picked up immediately, however far along the
    backfill below is.
  - backfill: continues from a persisted Gmail `nextPageToken` (stored in
    `SyncState`, see `_gmail_cursor_key`), walking one page further into
    history per call. Once a page returns no further token, that account's
    backfill is marked done and only the catch-up pass runs afterwards.

Both passes de-duplicate against existing `message_attachments` rows before
spending a `format='full'` call, so re-running `attachments_scan` (the tool
calls this every time it's invoked) is cheap and idempotent — no duplicate
rows, no repeat full-format fetches for messages already recorded.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.errors import PermanentError, TransientError
from app.integrations.attachments.models import MessageAttachment
from app.integrations.attachments.sources import (
    SUPPORTED_INGEST_SOURCES,
    unsupported_source_reason,
)

# WhatsApp media expires from CDN URLs (the `oe` parameter is a hard timestamp)
# and the re-upload path (sock.updateMediaMessage) only succeeds while the
# *sender's* device still has the bytes. Empirically:
#   - outbound (fromMe=true): unrecoverable after ~3 weeks
#   - inbound: unrecoverable after ~30 days (sender's WA client has rotated)
# Pre-skipping these keeps rows out of 'pending' purgatory and avoids wasting
# bridge connection attempts that will all 403 on the CDN.
OUTBOUND_EXPIRY_DAYS = 21
INBOUND_EXPIRY_DAYS = 30

logger = logging.getLogger(__name__)


# Keep this aligned with parsers we actually have in historical_corpus.parsers.
# Anything else lands as 'unsupported' — user still sees the row but can't ingest.
PARSEABLE_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/msword",
    "application/vnd.ms-excel",
}


def _discovery_status(source: str, mime_type: str | None) -> tuple[str, str | None]:
    """(parse_status, skip_reason) for a newly-discovered attachment.

    Two independent gates, checked in order: a source `ingest.py` can't
    handle at all (SUPPORTED_INGEST_SOURCES, shared with ingest.py — see
    sources.py) beats a mimetype we have no parser for. Either way the row
    is queued 'unsupported', not 'pending' — 'pending' means "will be
    consumed if approved".
    """
    if source not in SUPPORTED_INGEST_SOURCES:
        return "unsupported", unsupported_source_reason(source)
    if mime_type not in PARSEABLE_MIMES:
        return "unsupported", f"mimetype {mime_type!r} not parseable"
    return "pending", None


def reevaluate_unsupported_gmail_rows(session: Session, *, dry_run: bool = False) -> dict:
    """One-off re-evaluation of Gmail rows queued `unsupported` for their SOURCE.

    Before 2026-09-07 `gmail` was not in `SUPPORTED_INGEST_SOURCES`, so every
    Gmail attachment scan_gmail discovered (1,189 in production) was written
    `unsupported` with `unsupported_source_reason('gmail')` regardless of
    mimetype. Now that ingest can download them, each of those rows gets the
    verdict a fresh discovery would give it — `_discovery_status('gmail',
    mime_type)` — so parseable mimetypes become `pending` and the rest stay
    `unsupported` with the *mimetype* reason instead.

    Idempotent by construction: the selection is `skip_reason ==
    unsupported_source_reason('gmail')` and every row touched leaves that
    reason behind, so a second run selects nothing. Rows in any other status
    or with any other reason (ingested, failed, hand-edited, mimetype-
    unsupported) are never selected. Never deletes.

    Runs unscoped on purpose — this is a data repair over every owner's rows,
    each of which keeps its own `user_id`; it is not a tool handler.
    Callable from `app.scripts.reevaluate_gmail_attachments` (dry-run by
    default there).
    """
    source_reason = unsupported_source_reason("gmail")
    rows = (
        session.query(MessageAttachment)
        .filter(
            MessageAttachment.source == "gmail",
            MessageAttachment.parse_status == "unsupported",
            MessageAttachment.skip_reason == source_reason,
        )
        .order_by(MessageAttachment.id)
        .all()
    )

    now_pending = still_unsupported = 0
    for row in rows:
        status, reason = _discovery_status("gmail", row.mime_type)
        # `_discovery_status` can only hand back the source reason if gmail
        # left the shared set again — in which case there is nothing to
        # re-evaluate and rewriting the same value would fake a change.
        if reason == source_reason:
            continue
        if status == "pending":
            now_pending += 1
        else:
            still_unsupported += 1
        if not dry_run:
            row.parse_status = status
            row.skip_reason = reason

    if not dry_run and (now_pending or still_unsupported):
        session.commit()

    return {
        "source": "gmail",
        "selected": len(rows),
        "now_pending": now_pending,
        "still_unsupported": still_unsupported,
        "dry_run": dry_run,
    }


def _extract_document_meta(raw: dict) -> dict | None:
    """Pull filename + mimetype + size from a Baileys documentMessage payload.

    Baileys wraps the actual document in `message.documentMessage`; filename
    lives as `fileName`, size as `fileLength`. Both may be strings when
    serialised through JSON.
    """
    msg = raw.get("message") or {}
    doc = msg.get("documentMessage")
    if not doc:
        return None
    size = doc.get("fileLength")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    return {
        "filename": doc.get("fileName") or doc.get("title"),
        "mime_type": doc.get("mimetype"),
        "size_bytes": size,
    }


def scan_whatsapp(session: Session, user_id: int | None = None) -> dict:
    """Insert MessageAttachment rows for every WhatsApp document we don't already track.

    Idempotent: the `uq_msg_attachment` constraint collapses re-scans. Returns
    counts by status so the caller can surface progress.

    `user_id` scopes the scan to one user's `whatsapp_messages` — the MCP
    tool passes the caller's, because a tool one user invokes must not walk
    the other user's private messages (2026-09-06 scoping audit). `None` is
    the scheduled-sync shape: it runs with no bound user and attributes each
    row to its owner via `w.user_id`, so it stays household-wide.
    """
    # Select only rows we haven't already recorded. Doing this in SQL (rather
    # than iterating all 328 documents) keeps incremental scans cheap after
    # the initial backfill. The `w.user_id = :uid` predicate is the same shape
    # as snags/capture.py and household/capture.py.
    #
    # The anti-join is per USER, not per message id (2026-09-07). With two
    # bridges a message both people hold — the family group chat — exists as
    # two `whatsapp_messages` rows with the same `message_id` and different
    # `user_id`. Joining on `message_ref` alone made the attachment "owned by
    # whoever scanned first": the second user's row matched the first user's
    # attachment and was never discovered, so `attachments_pending` showed it
    # to one person only. `uq_msg_attachment` is already
    # (user_id, source, message_ref, filename), so one row per user is the
    # intended shape — the join just has to ask the same question.
    uid_clause = "AND w.user_id = :uid" if user_id is not None else ""
    params = {"uid": user_id} if user_id is not None else {}
    sql = sa_text(f"""
        SELECT w.message_id, w.chat_name, w.sender_name, w.timestamp,
               w.media_caption, w.raw_json, w.is_from_me, w.user_id
          FROM whatsapp_messages w
          LEFT JOIN message_attachments a
            ON a.source = 'whatsapp'
           AND a.message_ref = w.message_id
           AND a.user_id = w.user_id
         WHERE w.message_type = 'document' AND a.id IS NULL
           {uid_clause}
    """)
    rows = session.execute(sql, params).all()

    now = datetime.now(timezone.utc)
    outbound_cutoff = now - timedelta(days=OUTBOUND_EXPIRY_DAYS)
    inbound_cutoff = now - timedelta(days=INBOUND_EXPIRY_DAYS)
    created = skipped_unsupported = skipped_malformed = skipped_outbound = skipped_inbound = 0
    for r in rows:
        try:
            raw = json.loads(r.raw_json) if r.raw_json else {}
        except (TypeError, ValueError):
            skipped_malformed += 1
            continue

        meta = _extract_document_meta(raw)
        if not meta:
            skipped_malformed += 1
            continue

        # Prefer raw filename; fall back to the media_caption the bridge stored,
        # which for documents is already set to documentMessage.fileName.
        filename = meta["filename"] or r.media_caption

        parseable = meta["mime_type"] in PARSEABLE_MIMES
        expired_outbound = (
            parseable and r.is_from_me and r.timestamp
            and r.timestamp < outbound_cutoff
        )
        expired_inbound = (
            parseable and not r.is_from_me and r.timestamp
            and r.timestamp < inbound_cutoff
        )
        if expired_outbound:
            status = "skipped"
            skip_reason = f"outbound older than {OUTBOUND_EXPIRY_DAYS}d — media likely unrecoverable"
        elif expired_inbound:
            status = "skipped"
            skip_reason = f"inbound older than {INBOUND_EXPIRY_DAYS}d — sender's CDN copy likely expired"
        else:
            status, skip_reason = _discovery_status("whatsapp", meta["mime_type"])

        session.add(MessageAttachment(
            # Inherit user_id from parent WhatsApp message (per Phase A
            # plan). Whatsapp_messages.user_id defaults to 1 today; second
            # bridge in Phase F will write user_id=2 explicitly.
            user_id=r.user_id,
            source="whatsapp",
            message_ref=r.message_id,
            filename=filename,
            mime_type=meta["mime_type"],
            size_bytes=meta["size_bytes"],
            sender_name=r.sender_name,
            chat_or_thread=r.chat_name,
            message_ts=r.timestamp,
            parse_status=status,
            skip_reason=skip_reason,
        ))
        if expired_outbound:
            skipped_outbound += 1
        elif expired_inbound:
            skipped_inbound += 1
        elif parseable:
            created += 1
        else:
            skipped_unsupported += 1

    session.commit()
    return {
        "source": "whatsapp",
        "new_pending": created,
        "unsupported": skipped_unsupported,
        "skipped_outbound_expired": skipped_outbound,
        "skipped_inbound_expired": skipped_inbound,
        "malformed": skipped_malformed,
    }


# Ids checked per pass, per account, per call (id-only list — cheap).
GMAIL_LIST_PAGE = 100
# New (not-already-recorded) messages actually refetched with format='full'
# per pass, per account, per call — this is the API-quota-bounded knob.
GMAIL_SCAN_BATCH = 25


def _gmail_cursor_key(user_id: int, account_email: str) -> str:
    """SyncState.integration key for one account's backfill page-token cursor.

    SyncState.integration is varchar(50); account emails can be long, so the
    key carries a short hash rather than the raw address. Follows the same
    "extra row in the shared sync_state table, keyed by a synthetic
    integration name" pattern as `lastfm/sync.py`'s backfill cursor — these
    rows are deliberately not among the registered integration names, so
    they don't show up in the dashboard's integration list.
    """
    h = hashlib.sha1(account_email.encode()).hexdigest()[:10]
    return f"gmail_attach:{user_id}:{h}"


def _load_gmail_cursor(session: Session, user_id: int, account_email: str) -> dict:
    from app.models.tokens import SyncState

    state = (
        session.query(SyncState)
        .filter_by(integration=_gmail_cursor_key(user_id, account_email))
        .first()
    )
    if not state or not state.last_error:
        return {"page_token": None, "backfill_done": False}
    try:
        cursor = json.loads(state.last_error)
    except (TypeError, ValueError):
        return {"page_token": None, "backfill_done": False}
    cursor.setdefault("page_token", None)
    cursor.setdefault("backfill_done", False)
    return cursor


def _save_gmail_cursor(session: Session, user_id: int, account_email: str, cursor: dict) -> None:
    from app.models.tokens import SyncState

    key = _gmail_cursor_key(user_id, account_email)
    state = session.query(SyncState).filter_by(integration=key).first()
    if not state:
        state = SyncState(integration=key)
        session.add(state)
    state.last_error = json.dumps(cursor)
    state.last_sync_status = "ok"
    state.last_sync_at = datetime.now(timezone.utc)


def _existing_gmail_refs(session: Session, user_id: int, message_ids: list[str]) -> set[str]:
    if not message_ids:
        return set()
    rows = (
        session.query(MessageAttachment.message_ref)
        .filter(
            MessageAttachment.user_id == user_id,
            MessageAttachment.source == "gmail",
            MessageAttachment.message_ref.in_(message_ids),
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _insert_gmail_attachments(
    session: Session, user_id: int, account_email: str, full_messages: list[dict],
) -> tuple[int, int]:
    """Insert MessageAttachment rows from full-format message dicts.

    Returns (created, unsupported). `storage_path` carries the Gmail
    `attachmentId` (not a local path — gmail ingest doesn't download yet;
    this keeps the id around for when it does, per the module's original
    design sketch: `users.messages.attachments.get(messageId, id)`).
    """
    created = unsupported = 0
    for msg in full_messages:
        ts = None
        if msg.get("internal_date"):
            ts = datetime.fromtimestamp(msg["internal_date"] / 1000, tz=timezone.utc)
        # One logical attachment can arrive as several MIME parts with the
        # same filename: a Google Calendar invite carries `invite.ics` twice,
        # as `text/calendar` AND `application/ics`. `uq_msg_attachment` is
        # (user_id, source, message_ref, filename) with no mime_type, so the
        # two parts collide *with each other* inside this transaction and the
        # flush aborts the entire Gmail pass — Gmail discovery never
        # completed for any user with a calendar invite in range.
        #
        # Keep the first part and drop the rest. Deliberately NOT fixed by
        # adding mime_type to the constraint: both parts are one attachment
        # to a human, so widening the key would make `attachments_pending`
        # list `invite.ics` twice forever.
        #
        # `filename` is nullable and Postgres does not collide NULLs, so
        # unnamed parts must NOT be deduped against each other — hence the
        # `is not None` guard rather than putting `None` in the seen set.
        # (`_process_candidates` guards the duplicate-*message* case; this is
        # the duplicate-*part-within-one-message* case it never covered.)
        seen_filenames: set[str] = set()
        for att in msg.get("attachments", []):
            fname = att["filename"]
            if fname is not None:
                if fname in seen_filenames:
                    continue
                seen_filenames.add(fname)
            status, skip_reason = _discovery_status("gmail", att["mime_type"])
            session.add(MessageAttachment(
                user_id=user_id,
                source="gmail",
                message_ref=msg["google_message_id"],
                filename=att["filename"],
                mime_type=att["mime_type"],
                size_bytes=att["size_bytes"],
                sender_name=msg.get("sender"),
                chat_or_thread=msg.get("subject") or account_email,
                message_ts=ts,
                parse_status=status,
                skip_reason=skip_reason,
                storage_path=att["attachment_id"],
            ))
            if status == "pending":
                created += 1
            else:
                unsupported += 1
    return created, unsupported


def _process_candidates(
    session: Session,
    account_email: str,
    user_id: int,
    message_ids: list[str],
    *,
    cap: int,
    already_handled: set[str],
) -> tuple[int, int]:
    """Skip already-recorded (or already-handled-this-call) ids, full-format-
    fetch + insert up to `cap` new ones.

    `already_handled` is mutated with every id this call decided to skip or
    insert — on the very first scan (empty cursor) the catch-up pass and the
    backfill pass both query the identical page 1, and without this the two
    passes would both try to insert the same rows in the same transaction
    and hit the unique constraint before either commits.
    """
    from app.plugin.capabilities import get_capability

    gmail = get_capability("mail.query")

    ids = [m for m in message_ids if m not in already_handled]
    already_handled.update(ids)
    if not ids:
        return 0, 0
    existing = _existing_gmail_refs(session, user_id, ids)
    unscanned = [m for m in ids if m not in existing][:cap]
    if not unscanned:
        return 0, 0
    full_messages = gmail.fetch_messages_full(account_email, session, unscanned, user_id=user_id)
    return _insert_gmail_attachments(session, user_id, account_email, full_messages)


def _scan_gmail_account(session: Session, account_email: str, *, user_id: int) -> dict:
    from app.plugin.capabilities import get_capability

    gmail = get_capability("mail.query")

    cursor = _load_gmail_cursor(session, user_id, account_email)
    new_pending = 0
    unsupported = 0
    handled_this_call: set[str] = set()

    # Catch-up: page 1 of has:attachment is always newest-first, so this
    # picks up freshly-arrived attachment mail every call regardless of
    # backfill progress. Cheap when nothing's new — the id list still comes
    # back, but _process_candidates skips anything already recorded before
    # spending a format='full' call.
    catchup_ids, _ = gmail.list_attachment_candidates_page(
        account_email, session, user_id=user_id, page_token=None, max_results=GMAIL_LIST_PAGE,
    )
    created, unsup = _process_candidates(
        session, account_email, user_id, catchup_ids, cap=GMAIL_SCAN_BATCH,
        already_handled=handled_this_call,
    )
    new_pending += created
    unsupported += unsup

    # Backfill: continue from the persisted page token, one page further
    # into history per call. Same has:attachment query as catch-up, so the
    # token stays valid across calls (the historical portion of the result
    # set it points into doesn't shift as new mail arrives at the front).
    # On the very first call (page_token is None) this is the SAME page 1
    # the catch-up pass just consumed — `handled_this_call` de-dupes that.
    if not cursor["backfill_done"]:
        page_ids, next_token = gmail.list_attachment_candidates_page(
            account_email, session, user_id=user_id,
            page_token=cursor["page_token"], max_results=GMAIL_SCAN_BATCH,
        )
        created, unsup = _process_candidates(
            session, account_email, user_id, page_ids, cap=GMAIL_SCAN_BATCH,
            already_handled=handled_this_call,
        )
        new_pending += created
        unsupported += unsup
        cursor["page_token"] = next_token
        if not next_token:
            cursor["backfill_done"] = True

    _save_gmail_cursor(session, user_id, account_email, cursor)
    session.commit()

    return {
        "account": account_email,
        "new_pending": new_pending,
        "unsupported": unsupported,
        "backfill_done": cursor["backfill_done"],
    }


def scan_gmail(session: Session) -> dict:
    """Bounded, checkpointed full-format Gmail attachment backfill.

    Scoped to the requesting user's connected Google accounts (via
    `current_user_id()` — this is a tool handler, not a sync() entrypoint;
    it is deliberately NOT wired into the integration's scheduled sync,
    which stays WhatsApp-only to avoid burning Gmail API quota on every
    30-min cycle). Safe to call repeatedly — see module docstring for the
    catch-up/backfill split and idempotency guarantees.
    """
    from app.auth.context import current_user_id
    from app.models.tokens import OAuthToken

    uid = current_user_id()
    accounts = [
        t.account_email
        for t in session.query(OAuthToken.account_email)
        .filter_by(user_id=uid, provider="google")
        .distinct()
        .all()
    ]
    if not accounts:
        return {
            "source": "gmail",
            "supported": True,
            "accounts_scanned": 0,
            "new_pending": 0,
            "unsupported": 0,
            "note": "no connected Google accounts for this user",
        }

    total_new = total_unsupported = 0
    per_account = []
    for account in accounts:
        try:
            result = _scan_gmail_account(session, account, user_id=uid)
        except (TransientError, PermanentError) as exc:
            # A dead/needs-reauth token for one account shouldn't block
            # scanning the user's other accounts.
            logger.warning(f"Gmail attachment scan failed for {account}: {exc}")
            per_account.append({"account": account, "error": str(exc)})
            continue
        total_new += result["new_pending"]
        total_unsupported += result["unsupported"]
        per_account.append(result)

    return {
        "source": "gmail",
        "supported": True,
        "accounts_scanned": len(accounts),
        "new_pending": total_new,
        "unsupported": total_unsupported,
        "accounts": per_account,
    }
