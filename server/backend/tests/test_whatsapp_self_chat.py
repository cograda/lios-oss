"""Message-to-self chats are a capture surface, not a conversation.

WhatsApp's "Message yourself" chat is where Alex drops ideas, links and short
notes. Every rule in `whatsapp/sync.py` was written for conversations, and two
of them actively damage notes:

  1. `_merge_runts` merges any segment under 150 characters into its nearest
     neighbour, *with no distance limit* — so a note typed today is glued to an
     unrelated one from days earlier and a search hit cannot be attributed to
     either.
  2. The 50-character floor is measured on the formatted text, which carries ~40
     characters of chat header and timestamp scaffolding. For a one-line note
     that floor mostly measures the header.

Both are correct for a conversation and wrong for a notebook, so the fix is a
branch keyed on configured self-chat JIDs, not a change to the shared rules.

The fixture data is the real thing: two notes three days apart, which is exactly
the pair that merged in production.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.whatsapp import sync
from app.integrations.whatsapp.models import WhatsAppMessage

SELF_JID = "50822398382303@lid"
OTHER_JID = "198758251913277@lid"


@pytest.fixture
def self_chat_configured(monkeypatch):
    """Map user 1's self-chat to SELF_JID without touching integration_config.

    A dict, not a set: a WhatsApp @lid is account-scoped, so "this chat is a
    notebook" is only expressible as a (user, jid) pair. See `sync.self_chat_map`.
    """
    monkeypatch.setattr(sync, "self_chat_map", lambda: {1: SELF_JID})


def _msg(chat_id: str, body: str, when: datetime, **kw) -> WhatsAppMessage:
    return WhatsAppMessage(
        user_id=kw.pop("user_id", 1),
        message_id=kw.pop("message_id", f"{chat_id}-{int(when.timestamp())}"),
        chat_id=chat_id,
        chat_name=kw.pop("chat_name", "Alex"),
        sender_id="me",
        sender_name=None,
        is_group=False,
        timestamp=when,
        message_type="text",
        body=body,
        is_from_me=kw.pop("is_from_me", True),
        **kw,
    )


BASE = datetime(2026, 8, 14, 11, 28, tzinfo=timezone.utc)


class TestBannerIsCleaned:
    """The formatted banner must be *stripped* before embedding, like the chat
    and group banners it sits beside.

    Missing it does more than leave a constant prefix diluting every vector:
    `RE_CHAT_PREFIX` matches across the newline that the surviving banner leaves,
    so it consumes the real message's timestamp instead of the banner, and the
    embedded text begins mid-timestamp ("22] me: …").
    """

    def test_note_to_self_banner_is_stripped(self):
        from app.integrations.embedding import cleaning

        cleaned = cleaning.clean(
            "[WhatsApp note to self]\n[2026-08-17 11:22] me: Craft project in Claude",
            "whatsapp",
        )
        assert cleaned == "Craft project in Claude"

    def test_chat_and_group_banners_are_unaffected(self):
        from app.integrations.embedding import cleaning

        for banner in ("[WhatsApp chat: Joe Doyle]", "[WhatsApp group: Family]"):
            cleaned = cleaning.clean(
                f"{banner}\n[2026-08-17 11:22] me: Craft project in Claude", "whatsapp"
            )
            assert cleaned == "Craft project in Claude"


class TestFormatting:
    """Header text is embedded alongside the body, so it has to be true."""

    def test_self_note_is_not_labelled_a_chat_with_someone(self):
        note = _msg(SELF_JID, "Craft project in Claude", BASE)
        text = sync._format_segment([note], is_self_note=True)

        assert text.startswith("[WhatsApp note to self]")
        # The bug this avoids: telling the embedding that a note the user wrote
        # to themselves is a conversation with a third party named Alex.
        assert "chat: Alex" not in text
        assert "Craft project in Claude" in text

    def test_conversation_formatting_is_unchanged(self):
        m = _msg(OTHER_JID, "are you around later", BASE, chat_name="Joe Doyle",
                 is_from_me=False)
        text = sync._format_segment([m])
        assert text.startswith("[WhatsApp chat: Joe Doyle]")

    def test_metadata_flags_a_self_note(self):
        note = _msg(SELF_JID, "Craft project in Claude", BASE)
        flagged = json.loads(sync._segment_metadata([note], is_self_note=True))
        plain = json.loads(sync._segment_metadata([note]))

        assert flagged["is_self_note"] is True
        # Absent rather than False on a conversation chunk, so existing
        # embeddings' metadata doesn't change shape.
        assert "is_self_note" not in plain


@pytest.mark.db
class TestSelfChatChunking:
    def _seed(self, session, rows):
        for r in rows:
            session.add(r)
        session.commit()

    def _chunks(self, session):
        """(source_id, text) for every queued whatsapp chunk."""
        from app.services.embedding import EmbeddingQueue

        return [
            (q.source_id, q.content)
            for q in session.query(EmbeddingQueue).filter_by(source="whatsapp").all()
        ]

    def test_each_note_becomes_its_own_chunk(self, db_session, self_chat_configured):
        """The headline fix. These two notes are three days apart and about
        completely different things; runt merging put them in one chunk."""
        self._seed(db_session, [
            _msg(SELF_JID, "I'm at the gate now, the code didn't work", BASE),
            _msg(SELF_JID, "Craft project in Claude — look at the vault", BASE + timedelta(days=3)),
        ])

        sync.embed_messages(db_session)
        chunks = self._chunks(db_session)

        assert len(chunks) == 2, "notes were merged into one chunk"
        gate = [t for _, t in chunks if "gate" in t]
        craft = [t for _, t in chunks if "Craft" in t]
        assert len(gate) == 1 and len(craft) == 1
        # The point of the whole change: neither chunk contaminates the other.
        assert "Craft" not in gate[0]
        assert "gate" not in craft[0]

    def test_a_lone_note_is_embedded_at_all(self, db_session, self_chat_configured):
        """A self-chat with one message used to be excluded by the two-message
        HAVING clause, so a first note was invisible to search entirely."""
        self._seed(db_session, [_msg(SELF_JID, "Order the second SCD41 sensor", BASE)])

        sync.embed_messages(db_session)
        assert len(self._chunks(db_session)) == 1

    def test_short_note_survives_the_length_floor(self, db_session, self_chat_configured):
        """23 characters, and the note this whole change was prompted by."""
        self._seed(db_session, [_msg(SELF_JID, "Craft project in Claude", BASE)])

        sync.embed_messages(db_session)
        chunks = self._chunks(db_session)
        assert len(chunks) == 1
        assert "Craft project in Claude" in chunks[0][1]

    def test_trivial_note_is_still_skipped(self, db_session, self_chat_configured):
        """A floor still exists — 'ok' as a chunk matches everything weakly."""
        self._seed(db_session, [_msg(SELF_JID, "ok", BASE)])

        sync.embed_messages(db_session)
        assert self._chunks(db_session) == []

    def test_a_pasted_wall_of_text_is_split_not_truncated(self, db_session, self_chat_configured):
        """`_split_giants` cannot help a single message — it splits at time gaps
        *between* messages and returns anything shorter than two untouched, after
        which `_format_segment` truncates at MAX_CHUNK_CHARS.

        So a pasted plan was searchable only up to its first 3,000 characters and
        the rest was silently unindexed. This chat is where Alex pastes whole
        architecture plans, so that is not a hypothetical.
        """
        huge = ("This is a pasted architecture plan. " * 300) + "FINAL-LINE-MARKER"
        self._seed(db_session, [_msg(SELF_JID, huge, BASE)])

        sync.embed_messages(db_session)
        chunks = self._chunks(db_session)

        assert len(chunks) >= 2, "a giant note was embedded as one oversized chunk"
        assert all(len(t) <= sync.MAX_CHUNK_CHARS for _, t in chunks)
        # Parts are distinct rows, not one id overwriting itself.
        assert len({sid for sid, _ in chunks}) == len(chunks)
        # The precise thing truncation destroyed: everything past char 3,000.
        # Under the old behaviour this marker was simply not in the index.
        assert any("FINAL-LINE-MARKER" in t for _, t in chunks)
        # And near-total recovery overall — a phrase straddling a part boundary
        # is normal splitting, so this is not asserted at exactly 300.
        recovered = sum(t.count("pasted architecture plan") for _, t in chunks)
        assert recovered >= 299, f"lost content: {recovered}/300 sentences indexed"

    def test_an_unsplittable_token_does_not_hang(self, db_session, self_chat_configured):
        """A single 5,000-character token (a long URL, a pasted key) has no
        whitespace to break on — the word-boundary search must fall back to a
        hard cut rather than looping forever."""
        self._seed(db_session, [_msg(SELF_JID, "https://example.test/" + "a" * 5000, BASE)])

        sync.embed_messages(db_session)
        chunks = self._chunks(db_session)
        assert len(chunks) >= 2
        assert all(len(t) <= sync.MAX_CHUNK_CHARS for _, t in chunks)

    def test_conversations_still_use_conversation_windows(self, db_session, self_chat_configured):
        """The regression guard: a real chat must keep its 30-minute grouping.
        Three messages two minutes apart are one chunk, not three."""
        self._seed(db_session, [
            _msg(OTHER_JID, "are you around for lunch this week at all", BASE,
                 chat_name="Joe Doyle", is_from_me=False),
            _msg(OTHER_JID, "yeah Thursday suits me best I think", BASE + timedelta(minutes=2),
                 chat_name="Joe Doyle"),
            _msg(OTHER_JID, "grand, Thursday it is then, usual place", BASE + timedelta(minutes=4),
                 chat_name="Joe Doyle", is_from_me=False),
        ])

        sync.embed_messages(db_session)
        chunks = self._chunks(db_session)
        assert len(chunks) == 1
        assert "Thursday" in chunks[0][1]

    def test_unconfigured_changes_nothing(self, db_session, monkeypatch):
        """With no JIDs configured the self-chat is treated as any other chat —
        so enabling this is an explicit act, and the default path is untouched."""
        monkeypatch.setattr(sync, "self_chat_map", lambda: {})
        self._seed(db_session, [
            _msg(SELF_JID, "I'm at the gate now, the code didn't work", BASE),
            _msg(SELF_JID, "Craft project in Claude — look at the vault", BASE + timedelta(days=3)),
        ])

        sync.embed_messages(db_session)
        # Merged by `_merge_runts`, which is the old behaviour exactly.
        assert len(self._chunks(db_session)) == 1

    def test_the_same_jid_under_another_user_is_not_a_self_chat(
        self, db_session, self_chat_configured
    ):
        """⚠️ Regression test for a bug that reached production on 2026-08-17.

        A WhatsApp `@lid` is scoped to the account that observed it, not global.
        Alex's self-chat LID matched **178 rows under Sam's bridge**, and those
        were messages *received from a third party*. The config was a flat list of
        JIDs, so the sweep treated all of them as her notes to self and filed 59
        into her inbox. Nothing crossed users — rows carry their owner — but
        "user-scoped rows" is a different guarantee from "user-scoped identifiers",
        and only the second one was needed.

        The config is now `{user_id: jid}` and matching is on the pair.
        """
        self._seed(db_session, [
            _msg(SELF_JID, "Alex's note about the gate sensor wiring", BASE,
                 user_id=1, message_id="u1-a"),
            # Same JID string, different account: under user 2's bridge this LID
            # is a conversation, and these are messages she RECEIVED.
            _msg(SELF_JID, "hiya, are you around for a chat later today?", BASE,
                 user_id=2, message_id="u2-a", is_from_me=False),
            _msg(SELF_JID, "no bother, talk in a while so", BASE + timedelta(minutes=3),
                 user_id=2, message_id="u2-b", is_from_me=False),
        ])

        sync.embed_messages(db_session)

        from app.services.embedding import EmbeddingQueue

        rows = db_session.query(EmbeddingQueue).filter_by(source="whatsapp").all()
        by_owner = {}
        for r in rows:
            flagged = json.loads(r.metadata_json or "{}").get("is_self_note", False)
            by_owner.setdefault(r.user_id, []).append((r.content, flagged))

        # `is_self_note` in the chunk metadata is the durable signal — the banner
        # itself is stripped by the cleaner before the text is queued.
        assert len(by_owner[1]) == 1
        content, flagged = by_owner[1][0]
        assert "gate sensor" in content
        assert flagged is True, "user 1's real note lost its self-note flag"

        # User 2's messages are a conversation, and must NOT be marked as notes
        # she wrote to herself.
        assert len(by_owner[2]) == 1
        other_content, other_flagged = by_owner[2][0]
        assert other_flagged is False, "another user's received messages were filed as self-notes"
        # And no leakage in either direction.
        assert "gate sensor" not in other_content
        assert "are you around" not in content

    def test_a_received_message_disqualifies_a_configured_self_chat(
        self, db_session, self_chat_configured
    ):
        """Belt to the pair-match's braces, for a misconfigured JID.

        In a genuine self-chat every message is `is_from_me`. One that isn't is
        proof the JID names something else under this account — so the chat falls
        back to conversation handling rather than filing another person's words as
        the user's own notes. This is what makes a wrong config value degrade
        instead of mislabelling.
        """
        self._seed(db_session, [
            _msg(SELF_JID, "this one really is my own note about the gate", BASE,
                 user_id=1, message_id="a"),
            _msg(SELF_JID, "but this one arrived from somebody else entirely", BASE,
                 user_id=1, message_id="b", is_from_me=False),
        ])

        sync.embed_messages(db_session)
        chunks = self._chunks(db_session)

        # Treated as a conversation: grouped into one window, not flagged.
        assert len(chunks) == 1
        from app.services.embedding import EmbeddingQueue

        row = db_session.query(EmbeddingQueue).filter_by(source="whatsapp").one()
        assert json.loads(row.metadata_json or "{}").get("is_self_note", False) is False


@pytest.mark.db
class TestSupersededChunksAreNotEmbedded:
    """Re-cutting segment boundaries must not leave the old cut queued.

    The cleanup at the end of `embed_messages` only ever pruned `Embedding`, so a
    superseded chunk that hadn't been processed yet survived, got embedded (paid
    for), and was deleted by the *next* run. Two costs: paying to embed text
    already scheduled for deletion, and a window where search returns both the old
    and new cuts of the same messages — the "a stale copy competes with its own
    live content" failure this project already hit with `.stversions`.

    Enabling self-note chunking re-cuts every boundary in a chat at once, which is
    the maximal case.
    """

    def test_the_old_merged_chunk_is_dropped_when_notes_are_split(
        self, db_session, monkeypatch
    ):
        from app.integrations.embedding.models import EmbeddingQueue

        rows = [
            _msg(SELF_JID, "I'm at the gate now and the code did not work", BASE, message_id="a"),
            _msg(SELF_JID, "Craft project in Claude, look at the vault", BASE + timedelta(minutes=1),
                 message_id="b"),
        ]
        for r in rows:
            db_session.add(r)
        db_session.commit()

        # First pass: not configured, so the two notes merge into one window.
        monkeypatch.setattr(sync, "self_chat_map", lambda: {})
        sync.embed_messages(db_session)
        before = db_session.query(EmbeddingQueue).filter_by(source="whatsapp").all()
        assert len(before) == 1, "expected the old merged cut"
        merged_id = before[0].source_id

        # Now enable self-note chunking — every boundary in the chat changes.
        monkeypatch.setattr(sync, "self_chat_map", lambda: {1: SELF_JID})
        sync.embed_messages(db_session)

        after = db_session.query(EmbeddingQueue).filter_by(source="whatsapp").all()
        ids = {r.source_id for r in after}
        assert merged_id not in ids, "the superseded merged chunk was left queued to be billed"
        assert len(after) == 2, f"expected exactly the two per-note chunks, got {len(after)}"

    def test_a_chunk_being_processed_is_left_alone(self, db_session, monkeypatch):
        """A `processing` row is mid-flight in the worker; deleting it underneath
        would be a race for no gain — it becomes an ordinary stale `Embedding` and
        the existing pass collects it next run."""
        from app.integrations.embedding.models import EmbeddingQueue

        db_session.add(_msg(SELF_JID, "a note that will be re-cut shortly", BASE, message_id="a"))
        db_session.commit()

        monkeypatch.setattr(sync, "self_chat_map", lambda: {})
        db_session.add(EmbeddingQueue(
            source="whatsapp", source_id="50822398382303@lid:1:1",
            user_id=1, content="an in-flight chunk", content_hash="x" * 32,
            status="processing",
        ))
        db_session.commit()

        monkeypatch.setattr(sync, "self_chat_map", lambda: {1: SELF_JID})
        sync.embed_messages(db_session)

        survivors = {
            r.source_id for r in
            db_session.query(EmbeddingQueue).filter_by(source="whatsapp", status="processing").all()
        }
        assert "50822398382303@lid:1:1" in survivors


@pytest.mark.db
class TestStaleCleanupIsPerOwner:
    """⚠️ `_segment_id` is `{chat_id}:{start}:{end}` — no owner in it.

    Since a `@lid` is account-scoped, the same `chat_id` legitimately appears under
    both bridges naming different conversations, so a `source_id` identifies a
    segment only *within* an owner. `EmbeddingService.enqueue` already keys identity
    on `(source, source_id, user_id)` for exactly this reason; both cleanup passes
    deleted on `source_id` alone, which left them asymmetric with the layer they
    clean up after.

    This also caught me out diagnostically: 18 of user 2's perfectly good chunks
    looked like user 1's stale ones, because the query I wrote to find stale rows
    filtered on `chat_id` and forgot `user_id` — the same class of mistake as the
    bug being investigated.
    """

    def test_recutting_one_users_chat_leaves_the_others_rows_alone(
        self, db_session, self_chat_configured
    ):
        from app.integrations.embedding.models import Embedding, EmbeddingQueue

        # User 1: a self-note. User 2: a real conversation on the SAME chat_id.
        db_session.add_all([
            _msg(SELF_JID, "my own note about the gate sensor wiring", BASE,
                 user_id=1, message_id="u1-a"),
            _msg(SELF_JID, "are you around for a chat later on today?", BASE,
                 user_id=2, message_id="u2-a", is_from_me=False),
            _msg(SELF_JID, "yeah grand, give me a shout after six", BASE + timedelta(minutes=2),
                 user_id=2, message_id="u2-b", is_from_me=False),
        ])
        db_session.commit()
        sync.embed_messages(db_session)

        # Promote everything to embedded so the *embedded* pass is what's tested.
        for q in db_session.query(EmbeddingQueue).filter_by(source="whatsapp").all():
            db_session.add(Embedding(
                source=q.source, source_id=q.source_id, user_id=q.user_id,
                chunk_text=q.content, content_hash=q.content_hash,
                metadata_json=q.metadata_json,
            ))
            q.status = "done"
        db_session.commit()

        u2_before = {
            r.source_id for r in
            db_session.query(Embedding).filter_by(source="whatsapp", user_id=2).all()
        }
        assert u2_before, "fixture failed to give user 2 any embeddings"

        # Now re-cut user 1's chat by adding a note — user 2's segments are untouched
        # by that, and their rows must survive it.
        db_session.add(_msg(SELF_JID, "a second note, about the heat pump curve",
                            BASE + timedelta(days=1), user_id=1, message_id="u1-b"))
        db_session.commit()
        sync.embed_messages(db_session)

        u2_after = {
            r.source_id for r in
            db_session.query(Embedding).filter_by(source="whatsapp", user_id=2).all()
        }
        assert u2_after == u2_before, (
            "user 2's embeddings were deleted by a cleanup pass scoped only to source_id"
        )

    def test_a_genuinely_stale_row_is_still_removed_for_its_own_owner(
        self, db_session, self_chat_configured
    ):
        """The scoping must not turn the cleanup into a no-op."""
        from app.integrations.embedding.models import Embedding

        db_session.add(_msg(SELF_JID, "a real note that will be kept", BASE,
                            user_id=1, message_id="u1-a"))
        db_session.add(Embedding(
            source="whatsapp", source_id=f"{SELF_JID}:1:1", user_id=1,
            chunk_text="an old cut nobody generates any more", content_hash="z" * 32,
        ))
        db_session.commit()

        sync.embed_messages(db_session)

        left = {
            r.source_id for r in
            db_session.query(Embedding).filter_by(source="whatsapp", user_id=1).all()
        }
        assert f"{SELF_JID}:1:1" not in left

    def test_one_owners_stale_row_is_not_spared_by_the_others_live_id(
        self, db_session, self_chat_configured
    ):
        """The case that actually separates per-owner from user-blind cleanup.

        User 2 holds a row whose `source_id` user 1 *currently generates*. Comparing
        bare source_ids finds that id in the keep-set and spares user 2's row — so a
        stale chunk sits in their search results indefinitely, kept alive by another
        person's data. Comparing `(user_id, source_id)` deletes it, correctly.

        Written deliberately as an invariant test rather than a reproduction: the
        live trigger needs a timestamp coincidence between two users' segments, so
        this is narrow in practice. It is still the asymmetry worth closing, because
        `EmbeddingService.enqueue` already keys on the owner and these passes did not.
        """
        from app.integrations.embedding.models import Embedding, EmbeddingQueue

        db_session.add(_msg(SELF_JID, "my own note about the gate sensor wiring", BASE,
                            user_id=1, message_id="u1-a"))
        db_session.commit()
        sync.embed_messages(db_session)

        # An id user 1 legitimately generates right now.
        live_id = db_session.query(EmbeddingQueue).filter_by(
            source="whatsapp", user_id=1
        ).one().source_id

        # User 2 holds a row under that same id — stale for them, live for user 1.
        db_session.add(Embedding(
            source="whatsapp", source_id=live_id, user_id=2,
            chunk_text="an old cut of user 2's conversation", content_hash="q" * 32,
        ))
        db_session.commit()

        sync.embed_messages(db_session)

        survived = db_session.query(Embedding).filter_by(
            source="whatsapp", source_id=live_id, user_id=2
        ).first()
        assert survived is None, (
            "user 2's stale row was spared because user 1 generates the same source_id"
        )
