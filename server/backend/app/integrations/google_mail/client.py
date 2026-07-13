"""Gmail API client — wraps the Google API for multiple accounts."""

import base64
import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.auth.oauth import get_credentials
from app.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)

# Gmail API batch limit
BATCH_SIZE = 50


def _classify_http_error(exc: Exception, context: str) -> Exception:
    """Map a googleapiclient/network failure to TransientError or PermanentError.

    Returns the exception to raise (chained `from exc` by the caller). Auth
    failures that mean "needs re-auth" are already handled upstream by
    `get_credentials` (raises `NeedsReauthError` before the API call is ever
    made) — a 401/403 reaching this point means something else is wrong
    (API disabled, insufficient scope, etc.), still permanent but distinct.
    """
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        if status in (401, 403):
            return PermanentError(f"{context}: HTTP {status} ({exc.reason})")
        if status == 429 or (status is not None and status >= 500):
            return TransientError(f"{context}: HTTP {status} ({exc.reason})")
        return exc
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return TransientError(f"{context}: {exc}")
    return exc


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
        raise _classify_http_error(exc, f"list_message_ids for {account_email}") from exc


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
