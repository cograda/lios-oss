"""conversations_since (lios#192) — deterministic cross-source grouping (db tier).

Seeds real WhatsAppMessage/MailMessage rows and calls the real
`whatsapp.query`/`mail.query` facades through the actual MCP handler — no
mocking of the fetch path, same convention as `test_tasks_intake.py`. This
tool makes no embedding call and no LLM call, so unlike intake there is
nothing to fake at the vector layer either.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user

pytestmark = pytest.mark.db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wa(session, user_id, message_id, *, date, chat_id="chat1", chat_name=None,
        sender_name="Cian", sender_id="123", body="hello", is_from_me=False, is_group=False):
    from app.integrations.whatsapp.models import WhatsAppMessage

    m = WhatsAppMessage(
        user_id=user_id, message_id=message_id, chat_id=chat_id, chat_name=chat_name,
        sender_id=sender_id, sender_name=sender_name, is_group=is_group, timestamp=date,
        message_type="text", body=body, is_from_me=is_from_me,
    )
    session.add(m)
    return m


def _mail(session, user_id, google_message_id, *, date, thread_id="t1", subject="Subject",
          sender="a@b.com", snippet="hello"):
    from app.integrations.google_mail.models import MailMessage

    m = MailMessage(
        user_id=user_id, google_message_id=google_message_id, thread_id=thread_id,
        account_email="me@example.com", subject=subject, sender=sender, to="me@example.com",
        date=date, snippet=snippet, labels="", is_read=False, is_starred=False,
        has_attachments=False,
    )
    session.add(m)
    return m


def _own_token(session, user_id, email="me@example.com"):
    from app.models.tokens import OAuthToken

    t = OAuthToken(
        user_id=user_id, provider="google", account_email=email,
        access_token="x", refresh_token="y",
    )
    session.add(t)
    return t


def _call(session, since, **kw):
    from app.integrations.system.tools import handle_conversations_since

    args = {"since": since.isoformat(), **kw}
    with use_user(1):
        return json.loads(handle_conversations_since(session, args))


@pytest.fixture(autouse=True)
def fixed_burst_gap(monkeypatch):
    """Pin the burst gap to 60 minutes so tests don't depend on the config
    default (which is itself a design decision this suite shouldn't couple
    to)."""
    from app.integrations.system import conversations

    monkeypatch.setattr(conversations, "_burst_gap_minutes", lambda: 60)


@pytest.fixture(autouse=True)
def no_self_chat(monkeypatch):
    """Default: nobody has a configured self-chat jid — the exclusion test
    below overrides this explicitly."""
    from app.integrations.whatsapp import sync as wa_sync

    monkeypatch.setattr(wa_sync, "self_chat_map", lambda: {})


# ─── WhatsApp: burst grouping within a chat ────────────────────────────────


def test_whatsapp_burst_gap_splits_into_two_groups(db_session):
    with use_user(1):
        base = _now() - timedelta(hours=3)
        _wa(db_session, 1, "wa-1", date=base, body="are we still on for saturday")
        _wa(db_session, 1, "wa-2", date=base + timedelta(minutes=5), body="yes 10am works", is_from_me=True)
        # more than 60 minutes after wa-2 -> new burst
        _wa(db_session, 1, "wa-3", date=base + timedelta(minutes=90), body="actually can we push to 11")
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"])
    groups = out["groups"]
    assert len(groups) == 2
    assert {g["message_count"] for g in groups} == {2, 1}
    # newest burst (wa-3) sorts first
    assert groups[0]["last_message"]["text"] == "actually can we push to 11"


def test_whatsapp_gap_boundary_exact_gap_is_same_burst(db_session):
    with use_user(1):
        base = _now() - timedelta(hours=2)
        _wa(db_session, 1, "wa-a", date=base, body="ping")
        # Exactly 60 minutes later: `> gap` is false at equality, so this is
        # still the same burst.
        _wa(db_session, 1, "wa-b", date=base + timedelta(minutes=60), body="pong", is_from_me=True)
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"])
    assert len(out["groups"]) == 1
    assert out["groups"][0]["message_count"] == 2


def test_whatsapp_group_all_from_me_is_excluded(db_session):
    with use_user(1):
        base = _now() - timedelta(hours=1)
        _wa(db_session, 1, "wa-only-mine", date=base, body="note to self style outgoing", is_from_me=True)
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"])
    assert out["groups"] == []


def test_whatsapp_self_chat_jid_is_excluded(db_session, monkeypatch):
    from app.integrations.whatsapp import sync as wa_sync

    monkeypatch.setattr(wa_sync, "self_chat_map", lambda: {1: "self@lid"})

    with use_user(1):
        base = _now() - timedelta(hours=1)
        _wa(db_session, 1, "wa-note", date=base, chat_id="self@lid", body="buy milk", is_from_me=True)
        # A real conversation in a different chat should still show up.
        _wa(db_session, 1, "wa-real", date=base, chat_id="chat2", body="are you coming?")
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"])
    keys = {g["key"] for g in out["groups"]}
    assert "self@lid" not in keys
    assert "chat2" in keys


def test_whatsapp_has_question_flag(db_session):
    with use_user(1):
        base = _now() - timedelta(hours=1)
        _wa(db_session, 1, "wa-q1", date=base, chat_id="chatq", body="dinner tonight sounds good")
        _wa(db_session, 1, "wa-q2", date=base + timedelta(minutes=1), chat_id="chatq2",
            body="have we got a date for the AGM?")
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"])
    by_key = {g["key"]: g for g in out["groups"]}
    assert by_key["chatq"]["has_question"] is False
    assert by_key["chatq2"]["has_question"] is True


def test_whatsapp_messages_capped_at_20_oldest_first(db_session):
    with use_user(1):
        base = _now() - timedelta(hours=1)
        for i in range(25):
            _wa(db_session, 1, f"wa-cap-{i}", date=base + timedelta(minutes=i), chat_id="chatcap",
                body=f"message {i}", is_from_me=(i % 2 == 0))
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"])
    group = next(g for g in out["groups"] if g["key"] == "chatcap")
    assert group["message_count"] == 25
    assert len(group["messages"]) == 20
    # oldest-first within the capped window
    texts = [m["text"] for m in group["messages"]]
    assert texts == sorted(texts, key=lambda t: int(t.split()[-1]))


# ─── Gmail: thread grouping ─────────────────────────────────────────────────


def test_gmail_thread_grouping_is_by_thread_id(db_session):
    with use_user(1):
        _own_token(db_session, 1)
        base = _now() - timedelta(hours=2)
        _mail(db_session, 1, "m-1", date=base, thread_id="thread-a",
              subject="AGM date?", sender="Alex <alex@example.com>", snippet="have we got a date for the AGM?")
        _mail(db_session, 1, "m-2", date=base + timedelta(minutes=30), thread_id="thread-a",
              subject="Re: AGM date?", sender="Stef <stef@example.com>",
              snippet="third week of sept, will confirm")
        _mail(db_session, 1, "m-3", date=base, thread_id="thread-b",
              subject="Unrelated", sender="Other <other@example.com>", snippet="fyi")
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["mail"])
    groups = {g["key"]: g for g in out["groups"]}
    assert set(groups) == {"thread-a", "thread-b"}
    assert groups["thread-a"]["message_count"] == 2
    assert groups["thread-a"]["title"] == "Re: AGM date?"
    assert set(groups["thread-a"]["participants"]) == {"Alex", "Stef"}
    assert groups["thread-a"]["has_question"] is True
    assert groups["thread-b"]["has_question"] is False


def test_gmail_thread_all_from_me_is_excluded(db_session):
    with use_user(1):
        _own_token(db_session, 1, email="me@example.com")
        base = _now() - timedelta(hours=1)
        _mail(db_session, 1, "m-sent", date=base, thread_id="thread-sent",
              subject="Sent only", sender="Me <me@example.com>", snippet="just me")
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["mail"])
    assert out["groups"] == []


# ─── Scoping (mandatory) ────────────────────────────────────────────────────


def test_scoping_excludes_other_users_data(db_session):
    LEAK_CANARY = "LEAK-CANARY-U2-BODY"
    with use_user(2):
        base = _now() - timedelta(hours=1)
        _wa(db_session, 2, "wa-u2", date=base, chat_id="chat-u2", body=LEAK_CANARY)
        _own_token(db_session, 2, email="sam@example.com")
        _mail(db_session, 2, "m-u2", date=base, thread_id="thread-u2",
              subject="U2 mail", sender="Someone <someone@example.com>", snippet=LEAK_CANARY)
        db_session.commit()

    with use_user(1):
        base1 = _now() - timedelta(hours=1)
        _own_token(db_session, 1)
        _wa(db_session, 1, "wa-u1", date=base1, chat_id="chat-u1", body="my own message")
        db_session.commit()

    # Called as user 1 (see `_call`) — none of user 2's rows may appear.
    out = _call(db_session, base1 - timedelta(minutes=1))
    dumped = json.dumps(out)
    assert LEAK_CANARY not in dumped
    keys = {g["key"] for g in out["groups"]}
    assert "chat-u2" not in keys
    assert "thread-u2" not in keys
    assert "chat-u1" in keys


# ─── Ordering, sources filter, limit ────────────────────────────────────────


def test_groups_sorted_by_last_message_desc_and_limit_applies(db_session):
    with use_user(1):
        base = _now() - timedelta(hours=5)
        _wa(db_session, 1, "wa-early", date=base, chat_id="chat-early", body="earliest one")
        _wa(db_session, 1, "wa-mid", date=base + timedelta(hours=1), chat_id="chat-mid", body="middle one")
        _wa(db_session, 1, "wa-late", date=base + timedelta(hours=2), chat_id="chat-late", body="latest one")
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"])
    keys_in_order = [g["key"] for g in out["groups"]]
    assert keys_in_order == ["chat-late", "chat-mid", "chat-early"]

    limited = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"], limit=2)
    assert len(limited["groups"]) == 2
    assert limited["counts"]["total_before_limit"] == 3


def test_sources_param_filters_to_requested_source(db_session):
    with use_user(1):
        base = _now() - timedelta(hours=1)
        _own_token(db_session, 1)
        _wa(db_session, 1, "wa-only", date=base, chat_id="chat-only", body="whatsapp only")
        _mail(db_session, 1, "m-only", date=base, thread_id="thread-only",
              subject="mail only", sender="a@b.com", snippet="mail only")
        db_session.commit()

    out = _call(db_session, base - timedelta(minutes=1), sources=["whatsapp"])
    assert all(g["source"] == "whatsapp" for g in out["groups"])
    assert out["counts"]["mail"] == 0


# ─── since validation ───────────────────────────────────────────────────────


def test_since_required(db_session):
    from app.integrations.system.tools import handle_conversations_since

    with pytest.raises(ValueError, match="'since' is required"):
        with use_user(1):
            handle_conversations_since(db_session, {})


def test_since_over_30_days_is_refused(db_session):
    from app.integrations.system.tools import handle_conversations_since

    too_old = (_now() - timedelta(days=31)).isoformat()
    with pytest.raises(ValueError, match="30 days"):
        with use_user(1):
            handle_conversations_since(db_session, {"since": too_old})
