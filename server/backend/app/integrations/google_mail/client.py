"""Gmail API client — wraps the Google API for multiple accounts."""

import base64
import logging

from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.auth.oauth import get_credentials
from app.errors import PermanentError
from app.plugin.sync_runtime import classify_exc

logger = logging.getLogger(__name__)

# Gmail API batch limit
BATCH_SIZE = 50

# See google_calendar/client.py::_classify — same rationale: get_credentials
# already raises NeedsReauthError before any HTTP call for a dead refresh
# token, so a 401/403 reaching classify_exc means something else (disabled
# API, insufficient scope) and should stay a plain PermanentError.
_OVERRIDES = {401: PermanentError, 403: PermanentError}


def _classify(exc: Exception, context: str) -> Exception:
    return classify_exc(exc, context, provider="google", overrides=_OVERRIDES)


def get_gmail_service(account_email: str, session: Session, *, user_id: int):
    """Build a Gmail API service for the given account.

    `user_id` scopes the OAuthToken lookup so Sam cannot read Alex's mail
    (and vice versa). Returns None if no valid credentials are stored.
    """
    creds = get_credentials(account_email, session, user_id=user_id)
    if creds is None:
        logger.warning(f"No credentials for {account_email}")
        return None

    return build("gmail", "v1", credentials=creds)


def list_message_ids(
    account_email: str,
    session: Session,
    *,
    user_id: int,
    query: str = "",
    label_ids: list[str] | None = None,
    max_results: int | None = None,
) -> list[dict]:
    """Fetch message ID/threadId pairs from Gmail, paginating through all results.

    Args:
        query: Gmail search query (e.g. "after:2021/01/01")
        label_ids: Filter by label IDs (e.g. ["INBOX"])
        max_results: Cap on total messages. None = no limit (fetch all).

    Returns list of {"id": ..., "threadId": ...} dicts.
    """
    service = get_gmail_service(account_email, session, user_id=user_id)
    if service is None:
        return []

    try:
        all_ids = []
        page_token = None

        while True:
            kwargs = {"userId": "me", "maxResults": min(500, max_results or 500)}
            if query:
                kwargs["q"] = query
            if label_ids:
                kwargs["labelIds"] = label_ids
            if page_token:
                kwargs["pageToken"] = page_token

            response = service.users().messages().list(**kwargs).execute()
            batch = response.get("messages", [])
            all_ids.extend(batch)

            if max_results and len(all_ids) >= max_results:
                all_ids = all_ids[:max_results]
                break

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        logger.info(f"Listed {len(all_ids)} message IDs for {account_email}")
        return all_ids

    except Exception as exc:
        # Previously swallowed to `return []`, which made a total API/network
        # failure indistinguishable from "genuinely no messages" — sync_mail
        # would report 0 synced as a *success*. Raise (typed + chained) so
        # the scheduler can tell a real failure from an empty inbox.
        logger.exception(f"Failed to list messages for {account_email}")
        raise _classify(exc, f"list_message_ids for {account_email}") from exc


def list_attachment_candidates_page(
    account_email: str,
    session: Session,
    *,
    user_id: int,
    page_token: str | None = None,
    max_results: int = 100,
) -> tuple[list[str], str | None]:
    """One page of `has:attachment` message ids (id-only, no payload fetched).

    Unlike `list_message_ids`, this does NOT loop through every page — it
    returns exactly one page plus Gmail's `nextPageToken` so callers can
    persist the token as a resumable cursor (see
    `attachments/scan.py::scan_gmail`). Cheap: this is the same
    `messages.list` call `list_message_ids` makes, just not looped.
    """
    service = get_gmail_service(account_email, session, user_id=user_id)
    if service is None:
        return [], None

    try:
        kwargs: dict = {"userId": "me", "maxResults": max_results, "q": "has:attachment"}
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.users().messages().list(**kwargs).execute()
    except Exception as exc:
        logger.exception(f"Failed to list attachment candidates for {account_email}")
        raise _classify(
            exc, f"list_attachment_candidates_page for {account_email}"
        ) from exc

    ids = [m["id"] for m in response.get("messages", [])]
    return ids, response.get("nextPageToken")


def fetch_messages_full(
    account_email: str,
    session: Session,
    message_ids: list[str],
    *,
    user_id: int,
) -> list[dict]:
    """Batch-fetch full messages (format='full') and extract attachment parts.

    Unlike `fetch_messages_metadata` (format='metadata', headers only —
    `payload.parts` is stripped by the API at that format), this pulls the
    complete MIME structure so attachment filename/mimetype/size/attachmentId
    can be recovered. Costs ~10x metadata format in API quota — callers
    should only pass ids that genuinely need it (see scan.py's bounded
    candidate selection).

    Returns list of dicts: {google_message_id, thread_id, subject, sender,
    date (header string), internal_date (epoch ms int or None), attachments
    (list of {filename, mime_type, size_bytes, attachment_id})}.
    """
    service = get_gmail_service(account_email, session, user_id=user_id)
    if service is None:
        return []

    results = []

    for i in range(0, len(message_ids), BATCH_SIZE):
        chunk = message_ids[i : i + BATCH_SIZE]
        batch_results = {}

        def _callback(request_id, response, exception):
            if exception:
                logger.warning(f"Batch full-fetch failed for {request_id}: {exception}")
            else:
                batch_results[request_id] = response

        batch = service.new_batch_http_request(callback=_callback)
        for msg_id in chunk:
            batch.add(
                service.users().messages().get(userId="me", id=msg_id, format="full"),
                request_id=msg_id,
            )

        try:
            batch.execute()
        except Exception:
            logger.exception(f"Batch full-fetch execute failed for chunk starting at {i}")
            continue

        for msg_id in chunk:
            if msg_id not in batch_results:
                continue
            msg = batch_results[msg_id]
            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            internal_date = msg.get("internalDate")
            results.append({
                "google_message_id": msg["id"],
                "thread_id": msg.get("threadId", ""),
                "subject": headers.get("subject"),
                "sender": headers.get("from"),
                "date": headers.get("date"),
                "internal_date": int(internal_date) if internal_date else None,
                "attachments": _walk_attachment_parts(payload),
            })

    return results


class GmailAttachmentGone(PermanentError):
    """`users.messages.attachments.get` returned 404 for this (message, attachment).

    Gmail `attachmentId`s are not stable forever — the API documents them as
    opaque and they are observed to change (a fresh `format='full'` fetch of
    the same message returns a different id). A 404 therefore usually means
    "the id we recorded at scan time has rotated", not "the message is gone",
    which is why this is its own type: `attachments/ingest.py` needs to
    distinguish it from every other permanent failure to (a) try the user's
    other accounts when the row does not record which mailbox it came from
    and (b) write a skip_reason that says what actually happened.
    """


def fetch_attachment(
    account_email: str,
    session: Session,
    message_id: str,
    attachment_id: str,
    *,
    user_id: int,
) -> bytes | None:
    """Download one attachment's bytes via `users.messages.attachments.get`.

    The only Gmail call that returns attachment *content*; everything else
    in this module returns structure. Gmail hands the body back as
    base64url (`-`/`_` alphabet, padding optional), so it is decoded here —
    callers get raw file bytes.

    Uses the same `get_gmail_service` / `get_credentials` path as every
    other call here, scoped to `user_id` — the token used is always the
    mailbox owner's. Returns None when no credentials are stored for
    `(user_id, account_email)`; raises `GmailAttachmentGone` on 404 and the
    usual classified error otherwise.
    """
    service = get_gmail_service(account_email, session, user_id=user_id)
    if service is None:
        return None

    try:
        response = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
    except Exception as exc:
        if getattr(getattr(exc, "resp", None), "status", None) == 404:
            raise GmailAttachmentGone(
                f"Gmail attachment {attachment_id[:12]}… on message {message_id} "
                f"not found in {account_email} (404)"
            ) from exc
        logger.exception(f"Failed to fetch attachment on {message_id} for {account_email}")
        raise _classify(exc, f"fetch_attachment for {account_email}") from exc

    data = response.get("data") or ""
    # base64url with optional padding: urlsafe_b64decode requires the
    # padding Gmail omits, so restore it before decoding.
    data += "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data)


def _walk_attachment_parts(payload: dict) -> list[dict]:
    """Recursively walk a message payload's MIME tree, collecting attachments.

    A part counts as an attachment when it carries both a filename AND a
    `body.attachmentId` — inline content referenced by `Content-ID` (e.g.
    embedded logos) has a filename-less body with inline data instead and is
    skipped, since there's nothing to fetch bytes for later via
    `users.messages.attachments.get`. Handles arbitrary multipart nesting
    (e.g. multipart/mixed containing multipart/alternative containing the
    actual text parts, with the attachment as a mixed-level sibling part).
    """
    found = []
    filename = payload.get("filename")
    body = payload.get("body") or {}
    attachment_id = body.get("attachmentId")
    if filename and attachment_id:
        size = body.get("size")
        found.append({
            "filename": filename,
            "mime_type": payload.get("mimeType"),
            "size_bytes": int(size) if isinstance(size, (int, str)) and str(size).isdigit() else None,
            "attachment_id": attachment_id,
        })
    for part in payload.get("parts") or []:
        found.extend(_walk_attachment_parts(part))
    return found


def fetch_messages_metadata(
    account_email: str,
    session: Session,
    message_ids: list[str],
    *,
    user_id: int,
) -> list[dict]:
    """Batch-fetch message metadata for a list of message IDs.

    Uses Gmail batch API (up to 50 per batch) for efficiency.
    Returns list of parsed message metadata dicts.
    """
    service = get_gmail_service(account_email, session, user_id=user_id)
    if service is None:
        return []

    results = []

    for i in range(0, len(message_ids), BATCH_SIZE):
        chunk = message_ids[i : i + BATCH_SIZE]
        batch_results = {}

        def _callback(request_id, response, exception):
            if exception:
                logger.warning(f"Batch fetch failed for {request_id}: {exception}")
            else:
                batch_results[request_id] = response

        batch = service.new_batch_http_request(callback=_callback)
        for msg_id in chunk:
            batch.add(
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Date"],
                ),
                request_id=msg_id,
            )

        try:
            batch.execute()
        except Exception:
            logger.exception(f"Batch execute failed for chunk starting at {i}")
            continue

        for msg_id in chunk:
            if msg_id in batch_results:
                results.append(_parse_message(batch_results[msg_id], account_email))

    return results


def fetch_messages_bodies(
    account_email: str,
    session: Session,
    message_ids: list[str],
    *,
    user_id: int,
) -> dict[str, str]:
    """Batch-fetch full message bodies for a list of message IDs.

    Uses Gmail batch API. Returns dict of {message_id: plain_text_body}.
    Skips messages where body extraction fails.
    """
    service = get_gmail_service(account_email, session, user_id=user_id)
    if service is None:
        return {}

    results = {}

    for i in range(0, len(message_ids), BATCH_SIZE):
        chunk = message_ids[i : i + BATCH_SIZE]
        batch_results = {}

        def _callback(request_id, response, exception):
            if exception:
                logger.warning(f"Batch body fetch failed for {request_id}: {exception}")
            else:
                batch_results[request_id] = response

        batch = service.new_batch_http_request(callback=_callback)
        for msg_id in chunk:
            batch.add(
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full"),
                request_id=msg_id,
            )

        try:
            batch.execute()
        except Exception:
            logger.exception(f"Batch body fetch failed for chunk starting at {i}")
            continue

        for msg_id in chunk:
            if msg_id in batch_results:
                body = _extract_body(batch_results[msg_id].get("payload", {}))
                if body:
                    results[msg_id] = body

    return results


def list_messages(
    account_email: str,
    session: Session,
    *,
    user_id: int,
    query: str = "",
    max_results: int = 50,
    label_ids: list[str] | None = None,
) -> list[dict]:
    """Fetch message metadata from Gmail (convenience wrapper).

    Combines list_message_ids + fetch_messages_metadata.
    For large fetches, use those functions directly with batched DB commits.
    """
    ids = list_message_ids(
        account_email, session,
        user_id=user_id, query=query, label_ids=label_ids, max_results=max_results,
    )
    if not ids:
        return []

    return fetch_messages_metadata(
        account_email, session, [m["id"] for m in ids], user_id=user_id,
    )


def get_message(
    account_email: str,
    session: Session,
    message_id: str,
    *,
    user_id: int,
) -> dict | None:
    """Fetch a single message with full body text."""
    service = get_gmail_service(account_email, session, user_id=user_id)
    if service is None:
        return None

    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        result = _parse_message(msg, account_email)
        result["body"] = _extract_body(msg.get("payload", {}))
        return result
    except Exception:
        logger.exception(f"Failed to fetch message {message_id}")
        return None


def get_thread(
    account_email: str,
    session: Session,
    thread_id: str,
    *,
    user_id: int,
) -> list[dict]:
    """Fetch all messages in a thread."""
    service = get_gmail_service(account_email, session, user_id=user_id)
    if service is None:
        return []

    try:
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="metadata",
                 metadataHeaders=["Subject", "From", "To", "Date"])
            .execute()
        )
        return [
            _parse_message(msg, account_email)
            for msg in thread.get("messages", [])
        ]
    except Exception:
        logger.exception(f"Failed to fetch thread {thread_id}")
        return []


def _parse_message(msg: dict, account_email: str) -> dict:
    """Extract metadata from a Gmail API message response."""
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        headers[h["name"].lower()] = h["value"]

    label_ids = msg.get("labelIds", [])

    return {
        "google_message_id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "account_email": account_email,
        "subject": headers.get("subject"),
        "sender": headers.get("from"),
        "to": headers.get("to"),
        "date": headers.get("date"),
        "snippet": msg.get("snippet", ""),
        "labels": ",".join(label_ids),
        "is_read": "UNREAD" not in label_ids,
        "is_starred": "STARRED" in label_ids,
        "has_attachments": _has_attachments(msg.get("payload", {})),
        "size_estimate": msg.get("sizeEstimate"),
    }


def _has_attachments(payload: dict) -> bool:
    """Check if message has attachments."""
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("filename"):
            return True
        if part.get("parts"):
            if _has_attachments(part):
                return True
    return False


def _extract_body(payload: dict) -> str:
    """Extract plain text body from message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if part.get("parts"):
            body = _extract_body(part)
            if body:
                return body

    return ""
