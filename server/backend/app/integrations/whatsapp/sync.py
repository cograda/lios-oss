"""Conversation-window embedding for WhatsApp messages.

Instead of embedding individual messages (which are often too short to be
meaningful), this module groups messages into conversation segments based
on time proximity within the same chat, then embeds each segment as a unit.

Strategy:
1. Segment: Split each chat's messages at 30-min gaps
2. Merge runts: Segments under MIN_SEGMENT_CHARS get merged with neighbours
3. Split giants: Segments over MAX_CHUNK_CHARS get split at natural gaps
4. Result: Every message ends up in exactly one chunk
"""

import json
import logging
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.whatsapp.models import WhatsAppMessage
from app.services.embedding import Embedding, EmbeddingService

logger = logging.getLogger(__name__)

# --- Chunking parameters ---
GAP_THRESHOLD = timedelta(minutes=30)  # Time gap that starts a new segment
MIN_SEGMENT_CHARS = 150                # Merge segments smaller than this
MAX_CHUNK_CHARS = 3000                 # Split segments larger than this
MIN_MESSAGES_FOR_EMBED = 2            # Don't embed segments with fewer messages


def _build_segments(messages: list[WhatsAppMessage]) -> list[list[WhatsAppMessage]]:
    """Split a chat's messages into time-windowed segments.

    Messages must be sorted by timestamp ascending.
    """
    if not messages:
        return []

    segments = []
    current = [messages[0]]

    for msg in messages[1:]:
        gap = msg.timestamp - current[-1].timestamp
        if gap > GAP_THRESHOLD:
            segments.append(current)
            current = [msg]
        else:
            current.append(msg)

    segments.append(current)
    return segments


def _segment_text_length(segment: list[WhatsAppMessage]) -> int:
    """Total body text length of a segment."""
    return sum(len(msg.body or "") for msg in segment)


def _merge_runts(segments: list[list[WhatsAppMessage]]) -> list[list[WhatsAppMessage]]:
    """Merge undersized segments with their nearest neighbour.

    Prefers merging with the neighbour that has the smallest time gap.
    Repeats until no runts remain or only one segment is left.
    """
    if len(segments) <= 1:
        return segments

    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(segments):
            if _segment_text_length(segments[i]) < MIN_SEGMENT_CHARS and len(segments) > 1:
                if i == 0:
                    merge_target = 1
                elif i == len(segments) - 1:
                    merge_target = i - 1
                else:
                    gap_before = segments[i][0].timestamp - segments[i - 1][-1].timestamp
                    gap_after = segments[i + 1][0].timestamp - segments[i][-1].timestamp
                    merge_target = i - 1 if gap_before <= gap_after else i + 1

                if merge_target < i:
                    segments[merge_target] = segments[merge_target] + segments[i]
                    segments.pop(i)
                else:
                    segments[i] = segments[i] + segments[merge_target]
                    segments.pop(merge_target)

                changed = True
            else:
                i += 1

    return segments


def _split_giants(segments: list[list[WhatsAppMessage]]) -> list[list[WhatsAppMessage]]:
    """Split oversized segments at the largest internal time gap."""
    result = []
    for segment in segments:
        if _segment_text_length(segment) <= MAX_CHUNK_CHARS or len(segment) < 2:
            result.append(segment)
            continue

        # Find the largest internal gap
        best_gap_idx = 0
        best_gap = timedelta(0)
        for j in range(1, len(segment)):
            gap = segment[j].timestamp - segment[j - 1].timestamp
            if gap > best_gap:
                best_gap = gap
                best_gap_idx = j

        left = segment[:best_gap_idx]
        right = segment[best_gap_idx:]

        if left and right:
            result.extend(_split_giants([left]))
            result.extend(_split_giants([right]))
        else:
            result.append(segment)

    return result


def _format_segment(segment: list[WhatsAppMessage]) -> str:
    """Format a message segment into embeddable text."""
    if not segment:
        return ""

    first = segment[0]
    chat_label = first.chat_name or first.chat_id or "unknown"

    if first.is_group:
        header = f"[WhatsApp group: {chat_label}]"
    else:
        header = f"[WhatsApp chat: {chat_label}]"

    lines = [header]
    for msg in segment:
        sender = msg.sender_name or msg.sender_id or "unknown"
        if msg.is_from_me:
            sender = "me"
        time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M") if msg.timestamp else ""

        if msg.body:
            lines.append(f"[{time_str}] {sender}: {msg.body}")
        elif msg.media_caption:
            lines.append(f"[{time_str}] {sender}: [{msg.message_type}] {msg.media_caption}")
        else:
            lines.append(f"[{time_str}] {sender}: [{msg.message_type}]")

    text = "\n".join(lines)
    return text[:MAX_CHUNK_CHARS] if len(text) > MAX_CHUNK_CHARS else text


def _segment_id(segment: list[WhatsAppMessage]) -> str:
    """Generate a unique ID for a conversation segment.

    Format: {chat_id}:{start_epoch}:{end_epoch}
    This changes when the segment boundaries shift (new messages arrive),
    which triggers re-embedding via the content hash check.
    """
    first = segment[0]
    last = segment[-1]
    start_ts = int(first.timestamp.timestamp())
    end_ts = int(last.timestamp.timestamp())
    return f"{first.chat_id}:{start_ts}:{end_ts}"


def _segment_metadata(segment: list[WhatsAppMessage]) -> str:
    """Build JSON metadata for a segment."""
    first = segment[0]
    last = segment[-1]
    senders = list({
        m.sender_name or m.sender_id
        for m in segment
        if not m.is_from_me and (m.sender_name or m.sender_id)
    })

    return json.dumps({
        "chat_id": first.chat_id,
        "chat_name": first.chat_name,
        "is_group": first.is_group,
        "start": first.timestamp.isoformat(),
        "end": last.timestamp.isoformat(),
        "message_count": len(segment),
        "participants": senders[:10],
    })


def embed_messages(session: Session, batch_size: int = 200) -> int:
    """Build conversation-window chunks and enqueue for embedding.

    Groups messages by chat, segments by time windows, merges small
    segments, splits large ones, then enqueues each chunk.

    Cleans up old embeddings for segments whose boundaries have shifted
    due to new messages arriving.

    Returns the number of chunks enqueued.
    """
    # Get all chats that have text messages, per owning user — segments
    # must never mix two users' message caches, and each segment's
    # embedding carries its owner's user_id for search scoping.
    chat_keys = (
        session.query(WhatsAppMessage.user_id, WhatsAppMessage.chat_id)
        .filter(WhatsAppMessage.body.isnot(None))
        .group_by(WhatsAppMessage.user_id, WhatsAppMessage.chat_id)
        .having(func.count(WhatsAppMessage.id) >= MIN_MESSAGES_FOR_EMBED)
        .all()
    )

    if not chat_keys:
        logger.info("No WhatsApp chats with enough messages to embed")
        return 0

    total_enqueued = 0
    all_new_segment_ids = set()

    for owner_id, chat_id in chat_keys:
        messages = (
            session.query(WhatsAppMessage)
            .filter_by(user_id=owner_id, chat_id=chat_id)
            .filter(WhatsAppMessage.body.isnot(None))
            .order_by(WhatsAppMessage.timestamp)
            .all()
        )

        if len(messages) < MIN_MESSAGES_FOR_EMBED:
            continue

        segments = _build_segments(messages)
        segments = _merge_runts(segments)
        segments = _split_giants(segments)

        for segment in segments:
            if not segment:
                continue

            seg_id = _segment_id(segment)
            text = _format_segment(segment)
            all_new_segment_ids.add(seg_id)

            if len(text.strip()) < 50:
                continue

            metadata = _segment_metadata(segment)

            enqueued = EmbeddingService.enqueue(
                session, "whatsapp", seg_id, text, metadata,
                user_id=owner_id,
            )
            if enqueued:
                total_enqueued += 1

        session.commit()

    # Clean up stale embeddings for segments that no longer exist
    # (boundaries shifted because new messages arrived)
    existing_ids = set(
        row[0] for row in
        session.query(Embedding.source_id)
        .filter_by(source="whatsapp")
        .all()
    )
    stale_ids = existing_ids - all_new_segment_ids
    if stale_ids:
        session.query(Embedding).filter(
            Embedding.source == "whatsapp",
            Embedding.source_id.in_(stale_ids),
        ).delete(synchronize_session=False)
        session.commit()
        logger.info(f"Cleaned up {len(stale_ids)} stale WhatsApp embedding segments")

    if total_enqueued:
        logger.info(f"Enqueued {total_enqueued} WhatsApp conversation chunks for embedding")
    else:
        logger.info("All WhatsApp conversation chunks already embedded")

    return total_enqueued
