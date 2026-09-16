"""Aggregates must be scoped too, not just rows.

`test_user_scoping.py`'s leak canary seeds string canaries for user 2 and
asserts user 1's tool output never contains them. That catches leaked
*content*, but a count leaks nothing textual: `total_messages: 128294`
contains no canary, so an unscoped aggregate passes the canary while telling
one user exactly how much data the other has.

That is not hypothetical — `whatsapp_stats` shipped unscoped and, the moment a
second bridge existed, reported one user's 128k messages and 2020-onwards
history span to the other. Volume, group count and earliest-message date
describe a person's life even with no message body attached.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.auth.context import use_user

pytestmark = pytest.mark.db


def _msg(user_id: int, n: int, when: datetime):
    from app.integrations.whatsapp.models import WhatsAppMessage

    return WhatsAppMessage(
        user_id=user_id, message_id=f"U{user_id}-M{n}", chat_id=f"chat{user_id}",
        chat_name=f"Chat {user_id}", sender_id=f"s{user_id}", sender_name="S",
        is_group=False, timestamp=when, message_type="text",
        body=f"message {n}", media_caption=None, is_from_me=False,
        reply_to_id=None, raw_json="{}",
    )


def _contact(user_id: int, n: int, is_group: bool = False):
    from app.integrations.whatsapp.models import WhatsAppContact

    return WhatsAppContact(
        user_id=user_id, jid=f"u{user_id}-c{n}@s.whatsapp.net",
        name=f"Contact {n}", notify_name=f"Contact {n}", is_group=is_group,
        last_message_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def _two_users_of_whatsapp(db_session):
    old = datetime(2020, 4, 18, tzinfo=timezone.utc)
    new = datetime(2026, 8, 7, tzinfo=timezone.utc)

    db_session.add_all([
        # User 1: lots of history, reaching back years.
        *[_msg(1, i, old + timedelta(days=i)) for i in range(5)],
        *[_contact(1, i) for i in range(4)],
        _contact(1, 90, is_group=True),
        _contact(1, 91, is_group=True),
        # User 2: brand new, one message.
        _msg(2, 0, new),
        _contact(2, 0),
    ])
    db_session.commit()


def _stats(user_id: int) -> dict:
    import json

    from app.integrations.whatsapp.tools import _whatsapp_stats_compute
    from app.db import get_db

    db = get_db()
    with db.session() as session, use_user(user_id):
        result = _whatsapp_stats_compute(session, {})
    # round-trips the same way the tool serialises it
    return json.loads(json.dumps(result, default=str))


def test_stats_counts_are_per_user(real_db, _two_users_of_whatsapp):
    assert _stats(1)["total_messages"] == 5
    assert _stats(2)["total_messages"] == 1


def test_stats_contact_and_group_counts_are_per_user(real_db, _two_users_of_whatsapp):
    one, two = _stats(1), _stats(2)
    assert one["total_contacts"] == 6 and one["total_groups"] == 2
    assert two["total_contacts"] == 1 and two["total_groups"] == 0


def test_history_span_does_not_leak_the_other_users_earliest_message(
    real_db, _two_users_of_whatsapp
):
    """The most quietly revealing field: an `earliest` of 2020 tells a user who
    joined last week exactly how far back the other person's archive goes."""
    two = _stats(2)
    assert two["earliest"].startswith("2026-08-07")
    assert not two["earliest"].startswith("2020")
