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

# Manifest default for `google_mail.embed_backfill_from` (see manifest.py's
# ConfigFieldSpec docstring for the production numbers this floor is set
# against: 24,493 `tm:<sha256>` rows spanning 2004-11-26 → 2026-07-15, and
# 127,614 + 2,342 real `mail_messages` rows for the two users).
DEFAULT_EMBED_BACKFILL_FROM = "2026-07-15"

# `embed_messages` refuses rather than silently truncating past this many
# eligible messages in one call — a large Gemini bill should be a decision,
# not a side effect of calling a tool with no arguments.
DEFAULT_MAX_EMBED_MESSAGES = 5000


class EmbedBacklogTooLargeError(ValueError):
    """`embed_messages` would enqueue more than `max_messages` — raised
    instead of truncating so an oversized backlog is a decision, not a
    silent partial run. Message names the actual count, the cap, and the
    config key that controls the floor."""


def _embed_backfill_from() -> datetime:
    """The `google_mail.embed_backfill_from` config value as a UTC datetime,
    falling back to `DEFAULT_EMBED_BACKFILL_FROM` on any config problem —
    same degrade-safe pattern `embedding.py::_recency_settings` uses.
    """
    from app.plugin.config_store import plugin_config

    try:
        cfg = plugin_config("google_mail")
        raw = getattr(cfg, "embed_backfill_from", None) or DEFAULT_EMBED_BACKFILL_FROM
    except Exception:  # noqa: BLE001
        logger.warning("google_mail.embed_backfill_from unavailable; using hardcoded default", exc_info=True)
        raw = DEFAULT_EMBED_BACKFILL_FROM
    dt = _parse_date_safe(raw) or _parse_date_safe(DEFAULT_EMBED_BACKFILL_FROM)
    return dt


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


def _mail_chunk_text(msg: MailMessage, body: str) -> str:
    """Subject as bare text + body, and nothing else. Measured 2026-08-05 on
    2,000 messages against the gemini reference space: dropping the subject
    costs -34.4% on 10-NN agreement (it is the highest-signal line in a
    mail), while the old `From:/To:/Subject:/Date:` header block was worth
    -4.0% versus this shape — sender/recipient/date are metadata, and
    they're already in `_mail_chunk_metadata` below for filtering.

    No truncation here on purpose: `EmbeddingService.enqueue()` cleans first
    and caps after. Truncating a raw HTML body at 4000 chars, as this used
    to, routinely kept `<style>` blocks and threw the prose away.
    """
    return f"{msg.subject or ''}\n\n{body}".strip()


def _mail_chunk_metadata(msg: MailMessage) -> str:
    date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else ""
    return json.dumps({
        "account": msg.account_email,
        "subject": msg.subject,
        "sender": msg.sender,
        "date": date_str,
        "thread_id": msg.thread_id,
    })


def _enqueue_mail_batch(
    session: Session, account: str, messages: list[MailMessage], *, user_id: int,
) -> int:
    """Fetch bodies for one account/user's batch and enqueue each message
    into the unified embedding pipeline. The one function both
    `embed_messages` (manual/backfill, over every un-embedded message) and
    `enqueue_new_mail` (sync-time, over just what was freshly synced) call
    to build chunk text — see `Code/CLAUDE.md`'s copy-and-verify section for
    why two independently-built text-shapes for the same source is exactly
    the drift this avoids.
    """
    from app.integrations.google_mail.client import fetch_messages_bodies
    from app.services.embedding import EmbeddingService

    if not messages:
        return 0

    msg_ids = [m.google_message_id for m in messages]
    # Fetch full bodies from Gmail API (scoped to this user's token)
    bodies = fetch_messages_bodies(account, session, msg_ids, user_id=user_id)

    enqueued = 0
    for msg in messages:
        body = bodies.get(msg.google_message_id, msg.snippet or "")
        chunk = _mail_chunk_text(msg, body)
        metadata = _mail_chunk_metadata(msg)
        if EmbeddingService.enqueue(
            session, "email", msg.google_message_id, chunk, metadata,
            user_id=user_id,
        ):
            enqueued += 1

    session.commit()
    return enqueued


def _eligible_unembedded_mail(session: Session, user_id: int | None, backfill_from: datetime):
    """Query of `MailMessage` rows dated on/after `backfill_from` that have
    no `email`-sourced embedding yet — the eligibility rule both
    `embed_messages` and `plan_embed_messages` (dry-run) share, so what a
    dry run reports is exactly what a real run would enqueue.

    Two things a naive anti-join gets wrong, both fixed here:

    - **Nullable dates are excluded, not included.** A message with no
      reliable `date` can't be compared to the backfill floor at all —
      treating it as "in scope" would silently defeat the floor for the
      exact rows most likely to be old/malformed imports.
    - **The "already embedded" check is scoped to the SAME user**, via a
      correlated `NOT EXISTS` rather than a flat `NOT IN` subquery. A flat
      subquery collects every embedded `source_id` across ALL users, so if
      user 2 ever holds an embedding whose `source_id` happens to equal one
      of user 1's `google_message_id`s (not impossible — ids are only
      unique per Gmail account, per `MailMessage`'s own docstring), user
      1's message would read as already-embedded and get silently skipped.
      `enqueue_new_mail` already got this right; this brings the manual
      backfill path to the same standard.
    """
    from sqlalchemy import exists

    from app.services.embedding import Embedding

    already_embedded = exists().where(
        Embedding.source == "email",
        Embedding.source_id == MailMessage.google_message_id,
        Embedding.user_id == MailMessage.user_id,
    )
    q = session.query(MailMessage).filter(
        MailMessage.date.isnot(None),
        MailMessage.date >= backfill_from,
        ~already_embedded,
    )
    if user_id is not None:
        q = q.filter(MailMessage.user_id == user_id)
    return q


def plan_embed_messages(session: Session, *, user_id: int | None = None) -> dict:
    """What `embed_messages` would enqueue right now, without enqueuing —
    the `gmail_embed` tool's `dry_run` path. Same eligibility rule as the
    real run (`_eligible_unembedded_mail`), so the preview can't drift from
    what actually happens.
    """
    backfill_from = _embed_backfill_from()
    rows = _eligible_unembedded_mail(session, user_id, backfill_from).with_entities(MailMessage.date).all()
    dates = [d for (d,) in rows if d is not None]
    return {
        "backfill_from": backfill_from.date().isoformat(),
        "count": len(rows),
        "earliest": min(dates).isoformat() if dates else None,
        "latest": max(dates).isoformat() if dates else None,
    }


def embed_messages(
    session: Session, batch_size: int = 200, *, user_id: int | None = None,
    max_messages: int | None = DEFAULT_MAX_EMBED_MESSAGES,
) -> int:
    """Enqueue un-embedded mail messages for semantic search.

    Fetches full message bodies from Gmail API (batch), combines with
    metadata into chunk text, and enqueues into the unified embedding
    pipeline. The background worker will embed them.

    `user_id` restricts the pass to one user's `mail_messages`. The
    `gmail_embed` MCP tool passes the caller's: unscoped, one user's tool
    call walked the other's mail cache AND fetched bodies with the other
    user's OAuth token (2026-09-06 scoping audit). `None` is the unbound
    scheduled/admin shape and stays household-wide, grouping by owner.

    This is the manual backfill path, bounded two ways since S5.1:

    1. **Time floor** (`google_mail.embed_backfill_from`, default
       `2026-07-15`). Before this fix, this walked every un-embedded row in
       `mail_messages` ever — measured on production, that's ~130k real
       messages (127,614 for user 1, 2,342 for user 2), because the
       ~24,493 historical `email`-sourced rows keyed `tm:<sha256>` (a
       timemachine bulk import, `metadata_json.date` spanning
       2004-11-26 → 2026-07-15) share no id with `google_message_id` and so
       never look "already embedded" to this codebase's own anti-join. The
       floor treats that import as coverage for everything up to its own
       latest date, so this only ever considers mail *since* it, without
       ever touching its rows (still a different id shape either way — see
       `enqueue_new_mail`'s docstring).
    2. **`max_messages`** (default `DEFAULT_MAX_EMBED_MESSAGES`, 5000):
       refuses with `EmbedBacklogTooLargeError` naming the actual count and
       the config key, rather than silently truncating, when more than
       that would be enqueued. `None` disables the cap for a caller who has
       deliberately decided to pay for a larger run.

    New mail is enqueued continuously at sync time instead (see
    `enqueue_new_mail`, called from `GoogleMailIntegration.store()`), so
    this is a deliberate catch-up/backfill rather than the only way mail
    ever gets embedded.

    Returns the number of messages enqueued.
    """
    backfill_from = _embed_backfill_from()
    unembedded = (
        _eligible_unembedded_mail(session, user_id, backfill_from)
        .order_by(MailMessage.date.desc().nullslast())
        .all()
    )

    if not unembedded:
        logger.info("All mail messages already embedded")
        return 0

    if max_messages is not None and len(unembedded) > max_messages:
        raise EmbedBacklogTooLargeError(
            f"embed_messages would enqueue {len(unembedded)} messages "
            f"(mail dated on/after {backfill_from.date().isoformat()}, the "
            f"google_mail.embed_backfill_from floor), over the max_messages "
            f"cap of {max_messages}. Pass a larger max_messages explicitly "
            f"if this is expected, or raise google_mail.embed_backfill_from "
            f"to shrink the backlog."
        )

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
            total += _enqueue_mail_batch(session, account, batch, user_id=uid)
            logger.info(f"Enqueued {total}/{len(unembedded)} mail messages")

    return total


def enqueue_new_mail(
    session: Session, google_message_ids: list[str], *, user_id: int, batch_size: int = 200,
) -> int:
    """Queue freshly-synced mail for embedding — the mail-side mirror of
    `whatsapp.sync.embed_messages` being called after every WhatsApp sync
    (`WhatsAppIntegration.sync()`). Called from `GoogleMailIntegration.store()`
    right after `store_mail` upserts `google_message_ids`.

    Scoped to exactly the ids just synced, further narrowed here to those
    genuinely missing an embedding — so an unchanged message's body is never
    re-fetched from Gmail on a routine metadata-only refresh (e.g. a read/
    starred flag flip). Mail is enqueued continuously from here on without
    ever touching the historical backlog: the ~24,493 `email`-sourced rows
    keyed `tm:<sha256>` are a historical timemachine import that predates
    this codebase entirely (no `tm:` literal exists anywhere under `app/`)
    and share no id with a `google_message_id`, so this can't reach them
    either, by construction. `embed_messages` (the `gmail_embed` tool) is
    the manual backfill over the same `_enqueue_mail_batch` helper — one
    function builds the chunk text for both paths.

    Already scoped per-user (`Embedding.user_id == user_id`), unlike
    `embed_messages`'s pre-S5.1 anti-join — see `_eligible_unembedded_mail`'s
    docstring for why a flat cross-user subquery there was a real gap even
    though this one, called with a single fixed `user_id` per call, was
    never actually exposed to it.
    """
    from app.services.embedding import Embedding

    if not google_message_ids:
        return 0

    already_embedded = (
        session.query(Embedding.source_id)
        .filter(
            Embedding.source == "email",
            Embedding.user_id == user_id,
            Embedding.source_id.in_(google_message_ids),
        )
        .subquery()
    )
    messages = (
        session.query(MailMessage)
        .filter(
            MailMessage.user_id == user_id,
            MailMessage.google_message_id.in_(google_message_ids),
            ~MailMessage.google_message_id.in_(session.query(already_embedded)),
        )
        .all()
    )
    if not messages:
        return 0

    by_account: dict[str, list[MailMessage]] = {}
    for msg in messages:
        by_account.setdefault(msg.account_email, []).append(msg)

    total = 0
    for account, msgs in by_account.items():
        for i in range(0, len(msgs), batch_size):
            total += _enqueue_mail_batch(session, account, msgs[i : i + batch_size], user_id=user_id)
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
