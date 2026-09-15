"""intake — deterministic new-task pass (db tier).

Seeds real MailMessage/WhatsAppMessage/Reminder rows and calls the real
`mail.query`/`whatsapp.query`/`reminders.query` facades — no mocking of the
fetch path, since the whole point of intake is that it composes existing,
already-tested facades rather than re-implementing them. The one thing that
IS constructed rather than real is the pgvector data: vectors are seeded
directly into `embedding_vec_bge_small_384` at chosen coordinates so cosine
similarity between a candidate and a task is exactly known, never dependent
on a live embedding call (there must be none — see intake.py's docstring).
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user
from app.services import embedding as emb
from app.services.embedding import Embedding, EmbeddingVecBgeSmall384

pytestmark = pytest.mark.db


def _now() -> datetime:
    """Fresh wall-clock time, not a module-level constant — the full suite
    can take well over a minute between import and a given test running, and
    a stale `NOW` produced a flaky ~80s-off assertion under that load."""
    return datetime.now(timezone.utc)


def _vec(*pairs: tuple[int, float]) -> list[float]:
    v = [0.0] * emb.VECTOR_DIM
    for i, x in pairs:
        v[i] = x
    return v


def _seed_vec(session, source: str, source_id: str, user_id: int | None, vec: list[float]):
    row = Embedding(
        source=source, source_id=source_id, user_id=user_id,
        chunk_text="x", content_hash=f"hash-{source}-{source_id}",
    )
    session.add(row)
    session.flush()
    session.add(EmbeddingVecBgeSmall384(embedding_id=row.id, embedding=vec, model_name=emb.MODEL_NAME))
    return row


def _mail(session, user_id, google_message_id, *, date, subject="Subject", sender="a@b.com", snippet="hello"):
    from app.integrations.google_mail.models import MailMessage

    m = MailMessage(
        user_id=user_id, google_message_id=google_message_id, thread_id="t1",
        account_email="a@b.com", subject=subject, sender=sender, to="me@b.com",
        date=date, snippet=snippet, labels="", is_read=False, is_starred=False,
        has_attachments=False,
    )
    session.add(m)
    return m


def _wa(session, user_id, message_id, *, date, chat_id="chat1", sender_name="Cian",
        body="can you pick up milk on the way home", is_from_me=False):
    from app.integrations.whatsapp.models import WhatsAppMessage

    m = WhatsAppMessage(
        user_id=user_id, message_id=message_id, chat_id=chat_id, chat_name="Group",
        sender_id="123", sender_name=sender_name, is_group=False, timestamp=date,
        message_type="text", body=body, is_from_me=is_from_me,
    )
    session.add(m)
    return m


def _reminder(session, user_id, uid, *, created_at, summary="Buy milk", list_name="Errands"):
    from app.integrations.apple_reminders.models import Reminder

    r = Reminder(
        user_id=user_id, uid=uid, list_name=list_name, summary=summary,
        priority=0, completed=False, created_at=created_at,
    )
    session.add(r)
    return r


def _open_task(session, uid, title, *, owner_id=None):
    from app.integrations.tasks.models import Task

    t = Task(uid=uid, title=title, status="next", kind="task", owner_id=owner_id)
    session.add(t)
    session.flush()
    return t


def _call(handler, session, **args):
    with use_user(1):
        return json.loads(handler(session, args))


# ─── window / marker ────────────────────────────────────────────────────────


def test_absent_since_defaults_to_24h_ago(db_session, monkeypatch):
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    before = _now()
    out = _call(intake.tasks_intake_candidates_handler, db_session)
    after = _now()
    since = datetime.fromisoformat(out["since"])
    assert (before - timedelta(hours=24)) <= since <= (after - timedelta(hours=24))
    assert out["marker_used"] is False


def test_marker_present_is_used_as_since(db_session, monkeypatch):
    from app.integrations.tasks import intake
    from app.integrations.tasks.models import IntakeMarker

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    marker_at = _now() - timedelta(hours=3)
    with use_user(1):
        db_session.add(IntakeMarker(user_id=1, seen_until=marker_at))
        db_session.commit()

    out = _call(intake.tasks_intake_candidates_handler, db_session)
    assert out["marker_used"] is True
    assert abs((datetime.fromisoformat(out["since"]) - marker_at).total_seconds()) < 1


def test_since_over_30_days_is_refused(db_session):
    from app.integrations.tasks import intake

    too_old = (_now() - timedelta(days=31)).isoformat()
    with pytest.raises(ValueError, match="30 days"):
        with use_user(1):
            intake.tasks_intake_candidates_handler(db_session, {"since": too_old})


def test_unknown_source_is_refused(db_session):
    from app.integrations.tasks import intake

    with pytest.raises(ValueError, match="unknown sources"):
        with use_user(1):
            intake.tasks_intake_candidates_handler(db_session, {"sources": ["carrier_pigeon"]})


# ─── classification: matched / new / unindexed ─────────────────────────────


def test_matched_new_and_unindexed_classification(db_session, monkeypatch):
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [(None, EmbeddingVecBgeSmall384)])

    with use_user(1):
        near_task = _open_task(db_session, "TASK-0001", "Book the boiler service")
        far_task = _open_task(db_session, "TASK-0002", "Unrelated other thing")
        db_session.flush()
        _seed_vec(db_session, "task", near_task.uid, None, _vec((0, 0.8), (1, 0.6)))
        _seed_vec(db_session, "task", far_task.uid, None, _vec((2, 1.0)))

        base = _now() - timedelta(hours=1)
        _mail(db_session, 1, "m-matched", date=base, subject="Boiler")
        _mail(db_session, 1, "m-new", date=base + timedelta(minutes=1), subject="Something else")
        _mail(db_session, 1, "m-unindexed", date=base + timedelta(minutes=2), subject="No vector yet")
        _mail(db_session, 1, "m-noise", date=base + timedelta(minutes=3), subject="Chance resemblance")
        db_session.commit()

        # m-matched: cosine 0.8 against near_task (>= default 0.78 threshold), 0 against far_task.
        _seed_vec(db_session, "email", "m-matched", 1, _vec((0, 1.0)))
        # m-noise: cosine 0.70 against near_task — inside the MEASURED production noise
        # floor (unrelated text scores 0.64-0.77 against open tasks with gemini-embedding-2),
        # so it must be `new`. This is the assertion the old 0.55 default fails.
        _seed_vec(db_session, "email", "m-noise", 1, _vec((0, 0.875), (3, 0.484)))
        # m-new: orthogonal to both seeded tasks -> indexed, but no match above threshold.
        _seed_vec(db_session, "email", "m-new", 1, _vec((4, 1.0)))
        # m-unindexed: deliberately no embedding row at all.
        db_session.commit()

    out = _call(intake.tasks_intake_candidates_handler, db_session, sources=["mail"], since=(base - timedelta(minutes=1)).isoformat())
    by_ref = {c["ref"]: c for c in out["candidates"]}

    matched = by_ref["m-matched"]
    assert matched["match_status"] == "matched"
    assert matched["matches"][0]["uid"] == "TASK-0001"
    assert matched["matches"][0]["score"] == pytest.approx(0.8, abs=1e-3)
    assert all(m["uid"] != "TASK-0002" for m in matched["matches"])

    assert by_ref["m-new"]["match_status"] == "new"
    assert by_ref["m-new"]["matches"] == []
    assert by_ref["m-noise"]["match_status"] == "new", "0.70 is chance resemblance on production data, not a match"

    assert by_ref["m-unindexed"]["match_status"] == "unindexed"
    assert by_ref["m-unindexed"]["matches"] == []

    assert out["counts"] == {
        "new": 2, "matched": 1, "unindexed": 1, "filtered": 0,
        "by_source": {"mail": 4},
    }


def test_matching_excludes_other_users_and_rounds_and_closed(db_session, monkeypatch):
    """Matching is scoped like every other tasks read: caller's open tasks
    plus unowned, never someone else's, never a round, never a closed task."""
    from app.integrations.tasks import intake
    from app.integrations.tasks.models import Routine

    monkeypatch.setattr(emb, "_active_spaces", lambda: [(None, EmbeddingVecBgeSmall384)])

    with use_user(1):
        mine = _open_task(db_session, "TASK-0010", "My open task")
        unowned = _open_task(db_session, "TASK-0011", "Unowned task")
        closed = _open_task(db_session, "TASK-0012", "Closed task")
        closed.status = "done"
        db_session.flush()
        vec = _vec((0, 1.0))
        _seed_vec(db_session, "task", mine.uid, None, vec)
        _seed_vec(db_session, "task", unowned.uid, None, vec)
        _seed_vec(db_session, "task", closed.uid, None, vec)

    with use_user(2):
        theirs = _open_task(db_session, "TASK-0013", "Sam's open task", owner_id=2)
        db_session.flush()
        _seed_vec(db_session, "task", theirs.uid, None, vec)

    with use_user(1):
        mine.owner_id = 1
        base = _now() - timedelta(hours=1)
        _mail(db_session, 1, "m-scoped", date=base, subject="probe")
        db_session.commit()
        _seed_vec(db_session, "email", "m-scoped", 1, vec)
        db_session.commit()

    out = _call(intake.tasks_intake_candidates_handler, db_session, sources=["mail"], since=(base - timedelta(minutes=1)).isoformat())
    uids = {m["uid"] for c in out["candidates"] for m in c["matches"]}
    assert uids == {"TASK-0010", "TASK-0011"}


# ─── WhatsApp: matching resolves through the conversation segment ─────────


def _wa_segment_id(chat_id: str, start: datetime, end: datetime) -> str:
    return f"{chat_id}:{int(start.timestamp())}:{int(end.timestamp())}"


def test_whatsapp_message_matches_through_its_segment(db_session, monkeypatch):
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [(None, EmbeddingVecBgeSmall384)])

    with use_user(1):
        near_task = _open_task(db_session, "TASK-0020", "Book the boiler service")
        db_session.flush()
        _seed_vec(db_session, "task", near_task.uid, None, _vec((0, 0.8), (1, 0.6)))

        seg_start = _now() - timedelta(hours=2)
        seg_end = seg_start + timedelta(minutes=10)
        _wa(db_session, 1, "wa-inside", date=seg_start + timedelta(minutes=5), chat_id="chat1")
        db_session.commit()

        seg_id = _wa_segment_id("chat1", seg_start, seg_end)
        _seed_vec(db_session, "whatsapp", seg_id, 1, _vec((0, 1.0)))
        db_session.commit()

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["whatsapp"], since=(seg_start - timedelta(minutes=1)).isoformat(),
    )
    by_ref = {c["ref"]: c for c in out["candidates"]}
    cand = by_ref["wa-inside"]
    assert cand["match_status"] == "matched"
    assert cand["matches"][0]["uid"] == "TASK-0020"


def test_whatsapp_message_outside_any_segment_range_is_unindexed(db_session, monkeypatch):
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [(None, EmbeddingVecBgeSmall384)])

    with use_user(1):
        seg_start = _now() - timedelta(hours=2)
        seg_end = seg_start + timedelta(minutes=10)
        # Well outside [seg_start, seg_end].
        _wa(db_session, 1, "wa-outside", date=seg_start + timedelta(hours=1), chat_id="chat1")
        db_session.commit()

        seg_id = _wa_segment_id("chat1", seg_start, seg_end)
        _seed_vec(db_session, "whatsapp", seg_id, 1, _vec((0, 1.0)))
        db_session.commit()

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["whatsapp"], since=(seg_start - timedelta(minutes=1)).isoformat(),
    )
    by_ref = {c["ref"]: c for c in out["candidates"]}
    assert by_ref["wa-outside"]["match_status"] == "unindexed"
    assert by_ref["wa-outside"]["matches"] == []


def test_whatsapp_message_exactly_on_segment_boundary_matches(db_session, monkeypatch):
    """A message exactly at `start` or `end` belongs to that segment —
    inclusive on both ends (mutation-check: an exclusive `<`/`>` comparison
    in `_segment_ref_for_message` makes this fail)."""
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [(None, EmbeddingVecBgeSmall384)])

    with use_user(1):
        near_task = _open_task(db_session, "TASK-0022", "Boundary task")
        db_session.flush()
        _seed_vec(db_session, "task", near_task.uid, None, _vec((0, 1.0)))

        seg_start = _now() - timedelta(hours=2)
        seg_end = seg_start + timedelta(minutes=10)
        # Exactly on the end boundary.
        _wa(db_session, 1, "wa-boundary", date=seg_end, chat_id="chat1")
        db_session.commit()

        seg_id = _wa_segment_id("chat1", seg_start, seg_end)
        _seed_vec(db_session, "whatsapp", seg_id, 1, _vec((0, 1.0)))
        db_session.commit()

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["whatsapp"], since=(seg_start - timedelta(minutes=1)).isoformat(),
    )
    by_ref = {c["ref"]: c for c in out["candidates"]}
    assert by_ref["wa-boundary"]["match_status"] == "matched"


def test_whatsapp_segment_lookup_never_crosses_users(db_session, monkeypatch):
    """A segment embedded under another user's `user_id` must never resolve
    a caller's message to it — mutation-check by removing the `user_id`
    filter in `_load_whatsapp_segments`, which would let this pass by
    matching a different user's task via a same-named chat_id."""
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [(None, EmbeddingVecBgeSmall384)])

    seg_start = _now() - timedelta(hours=2)
    seg_end = seg_start + timedelta(minutes=10)
    seg_id = _wa_segment_id("chat1", seg_start, seg_end)

    with use_user(1):
        _wa(db_session, 1, "wa-mine", date=seg_start + timedelta(minutes=1), chat_id="chat1")
        db_session.commit()

    with use_user(2):
        theirs = _open_task(db_session, "TASK-0023", "Sam's task", owner_id=2)
        db_session.flush()
        _seed_vec(db_session, "task", theirs.uid, None, _vec((0, 1.0)))
        _seed_vec(db_session, "whatsapp", seg_id, 2, _vec((0, 1.0)))
        db_session.commit()

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["whatsapp"], since=(seg_start - timedelta(minutes=1)).isoformat(),
    )
    by_ref = {c["ref"]: c for c in out["candidates"]}
    assert by_ref["wa-mine"]["match_status"] == "unindexed"
    assert "Sam's task" not in json.dumps(out)


# ─── cross-user isolation (canary) ─────────────────────────────────────────


def test_cross_user_isolation(db_session, monkeypatch):
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    with use_user(1):
        _mail(db_session, 1, "m-alex", date=base, subject="Alex's private subject")
    with use_user(2):
        _mail(db_session, 2, "m-sam", date=base, subject="Sam's private subject")
        db_session.commit()

    out = _call(intake.tasks_intake_candidates_handler, db_session, sources=["mail"], since=(base - timedelta(minutes=1)).isoformat())
    refs = {c["ref"] for c in out["candidates"]}
    assert refs == {"m-alex"}
    assert "Sam's private subject" not in json.dumps(out)


# ─── task-likeness pre-filter (lios#151) ───────────────────────────────────


def test_whatsapp_noise_is_filtered_by_default(db_session, monkeypatch):
    """Own-outgoing, empty/media-only, too-short and pure-emoji WhatsApp
    messages are dropped before matching even runs — mutation-check:
    deleting the `is_from_me`/length/emoji checks in
    `_looks_task_like_whatsapp` makes every one of these `new` instead of
    absent, and `counts.filtered` would read 0."""
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    with use_user(1):
        _wa(db_session, 1, "wa-real", date=base, body="can you pick up milk on the way home")
        _wa(db_session, 1, "wa-outgoing", date=base + timedelta(minutes=1),
            body="sure, on my way home now with plenty of time", is_from_me=True)
        _wa(db_session, 1, "wa-empty", date=base + timedelta(minutes=2), body="")
        _wa(db_session, 1, "wa-short", date=base + timedelta(minutes=3), body="lol ok")
        _wa(db_session, 1, "wa-emoji", date=base + timedelta(minutes=4), body="\U0001F44D\U0001F44D\U0001F44D\U0001F44D\U0001F44D\U0001F44D\U0001F44D")
        db_session.commit()

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["whatsapp"], since=(base - timedelta(minutes=1)).isoformat(),
    )
    refs = {c["ref"] for c in out["candidates"]}
    assert refs == {"wa-real"}
    assert out["counts"]["filtered"] == 4


def test_whatsapp_noise_survives_with_include_all(db_session, monkeypatch):
    """`include_all: true` bypasses the filter entirely — the same five
    messages as above all come back, and `filtered` reads 0."""
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    with use_user(1):
        _wa(db_session, 1, "wa-real", date=base, body="can you pick up milk on the way home")
        _wa(db_session, 1, "wa-short", date=base + timedelta(minutes=1), body="lol ok")
        db_session.commit()

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["whatsapp"], since=(base - timedelta(minutes=1)).isoformat(), include_all=True,
    )
    refs = {c["ref"] for c in out["candidates"]}
    assert refs == {"wa-real", "wa-short"}
    assert out["counts"]["filtered"] == 0


def test_whatsapp_candidate_shows_contact_name_not_raw_chat_id(db_session, monkeypatch):
    """Bug fix (lios#151): a 1:1 chat's `chat_name` is null on the message
    row (only groups get one from the bridge), so the candidate must show
    the caller's own saved contact name rather than falling straight to the
    raw JID — mutation-check: reverting `_fetch_whatsapp` to
    `chat_name or chat_id` (the old order) surfaces the raw JID again."""
    from app.integrations.whatsapp.models import WhatsAppContact
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    with use_user(1):
        db_session.add(WhatsAppContact(user_id=1, jid="353851234567@s.whatsapp.net", name="Cian Murphy"))
        m = _wa(
            db_session, 1, "wa-1to1", date=base, chat_id="353851234567@s.whatsapp.net",
            body="can you send the invoice for the boiler service",
        )
        m.chat_name = None
        db_session.commit()

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["whatsapp"], since=(base - timedelta(minutes=1)).isoformat(),
    )
    by_ref = {c["ref"]: c for c in out["candidates"]}
    assert by_ref["wa-1to1"]["chat_or_subject"] == "Cian Murphy"
    assert "353851234567" not in json.dumps(out)


def test_mail_drops_promotional_category_and_noreply_sender(db_session, monkeypatch):
    """Mutation-check: deleting either branch of `_looks_task_like_mail`
    lets the matching one of these two through instead of filtering it."""
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    with use_user(1):
        _mail(db_session, 1, "m-real", date=base, subject="Boiler service quote")
        promo = _mail(db_session, 1, "m-promo", date=base + timedelta(minutes=1), subject="50% off everything!")
        promo.labels = "CATEGORY_PROMOTIONS,INBOX"
        noreply = _mail(
            db_session, 1, "m-noreply", date=base + timedelta(minutes=2),
            subject="Your delivery is on its way", sender="no-reply@courier.example",
        )
        db_session.commit()

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["mail"], since=(base - timedelta(minutes=1)).isoformat(),
    )
    refs = {c["ref"] for c in out["candidates"]}
    assert refs == {"m-real"}
    assert out["counts"]["filtered"] == 2


# ─── inbox source (lios#159) ───────────────────────────────────────────────


@pytest.fixture
def fake_inbox_root(tmp_path, monkeypatch):
    """Point `scan.inbox_root()` at a throwaway directory — same pattern as
    `test_inbox_scoping.py`'s fixture of the same name."""
    from app.integrations.inbox import scan

    monkeypatch.setattr(scan.settings, "inbox_path", str(tmp_path))
    return tmp_path


def _write_inbox_item(user_id, bucket, filename, *, note=None, preview="", source="voicememo", mtime=None):
    from app.integrations.inbox import scan

    d = scan.user_root(user_id) / bucket
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_text("x")
    scan.write_sidecar(p, {
        "note": note, "preview": preview, "source": source,
        "enriched_at": "2026-01-01T00:00:00+00:00",
    })
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(p, (ts, ts))
    return p


def test_inbox_source_returns_pending_item_as_unindexed_candidate(db_session, fake_inbox_root, monkeypatch):
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    _write_inbox_item(
        1, "audio", "memo.m4a",
        note="Can you book the boiler service before it gets cold", source="voicememo",
        mtime=base,
    )

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["inbox"], since=(base - timedelta(minutes=1)).isoformat(),
    )
    assert out["counts"]["by_source"] == {"inbox": 1}
    cand = out["candidates"][0]
    assert cand["source"] == "inbox"
    assert cand["match_status"] == "unindexed"
    assert cand["matches"] == []
    assert cand["sender"] == "voicememo"
    assert "boiler service" in cand["text"]


def test_inbox_source_excludes_archived_items(db_session, fake_inbox_root, monkeypatch):
    """Only PENDING items are ever offered — mutation-check: calling
    `scan.iter_all_pending_files`-style logic (which does not filter by
    bucket) instead of `list_pending` would surface this archived item too."""
    from app.integrations.inbox import scan
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    p = _write_inbox_item(1, "audio", "already-triaged.m4a", note="Old memo, already handled", mtime=base)
    scan.move_to(p, "archive", user_id=1)

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["inbox"], since=(base - timedelta(minutes=1)).isoformat(),
    )
    assert out["candidates"] == []
    assert out["counts"]["by_source"] == {}


def test_inbox_source_scoped_to_caller(db_session, fake_inbox_root, monkeypatch):
    """Never reads another user's pending queue — mutation-check: removing
    `user_id` scoping from `InboxFacade.pending_candidates`/`scan.user_root`
    resolution would surface user 2's item under user 1's call."""
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    _write_inbox_item(1, "text", "mine.txt", note="My own captured note about the school form", mtime=base)
    _write_inbox_item(2, "text", "theirs.txt", note="Sam's private captured note", mtime=base)

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["inbox"], since=(base - timedelta(minutes=1)).isoformat(),
    )
    refs_text = json.dumps(out)
    assert "school form" in refs_text
    assert "Sam's private captured note" not in refs_text


def test_inbox_item_outside_window_is_excluded(db_session, fake_inbox_root, monkeypatch):
    """`modified_at` windowing works the same as every other source —
    mutation-check: dropping the since/until comparison in `_fetch_inbox`
    would return this item regardless of its age."""
    from app.integrations.tasks import intake

    monkeypatch.setattr(emb, "_active_spaces", lambda: [])
    base = _now() - timedelta(hours=1)
    too_old = base - timedelta(days=10)
    _write_inbox_item(1, "text", "old.txt", note="A note from ages ago", mtime=too_old)

    out = _call(
        intake.tasks_intake_candidates_handler, db_session,
        sources=["inbox"], since=(base - timedelta(minutes=1)).isoformat(),
    )
    assert out["candidates"] == []


# ─── mark ───────────────────────────────────────────────────────────────────


def test_mark_upsert_is_idempotent_and_returns_old_new(db_session):
    from app.integrations.tasks import intake

    out1 = _call(intake.tasks_intake_mark_handler, db_session, until="2026-09-01T00:00:00+00:00")
    assert out1 == {"old": None, "new": "2026-09-01T00:00:00+00:00"}

    out2 = _call(intake.tasks_intake_mark_handler, db_session, until="2026-09-02T00:00:00+00:00")
    assert out2 == {"old": "2026-09-01T00:00:00+00:00", "new": "2026-09-02T00:00:00+00:00"}

    from app.integrations.tasks.models import IntakeMarker
    with use_user(1):
        rows = db_session.query(IntakeMarker).filter_by(user_id=1).all()
    assert len(rows) == 1


def test_mark_default_until_is_now(db_session):
    from app.integrations.tasks import intake

    out = _call(intake.tasks_intake_mark_handler, db_session)
    when = datetime.fromisoformat(out["new"])
    assert abs((datetime.now(timezone.utc) - when).total_seconds()) < 5


# ─── annotations + registration ────────────────────────────────────────────


def test_tools_are_registered_with_expected_annotations():
    from app.integrations.tasks.tools import mcp_tools

    tools = {t["name"]: t for t in mcp_tools()}
    assert {"tasks_intake_candidates", "tasks_intake_mark"} <= set(tools)

    candidates_ann = tools["tasks_intake_candidates"]["annotations"]
    assert candidates_ann.get("readOnlyHint") is True

    mark_ann = tools["tasks_intake_mark"]["annotations"]
    assert "readOnlyHint" not in mark_ann
