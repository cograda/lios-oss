"""Snag register scoping (F5, hardening 2026-08-08, db tier).

Snags themselves stay household-shared by design (renovation is joint
work — no UserOwnedMixin on `Snag`). Two things around the edges of that
shared table are per-user and are what this file exercises:

  1. Attaching MediaItem evidence to a snag (`snag_update`'s
     `attach_media_ids`) must be scoped to the calling user's own
     MediaItem rows — MediaItem is UserOwnedMixin, so without the check a
     shared snag becomes an implicit channel for one user's WhatsApp media
     to reach the other.
  2. `snag_source_messages.message_ref` uniqueness is per-user
     (`(user_id, message_ref)`, not bare `message_ref`) — a group-chat
     message both WhatsApp bridges ingest shares a `message_ref`, and the
     old global-unique constraint made the second user's `snag_capture`
     silently no-op against the first user's already-captured row.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _real_vault_root(tmp_path, monkeypatch):
    """snag_update/snag_capture render the vault note as part of the write
    (real behavior, not stubbed) — give it a real directory for both test
    users instead of the default `/vaults`, which doesn't exist on a test
    runner. Mirrors tests/test_tool_call_runs.py::test_snag_add_records_affected.
    """
    from app.config import settings

    (tmp_path / "alex").mkdir()
    (tmp_path / "sam").mkdir()
    monkeypatch.setattr(settings, "vaults_root_path", str(tmp_path))
    # The render step also exports snag evidence out of the media store —
    # point media_root at tmp_path too, or CI dies on the real `/data/media`
    # (unwritable on a hosted runner).
    (tmp_path / "media").mkdir()
    monkeypatch.setattr(settings, "media_root", str(tmp_path / "media"))


# ---------------------------------------------------------------------------
# 1. Media ownership at attach time
# ---------------------------------------------------------------------------

def _make_snag(session, uid="SNAG-9001"):
    from app.integrations.snags.models import Snag

    snag = Snag(
        uid=uid, title="Test snag", description="test", room="Kitchen",
        trade="unknown", severity="minor", status="open",
        reported_at=datetime.now(timezone.utc),
    )
    session.add(snag)
    session.commit()
    return snag


def _make_media_item(session, user_id: int, message_ref="MSG-1"):
    from app.integrations.media.models import MediaItem

    item = MediaItem(
        user_id=user_id, source="whatsapp", message_ref=message_ref,
        media_type="image",
    )
    session.add(item)
    session.commit()
    return item


class TestAttachMediaOwnership:
    def test_attach_other_users_media_is_rejected(self, db_session):
        """User 1 must not be able to attach user 2's MediaItem as evidence."""
        from app.integrations.snags.models import SnagMedia
        from app.integrations.snags.tools import snag_update_handler

        snag = _make_snag(db_session)
        other_users_media = _make_media_item(db_session, user_id=2)

        with use_user(1):
            out = snag_update_handler(
                db_session,
                {"uid": snag.uid, "attach_media_ids": [other_users_media.id]},
            )

        import json
        result = json.loads(out)
        assert result.get("status") == "error"
        assert not (
            db_session.query(SnagMedia)
            .filter(SnagMedia.snag_id == snag.id, SnagMedia.media_item_id == other_users_media.id)
            .one_or_none()
        )

    def test_attach_own_media_succeeds(self, db_session):
        """Sanity check the rejection above isn't blocking everything —
        the same user's own MediaItem attaches cleanly."""
        from app.integrations.snags.models import SnagMedia
        from app.integrations.snags.tools import snag_update_handler

        snag = _make_snag(db_session, uid="SNAG-9002")
        own_media = _make_media_item(db_session, user_id=1, message_ref="MSG-2")

        with use_user(1):
            out = snag_update_handler(
                db_session,
                {"uid": snag.uid, "attach_media_ids": [own_media.id]},
            )

        import json
        result = json.loads(out)
        assert result.get("updated") is not None
        assert (
            db_session.query(SnagMedia)
            .filter(SnagMedia.snag_id == snag.id, SnagMedia.media_item_id == own_media.id)
            .one_or_none()
            is not None
        )

    def test_attach_nonexistent_media_is_rejected(self, db_session):
        """A media id that doesn't exist at all fails the same way as
        someone else's — no distinguishable "does this id exist" oracle."""
        from app.integrations.snags.tools import snag_update_handler

        snag = _make_snag(db_session, uid="SNAG-9003")

        with use_user(1):
            out = snag_update_handler(
                db_session, {"uid": snag.uid, "attach_media_ids": [999999]},
            )

        import json
        result = json.loads(out)
        assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# 2. Per-user message_ref dedupe in snag_capture
# ---------------------------------------------------------------------------

def _make_whatsapp_message(session, user_id: int, message_id: str, body: str):
    from app.integrations.whatsapp.models import WhatsAppMessage

    msg = WhatsAppMessage(
        user_id=user_id, message_id=message_id, chat_id="group-1",
        chat_name="Renovation Group", sender_id="+353-1", sender_name="Someone",
        is_group=True, timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
        message_type="text", body=body, is_from_me=False,
    )
    session.add(msg)
    session.commit()
    return msg


class TestPerUserMessageRefDedupe:
    def test_same_message_ref_two_users_creates_two_snags(self, db_session):
        """The same shared-group message_id, ingested by both users' bridges,
        must not make the second user's capture see it as already-captured."""
        from app.integrations.snags.capture import capture_whatsapp_snags
        from app.integrations.snags.models import Snag, SnagSourceMessage

        shared_ref = "SHARED-MSG-1"
        _make_whatsapp_message(db_session, 1, shared_ref, "Snag - Kitchen - tiler - crack in tile")
        _make_whatsapp_message(db_session, 2, shared_ref, "Snag - Kitchen - tiler - crack in tile")

        with use_user(1):
            result1 = capture_whatsapp_snags(db_session, since_days=7)
        with use_user(2):
            result2 = capture_whatsapp_snags(db_session, since_days=7)

        assert result1["snags_created"] == 1
        assert result2["snags_created"] == 1, (
            "user 2's capture silently deduped against user 1's snag_source_messages row"
        )
        assert db_session.query(Snag).count() == 2

        refs = (
            db_session.query(SnagSourceMessage)
            .filter(SnagSourceMessage.message_ref == shared_ref)
            .all()
        )
        assert len(refs) == 2
        assert {r.user_id for r in refs} == {1, 2}

    def test_repeat_capture_same_user_is_idempotent(self, db_session):
        """Re-running capture as the SAME user must not double-create."""
        from app.integrations.snags.capture import capture_whatsapp_snags
        from app.integrations.snags.models import Snag

        ref = "REPEAT-MSG-1"
        _make_whatsapp_message(db_session, 1, ref, "Snag - Bathroom - plumber - leaking tap")

        with use_user(1):
            first = capture_whatsapp_snags(db_session, since_days=7)
            second = capture_whatsapp_snags(db_session, since_days=7)

        assert first["snags_created"] == 1
        assert second["snags_created"] == 0
        assert db_session.query(Snag).count() == 1
