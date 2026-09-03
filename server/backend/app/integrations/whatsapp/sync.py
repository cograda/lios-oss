"""Conversation-window embedding for WhatsApp messages.

Instead of embedding individual messages (which are often too short to be
meaningful), this module groups messages into conversation segments based
on time proximity within the same chat, then embeds each segment as a unit.

Strategy:
1. Segment: Split each chat's messages at 30-min gaps
2. Merge runts: Segments under MIN_SEGMENT_CHARS get merged with neighbours
3. Split giants: Segments over MAX_CHUNK_CHARS get split at natural gaps
4. Result: Every message ends up in exactly one chunk

**Message-to-self chats are the documented exception** (see
`whatsapp_self_chat_jids` in `manifest.py`). Every rule above assumes a chat is
a *conversation*, where neighbouring messages are about the same thing and a
two-word reply only means something beside what preceded it. A self-chat is the
opposite: a capture surface, where each message is an independent, deliberately
terse note. Time-window grouping actively destroys those — a note typed today
gets merged with an unrelated one from last week (runt merging has no distance
limit), so a search matches a chunk holding two subjects and the caller cannot
tell which part earned the hit. So for those chats, one message is one chunk.
"""

import json
import logging
from datetime import timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.integrations.whatsapp.models import WhatsAppMessage
from app.services.embedding import Embedding, EmbeddingQueue, EmbeddingService

logger = logging.getLogger(__name__)

# --- Chunking parameters ---
GAP_THRESHOLD = timedelta(minutes=30)  # Time gap that starts a new segment
MIN_SEGMENT_CHARS = 150                # Merge segments smaller than this
MAX_CHUNK_CHARS = 3000                 # Split segments larger than this
MIN_MESSAGES_FOR_EMBED = 2            # Don't embed segments with fewer messages

# A self-chat note still has to clear a floor, or a bare "ok" becomes a chunk
# that matches everything weakly and nothing well. Far below MIN_SEGMENT_CHARS
# (150) on purpose: "Craft project in Claude" is 23 characters and is exactly
# the kind of note this exists to keep retrievable.
MIN_SELF_NOTE_CHARS = 12


def self_chat_map() -> dict[int, str]:
    """`{user_id: jid}` for configured message-to-self chats.

    ⚠️ **Keyed by user, and that is not cosmetic.** A WhatsApp `@lid` is scoped to
    the account that observed it, not global — the same LID string names different
    conversations under different bridges. Alex's self-chat LID matched 178 rows
    under Sam's bridge, all of them messages *received from a third party*. A
    flat set of JIDs therefore cannot express "this chat is a notebook"; only a
    (user, jid) pair can.

    Read per call rather than cached at import: `integration_config` is editable
    live from the dashboard, and this runs on a `*/30` cron — binding the value
    at import would mean a config change needs a container restart to take
    effect, which is the kind of surprise that gets debugged twice.

    Malformed keys are skipped rather than raised on: a hand-edited config value
    must not take the embedding pass down.
    """
    from app.plugin.config_store import plugin_config

    raw = plugin_config("whatsapp").whatsapp_self_chat_jids or {}
    out: dict[int, str] = {}
    for user_id, jid in raw.items():
        jid = (jid or "").strip()
        if not jid:
            continue
        try:
            out[int(user_id)] = jid
        except (TypeError, ValueError):
            logger.warning(f"[whatsapp] ignoring non-numeric self-chat user key {user_id!r}")
    return out


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


def _self_note_chunks(
    messages: list[WhatsAppMessage],
) -> list[tuple[str, str, list[WhatsAppMessage]]]:
    """One chunk per self-chat note, splitting any note too long to embed.

    Returns `(source_id, embeddable_text, segment)` triples.

    ⚠️ This exists because `_split_giants` cannot help here, and the reason is
    worth stating: it splits a segment at the largest *time gap between
    messages*, so it returns anything with `len(segment) < 2` untouched — and
    then `_format_segment` truncates at MAX_CHUNK_CHARS. A pasted 10,000-word
    plan therefore ends up searchable only up to its first 3,000 characters, with
    the rest silently unindexed. That predates self-chat support and applies to
    any single long message, but this chat is where whole architecture plans get
    pasted, so it is the path where it actually bites.

    The fix is deliberately confined to self-notes rather than applied in
    `_segment_id`: source ids key the embedding store, so changing the id format
    for conversation chunks would orphan every existing WhatsApp embedding and
    re-bill the entire back catalogue to say the same thing.
    """
    out: list[tuple[str, str, list[WhatsAppMessage]]] = []

    for msg in messages:
        segment = [msg]
        base_id = _segment_id(segment)
        text = _format_segment(segment, is_self_note=True, truncate=False)

        if len(text) <= MAX_CHUNK_CHARS:
            out.append((base_id, text, segment))
            continue

        header, _, body = text.partition("\n")
        # Reserve room for the header, which is re-prefixed onto every part so a
        # part retrieved on its own still says what it is.
        budget = MAX_CHUNK_CHARS - len(header) - 1
        for part_index, piece in enumerate(_split_on_words(body, budget), start=1):
            out.append((f"{base_id}:p{part_index}", f"{header}\n{piece}", segment))

    return out


def _split_on_words(text: str, budget: int) -> list[str]:
    """Break `text` into pieces of at most `budget` chars, preferring whitespace.

    Falls back to a hard cut when a single token exceeds the budget (a long URL,
    a base64 blob) — the alternative is an infinite loop or an oversized chunk.
    """
    if budget <= 0:
        return [text]

    pieces: list[str] = []
    remaining = text
    while len(remaining) > budget:
        cut = remaining.rfind(" ", 0, budget + 1)
        if cut <= 0:
            cut = budget
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        pieces.append(remaining)
    return [p for p in pieces if p]


def _format_segment(
    segment: list[WhatsAppMessage], *, is_self_note: bool = False, truncate: bool = True
) -> str:
    """Format a message segment into embeddable text.

    `truncate=False` returns the full text so a caller can split it into several
    chunks instead of losing the tail — see `_self_note_chunks`. The default
    stays truncating so the conversation path is byte-identical to before.
    """
    if not segment:
        return ""

    first = segment[0]
    chat_label = first.chat_name or first.chat_id or "unknown"

    if is_self_note:
        # The header is embedded along with the body, so it is worth being
        # accurate: labelling a captured note "chat with Alex" tells the
        # embedding it's a conversation with a third party, which is the one
        # thing it definitely is not.
        header = "[WhatsApp note to self]"
    elif first.is_group:
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
    if not truncate:
        return text
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


def _segment_metadata(segment: list[WhatsAppMessage], *, is_self_note: bool = False) -> str:
    """Build JSON metadata for a segment.

    `is_self_note` is carried through so a caller can tell a note the user wrote
    to themselves from a line of conversation. They read very differently — a
    self-note is a captured intention, so it deserves surfacing as one rather
    than as "a WhatsApp message from me".
    """
    first = segment[0]
    last = segment[-1]
    senders = list({
        m.sender_name or m.sender_id
        for m in segment
        if not m.is_from_me and (m.sender_name or m.sender_id)
    })

    meta = {
        "chat_id": first.chat_id,
        "chat_name": first.chat_name,
        "is_group": first.is_group,
        "start": first.timestamp.isoformat(),
        "end": last.timestamp.isoformat(),
        "message_count": len(segment),
        "participants": senders[:10],
    }
    if is_self_note:
        meta["is_self_note"] = True
    return json.dumps(meta)


def _delete_stale(
    session: Session,
    model,
    keep: set[tuple[int | None, str]],
    *,
    statuses: tuple[str, ...] | None,
) -> int:
    """Delete this source's rows whose `(user_id, source_id)` isn't in `keep`.

    One helper for both tables because the *only* difference between the two
    passes is a status filter, and the scoping rule is the part that must not
    drift between them — that drift is what made the pending pass inherit the
    embedded pass's user-blindness.

    Deletes grouped per owner rather than with a tuple `IN`: it keeps each
    parameter list bounded by one user's stale count, and avoids row-constructor
    NULL semantics entirely. WhatsApp always carries an owner (it's a per-user
    source), but the grouping is correct for a `None` owner too.
    """
    q = session.query(model.user_id, model.source_id).filter(model.source == "whatsapp")
    if statuses:
        q = q.filter(model.status.in_(statuses))

    stale: dict[int | None, list[str]] = {}
    for user_id, source_id in q.all():
        if (user_id, source_id) not in keep:
            stale.setdefault(user_id, []).append(source_id)

    if not stale:
        return 0

    removed = 0
    for user_id, source_ids in stale.items():
        owner = model.user_id.is_(None) if user_id is None else (model.user_id == user_id)
        cond = [model.source == "whatsapp", owner, model.source_id.in_(source_ids)]
        if statuses:
            cond.append(model.status.in_(statuses))
        removed += session.query(model).filter(*cond).delete(synchronize_session=False)

    session.commit()
    return removed


def embed_messages(session: Session, batch_size: int = 200) -> int:
    """Build conversation-window chunks and enqueue for embedding.

    Groups messages by chat, segments by time windows, merges small
    segments, splits large ones, then enqueues each chunk.

    Cleans up old embeddings for segments whose boundaries have shifted
    due to new messages arriving.

    Returns the number of chunks enqueued.
    """
    self_map = self_chat_map()

    # Get all chats that have text messages, per owning user — segments
    # must never mix two users' message caches, and each segment's
    # embedding carries its owner's user_id for search scoping.
    #
    # The two-message floor exists because one line of a conversation is rarely
    # meaningful alone. A self-chat note is the opposite — it's meaningful
    # *because* it stands alone — so those chats are exempt. The exemption is
    # matched on the (user_id, chat_id) *pair*, never the JID alone: a LID is
    # account-scoped, so the same string under another user's bridge is a
    # different conversation entirely. An empty `self_map` makes the OR term
    # constant-false and the query behaves exactly as it did before.
    self_pairs = [
        and_(WhatsAppMessage.user_id == uid, WhatsAppMessage.chat_id == jid)
        for uid, jid in self_map.items()
    ]
    chat_keys = (
        session.query(WhatsAppMessage.user_id, WhatsAppMessage.chat_id)
        .filter(WhatsAppMessage.body.isnot(None))
        .group_by(WhatsAppMessage.user_id, WhatsAppMessage.chat_id)
        .having(
            or_(
                func.count(WhatsAppMessage.id) >= MIN_MESSAGES_FOR_EMBED,
                *self_pairs,
            )
        )
        .all()
    )

    if not chat_keys:
        logger.info("No WhatsApp chats with enough messages to embed")
        return 0

    total_enqueued = 0
    # `(user_id, source_id)` pairs, never bare source_ids.
    #
    # ⚠️ `_segment_id` is `{chat_id}:{start}:{end}` with no owner in it, and a
    # WhatsApp `@lid` is account-scoped — the same `chat_id` legitimately appears
    # under both bridges naming different conversations (measured: 27 rows for
    # user 1, 178 for user 2, on one LID). So a source_id identifies a segment only
    # *within* an owner. `EmbeddingService.enqueue` already keys identity on
    # `(source, source_id, user_id)` for exactly this reason; these cleanup passes
    # did not, which left them asymmetric with the layer they clean up after.
    all_new_keys: set[tuple[int | None, str]] = set()

    for owner_id, chat_id in chat_keys:
        messages = (
            session.query(WhatsAppMessage)
            .filter_by(user_id=owner_id, chat_id=chat_id)
            .filter(WhatsAppMessage.body.isnot(None))
            .order_by(WhatsAppMessage.timestamp)
            .all()
        )

        # Pair match, plus the `is_from_me` invariant: if any message in a chat
        # configured as this user's notebook was *received*, the JID does not mean
        # what the config claims under this account, so treat the whole chat as
        # the conversation it actually is rather than filing someone else's words
        # as the user's own notes.
        is_self = self_map.get(owner_id) == chat_id and all(m.is_from_me for m in messages)
        if self_map.get(owner_id) == chat_id and not is_self:
            logger.warning(
                f"[whatsapp] chat {chat_id} is configured as user {owner_id}'s "
                "self-chat but contains received messages — treating as a "
                "conversation. Check `whatsapp_self_chat_jids`: a @lid is "
                "account-scoped and may name a different chat under this bridge."
            )

        if not is_self and len(messages) < MIN_MESSAGES_FOR_EMBED:
            continue

        if is_self:
            # One note, one chunk. `_merge_runts` is deliberately skipped —
            # merging is the bug here, not the safeguard — and oversized notes
            # are split on their own body rather than at message boundaries,
            # which a single message doesn't have.
            chunks = _self_note_chunks(messages)
        else:
            segments = _build_segments(messages)
            segments = _merge_runts(segments)
            segments = _split_giants(segments)
            chunks = [(_segment_id(s), _format_segment(s), s) for s in segments if s]

        for seg_id, text, segment in chunks:
            if not segment:
                continue

            all_new_keys.add((owner_id, seg_id))

            # The 50-char floor is measured on the *formatted* text, which
            # carries a chat header and a timestamp — roughly 40 characters of
            # scaffolding. For a one-line note that floor is mostly testing the
            # header, so self-chats are measured on the note itself instead.
            if is_self:
                if len((segment[0].body or "").strip()) < MIN_SELF_NOTE_CHARS:
                    continue
            elif len(text.strip()) < 50:
                continue

            metadata = _segment_metadata(segment, is_self_note=is_self)

            enqueued = EmbeddingService.enqueue(
                session, "whatsapp", seg_id, text, metadata,
                user_id=owner_id,
            )
            if enqueued:
                total_enqueued += 1

        session.commit()

    # Clean up stale rows for segments that no longer exist (boundaries shifted
    # because new messages arrived), in both the embedded and the pending tables.
    #
    # Scoped per owner. Deleting on `source_id` alone — which both passes used to
    # do — is unsafe now that a `chat_id` is known to be shared between users: one
    # user's chat dropping out of `chat_keys` (falling below the two-message floor,
    # say) would take the other user's identically-keyed row with it.
    stale_embedded = _delete_stale(
        session, Embedding, all_new_keys, statuses=None,
    )
    if stale_embedded:
        logger.info(f"Cleaned up {stale_embedded} stale WhatsApp embedding segments")

    # The pending pass exists because the embedded pass above is not enough on its
    # own: a superseded chunk that hadn't been processed yet survived, got embedded
    # — billed — and was then deleted by the *next* run. Two costs: paying to embed
    # text already scheduled for deletion, and a window where search returns both
    # the old and new cuts of the same messages, which is the "a stale copy competes
    # with its own live content" failure this project already hit with `.stversions`.
    #
    # Only `pending`: a `processing` row is mid-flight in the worker, and deleting it
    # underneath would be a race for no benefit — it becomes an ordinary stale
    # `Embedding` and the pass above collects it next run.
    stale_queued = _delete_stale(
        session, EmbeddingQueue, all_new_keys, statuses=("pending",),
    )
    if stale_queued:
        logger.info(f"Dropped {stale_queued} superseded WhatsApp chunks before embedding")

    if total_enqueued:
        logger.info(f"Enqueued {total_enqueued} WhatsApp conversation chunks for embedding")
    else:
        logger.info("All WhatsApp conversation chunks already embedded")

    return total_enqueued
