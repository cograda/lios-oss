"""Sync logic: poll Gmail API → upsert message metadata into Postgres.

`pull_mail`/`store_mail` are the `SourceIntegration.pull()`/`.store()` pair
(V4 chunk 4.3, batch C) — the pre-4.3 `sync_mail()` collapsed fetch and
persist into one function; this splits it the same way google_calendar's
4.1 conversion did. `sync_mail()` had exactly one caller
(`GoogleMailIntegration.sync()`'s per-account fan-out closure), which now
calls `pull_mail`/`store_mail` directly, so the combined function is removed
rather than kept as a redundant wrapper.
"""

import json
import logging
from datetime import datetime, timezone

from dateutil.parser import parse as parse_date
from sqlalchemy.orm import Session

from app.integrations.google_mail.client import (
    fetch_messages_metadata,
    list_message_ids,
    list_messages,
)
from app.integrations.google_mail.models import MailMessage
from app.plugin.bases import PullResult

logger = logging.getLogger(__name__)

# Commit every N messages during backfill to keep memory bounded
COMMIT_BATCH = 500


def pull_mail(
    account_email: str,
    session: Session,
    *,
    user_id: int,
    max_results: int = 500,
) -> PullResult:
    """Fetch recent INBOX message metadata for one account. No DB writes —
    see `store_mail`. `user_id` is stamped onto each record (`store()`'s
    `SourceIntegration` signature only receives `records`, not the account
    itself, so the owning user has to travel with the row — same reasoning
    as `account_email` already being a `_parse_message` field)."""
    messages = list_messages(
        account_email, session,
        user_id=user_id, label_ids=["INBOX"], max_results=max_results,
    )
    if not messages:
        logger.info(f"No messages found for {account_email}")
        return PullResult(records=[])
    for m in messages:
        m["user_id"] = user_id
    return PullResult(records=messages)


def store_mail(session: Session, records: list[dict]) -> int:
    """Upsert `records` (one account's fetched message metadata, each
    carrying its own `account_email`/`user_id`) and commit. Returns the
    number of messages persisted."""
    if not records:
        return 0
    account_email = records[0]["account_email"]
    user_id = records[0]["user_id"]
    synced = _upsert_messages(records, account_email, session, user_id=user_id)
    session.commit()
    logger.info(f"Synced {synced} messages for {account_email}")
    return synced




def backfill_mail(
    account_email: str,
    session: Session,
    *,
    user_id: int,
    after_date: str = "2021/01/01",
) -> int:
    """Backfill all messages from a date onwards (all labels, not just INBOX).

    Paginates through the full Gmail history, batch-fetches metadata,
    and commits in chunks. Skips messages already in the DB.

    Args:
        after_date: Gmail date filter (YYYY/MM/DD format).

    Returns the total number of new messages inserted.
    """
    query = f"after:{after_date}"
    logger.info(f"Starting backfill for {account_email}: {query}")

    # Get all message IDs matching the query
    all_ids = list_message_ids(account_email, session, user_id=user_id, query=query)
    if not all_ids:
        logger.info(f"No messages found for backfill query: {query}")
        return 0

    logger.info(f"Found {len(all_ids)} messages to process for {account_email}")

    # Find which ones we already have (scoped to this user — same google_id
    # could in theory exist in two users' accounts).
    msg_id_list = [m["id"] for m in all_ids]
    existing_ids = set(
        row[0]
        for row in session.query(MailMessage.google_message_id)
        .filter(
            MailMessage.user_id == user_id,
            MailMessage.google_message_id.in_(msg_id_list),
        )
        .all()
    )

    new_ids = [mid for mid in msg_id_list if mid not in existing_ids]
    logger.info(
        f"Backfill: {len(existing_ids)} already cached, {len(new_ids)} new to fetch"
    )

    if not new_ids:
        return 0

    # Fetch and insert in chunks
    total_inserted = 0
    for i in range(0, len(new_ids), COMMIT_BATCH):
        chunk_ids = new_ids[i : i + COMMIT_BATCH]
        messages = fetch_messages_metadata(
            account_email, session, chunk_ids, user_id=user_id,
        )

        inserted = _insert_new_messages(
            messages, account_email, session, user_id=user_id,
        )
        session.commit()
        total_inserted += inserted
        logger.info(
            f"Backfill progress: {total_inserted}/{len(new_ids)} "
            f"({total_inserted * 100 // len(new_ids)}%)"
        )

    logger.info(f"Backfill complete for {account_email}: {total_inserted} new messages")
    return total_inserted


def _upsert_messages(
    messages: list[dict],
    account_email: str,
    session: Session,
    *,
    user_id: int,
) -> int:
    """Upsert a list of message metadata dicts. Returns count processed."""
    count = 0
    for msg_data in messages:
        google_id = msg_data["google_message_id"]
        date_val = _parse_date_safe(msg_data.get("date"))

        existing = (
            session.query(MailMessage)
            .filter_by(user_id=user_id, google_message_id=google_id)
            .first()
        )

        if existing:
            existing.is_read = msg_data["is_read"]
            existing.is_starred = msg_data["is_starred"]
            existing.labels = msg_data["labels"]
            existing.synced_at = datetime.now(timezone.utc)
        else:
            session.add(
                MailMessage(
                    user_id=user_id,
                    google_message_id=google_id,
                    thread_id=msg_data["thread_id"],
                    account_email=account_email,
                    subject=msg_data["subject"],
                    sender=msg_data["sender"],
                    to=msg_data["to"],
                    date=date_val,
                    snippet=msg_data["snippet"],
                    labels=msg_data["labels"],
                    is_read=msg_data["is_read"],
                    is_starred=msg_data["is_starred"],
                    has_attachments=msg_data["has_attachments"],
                    size_estimate=msg_data["size_estimate"],
                )
            )
        count += 1

    return count


def _insert_new_messages(
    messages: list[dict],
    account_email: str,
    session: Session,
    *,
    user_id: int,
) -> int:
    """Insert new messages only (no upsert check — caller pre-filtered)."""
    count = 0
    for msg_data in messages:
        date_val = _parse_date_safe(msg_data.get("date"))
        session.add(
            MailMessage(
                user_id=user_id,
                google_message_id=msg_data["google_message_id"],
                thread_id=msg_data["thread_id"],
                account_email=account_email,
                subject=msg_data["subject"],
                sender=msg_data["sender"],
                to=msg_data["to"],
                date=date_val,
                snippet=msg_data["snippet"],
                labels=msg_data["labels"],
                is_read=msg_data["is_read"],
                is_starred=msg_data["is_starred"],
                has_attachments=msg_data["has_attachments"],
                size_estimate=msg_data["size_estimate"],
            )
        )
        count += 1

    return count


def embed_messages(session: Session, batch_size: int = 200) -> int:
    """Enqueue un-embedded mail messages for semantic search.

    Fetches full message bodies from Gmail API (batch), combines with
    metadata into chunk text, and enqueues into the unified embedding
    pipeline. The background worker will embed them.

    Returns the number of messages enqueued.
    """
    from app.integrations.google_mail.client import fetch_messages_bodies
    from app.services.embedding import Embedding, EmbeddingService

    # Find messages without embeddings in the unified table
    embedded_ids = (
        session.query(Embedding.source_id)
        .filter_by(source="email")
        .subquery()
    )
    unembedded = (
        session.query(MailMessage)
        .filter(~MailMessage.google_message_id.in_(session.query(embedded_ids)))
        .order_by(MailMessage.date.desc().nullslast())
        .all()
    )

    if not unembedded:
        logger.info("All mail messages already embedded")
        return 0

    logger.info(f"Enqueuing {len(unembedded)} mail messages for embedding")

    # Group by (user_id, account) — same google_message_id can in theory exist
    # in two users' caches, and OAuth tokens are per-user.
    by_account: dict[tuple[int, str], list[MailMessage]] = {}
    for msg in unembedded:
        by_account.setdefault((msg.user_id, msg.account_email), []).append(msg)

    total = 0

    for (uid, account), messages in by_account.items():
        for i in range(0, len(messages), batch_size):
            batch = messages[i : i + batch_size]
            msg_ids = [m.google_message_id for m in batch]

            # Fetch full bodies from Gmail API (scoped to this user's token)
            bodies = fetch_messages_bodies(account, session, msg_ids, user_id=uid)

            # Build chunk texts and enqueue
            for msg in batch:
                body = bodies.get(msg.google_message_id, msg.snippet or "")
                date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else ""

                # Subject as bare text + body, and nothing else. Measured
                # 2026-08-05 on 2,000 messages against the gemini reference
                # space: dropping the subject costs -34.4% on 10-NN agreement
                # (it is the highest-signal line in a mail), while the old
                # `From:/To:/Subject:/Date:` header block was worth -4.0%
                # versus this shape — sender/recipient/date are metadata, and
                # they're already in metadata_json below for filtering.
                #
                # No truncation here on purpose: EmbeddingService.enqueue()
                # cleans first and caps after. Truncating a raw HTML body at
                # 4000 chars, as this did, routinely kept `<style>` blocks and
                # threw the prose away.
                chunk = f"{msg.subject or ''}\n\n{body}".strip()

                metadata = json.dumps({
                    "account": msg.account_email,
                    "subject": msg.subject,
                    "sender": msg.sender,
                    "date": date_str,
                    "thread_id": msg.thread_id,
                })
                EmbeddingService.enqueue(
                    session, "email", msg.google_message_id, chunk, metadata,
                    user_id=uid,
                )

            session.commit()
            total += len(batch)
            logger.info(
                f"Enqueued {total}/{len(unembedded)} mail messages "
                f"({len(bodies)}/{len(batch)} with full body)"
            )

    return total


def _parse_date_safe(date_str: str | None) -> datetime | None:
    """Parse a date string, returning None on failure."""
    if not date_str:
        return None
    try:
        dt = parse_date(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
