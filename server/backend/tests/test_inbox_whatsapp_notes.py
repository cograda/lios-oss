"""Routing WhatsApp message-to-self notes into the inbox triage queue.

A typed note used to sit in `whatsapp_messages` forever with nothing surfacing
it, while a voice note captured seconds earlier got a summary and a triage
decision. This makes both capture channels land in the same queue.

The tests that matter most here are the idempotency ones. This runs on a
ten-minute cron, so a routing pass that fails to recognise its own previous work
doesn't produce one duplicate — it produces 144 a day, and the ones that matter
are the notes the user has already dealt with and archived.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.inbox import scan, whatsapp_notes

SELF_JID = "50822398382303@lid"
BASE = datetime(2026, 8, 17, 11, 22, 57, tzinfo=timezone.utc)


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    monkeypatch.setattr(scan.settings, "inbox_path", str(root))
    (scan.user_root(1) / "text").mkdir(parents=True)
    return scan.user_root(1)


@pytest.fixture
def notes(monkeypatch):
    """Stub the `whatsapp.query` capability and the InboxItem ledger write.

    The ledger is a DB row and this is a unit-tier test; `record_item` is
    exercised against a real Postgres in the db-tier suites. Everything this
    module actually guarantees — dedup, naming, sidecar shape — is filesystem
    state, so stubbing the one DB call keeps these tests fast without weakening
    what they check.
    """
    import app.plugin.capabilities as capabilities

    state: list[dict] = []
    recorded: list[tuple] = []

    class _WhatsApp:
        def self_notes(self, session, *, limit=100, user_id=None, since=None):
            # Mirrors the real facade's contract after the 2026-08-17 fix: only
            # messages the owning user SENT, in a chat configured for that user.
            # The stub enforces `is_from_me` so a routing bug can't hide behind a
            # permissive fake.
            rows = [n for n in state if n.get("is_from_me", True)]
            if user_id is not None:
                rows = [n for n in rows if n["user_id"] == user_id]
            if since is not None:
                rows = [n for n in rows if n["timestamp"] >= since]
            return rows[:limit]

    monkeypatch.setattr(
        capabilities, "get_capability",
        lambda name: _WhatsApp() if name == "whatsapp.query" else (_ for _ in ()).throw(KeyError(name)),
    )
    # No age limit by default in these tests: the fixture data is deliberately
    # dated, and the cutoff has its own dedicated tests below.
    import app.plugin.config_store as config_store

    class _Cfg:
        inbox_whatsapp_note_max_age_days = 0

    monkeypatch.setattr(config_store, "plugin_config", lambda name: _Cfg())
    monkeypatch.setattr(
        scan, "record_item",
        lambda uid, rel, **kw: recorded.append((uid, rel, kw.get("sha256"))),
    )

    def setter(*rows: dict):
        state.clear()
        state.extend(rows)
        return state

    setter.recorded = recorded
    return setter


def _note(body: str, *, when: datetime = BASE, mid: str = "3A69A24B",
          user_id: int = 1, is_from_me: bool = True) -> dict:
    return {
        "message_id": mid,
        "user_id": user_id,
        "chat_id": SELF_JID,
        "body": body,
        "timestamp": when,
        "message_type": "text",
        "is_from_me": is_from_me,
    }


class TestRouting:
    def test_a_note_becomes_a_pending_inbox_item(self, inbox, notes):
        notes(_note("Craft project in Claude — look at the vault structure"))

        counts = whatsapp_notes.route_self_notes(session=None)

        assert counts["routed"] == 1
        items = scan.list_pending(1)
        assert len(items) == 1
        item = items[0]
        assert item["kind"] == "markdown"
        assert item["source"] == "whatsapp-self-chat"
        # `note` is what `summarise()` leads with and what `inbox_pending` shows.
        assert "Craft project in Claude" in item["note"]
        assert "Craft project in Claude" in item["summary"]

    def test_provenance_is_written_into_the_file_itself(self, inbox, notes):
        """The file is the thing that survives being routed onward by
        `inbox_to_vault`, so the message id has to live in it, not only in a
        sidecar that gets left behind."""
        notes(_note("Order the second SCD41 sensor", mid="MSG-1234"))
        whatsapp_notes.route_self_notes(session=None)

        path = scan.iter_pending_files(1)[0]
        content = path.read_text()
        assert content.startswith("---\n")
        assert "source: whatsapp-self-chat" in content
        assert "message_id: MSG-1234" in content
        assert "Order the second SCD41 sensor" in content

        meta = scan.read_sidecar(path)
        assert meta["extra"]["message_id"] == "MSG-1234"
        assert meta["sha256"]

    def test_filename_is_stamped_from_the_message_not_the_sweep(self, inbox, notes):
        """The inbox sorts by filename, so a backlog drained in one sweep must
        still read in capture order rather than all landing at the cron's time."""
        notes(
            _note("first note about the gate sensor", when=BASE, mid="A"),
            _note("second note about the heat pump", when=BASE + timedelta(days=2), mid="B"),
        )
        whatsapp_notes.route_self_notes(session=None)

        names = [p.name for p in scan.iter_pending_files(1)]
        assert names[0].startswith("20260817-")
        assert names[1].startswith("20260819-")

    def test_trivial_notes_are_not_queued(self, inbox, notes):
        """Otherwise 'ok' and a thumbs-up become triage items to dismiss by hand."""
        notes(_note("ok", mid="A"), _note("👍", mid="B"))

        counts = whatsapp_notes.route_self_notes(session=None)
        assert counts["too_short"] == 2
        assert counts["routed"] == 0
        assert scan.iter_pending_files(1) == []

    def test_unconfigured_is_a_silent_no_op(self, inbox, notes):
        notes()  # no self-chat configured → facade returns nothing
        counts = whatsapp_notes.route_self_notes(session=None)
        assert counts == {"considered": 0, "routed": 0, "skipped": 0,
                          "too_short": 0, "failed": 0}

    def test_a_received_message_is_never_routed(self, inbox, notes):
        """⚠️ Regression test for a bug that reached production on 2026-08-17.

        A WhatsApp `@lid` is account-scoped, so the JID configured as Alex's
        self-chat matched 178 rows under Sam's bridge — messages she had
        RECEIVED from a third party. 59 were filed into her inbox as her own
        notes before it was caught. The facade now requires `is_from_me`, and
        nothing that fails it may become an inbox item.
        """
        notes(
            _note("my own note about the gate sensor", mid="MINE"),
            _note("hiya, are you around later for a chat?", mid="THEIRS", is_from_me=False),
        )

        counts = whatsapp_notes.route_self_notes(session=None)

        assert counts["routed"] == 1
        bodies = [p.read_text() for p in scan.iter_pending_files(1)]
        assert len(bodies) == 1
        assert "gate sensor" in bodies[0]
        assert "are you around" not in bodies[0]

    def test_each_note_is_written_to_its_own_owner(self, inbox, notes, tmp_path):
        """`self_notes` is deliberately not user-scoped — it's a cron with no
        request user — so every row's own `user_id` must be honoured."""
        (scan.user_root(2) / "text").mkdir(parents=True)
        notes(
            _note("Alex's note about the gate sensor", mid="C", user_id=1),
            _note("Sam's note about the school forms", mid="N", user_id=2),
        )

        whatsapp_notes.route_self_notes(session=None)

        alex = [p.read_text() for p in scan.iter_pending_files(1)]
        sam = [p.read_text() for p in scan.iter_pending_files(2)]
        assert len(alex) == 1 and len(sam) == 1
        assert "gate sensor" in alex[0]
        assert "school forms" in sam[0]
        assert "school forms" not in alex[0]

    def test_one_bad_note_does_not_stall_the_sweep(self, inbox, notes):
        """The message stays in `whatsapp_messages` regardless, so a failure is a
        retry next run — but it must not take the other notes down with it."""
        bad = _note("this note has a broken timestamp", mid="BAD")
        bad["timestamp"] = object()  # no .strftime, no .isoformat
        notes(bad, _note("this one is perfectly fine and should land", mid="OK"))

        counts = whatsapp_notes.route_self_notes(session=None)

        assert counts["failed"] == 1
        assert counts["routed"] == 1
        assert len(scan.iter_pending_files(1)) == 1


class TestIdempotency:
    """A ten-minute cron that can't recognise its own work makes 144 duplicates
    a day, not one."""

    def test_second_sweep_routes_nothing_new(self, inbox, notes):
        notes(_note("Craft project in Claude — look at the vault"))
        whatsapp_notes.route_self_notes(session=None)

        counts = whatsapp_notes.route_self_notes(session=None)

        assert counts["skipped"] == 1
        assert counts["routed"] == 0
        assert len(scan.iter_pending_files(1)) == 1

    def test_an_archived_note_does_not_come_back(self, inbox, notes):
        """The case that actually bites. The user triages a note and archives it;
        the next sweep must not resurrect it as new work.

        This is why dedup keys on `scan.find_by_hash`, which searches terminal
        buckets — a pending-only check would pass the test above and fail here.
        """
        notes(_note("Craft project in Claude — look at the vault"))
        whatsapp_notes.route_self_notes(session=None)

        path = scan.iter_pending_files(1)[0]
        scan.move_to(path, "archive", user_id=1)
        assert scan.iter_pending_files(1) == []

        counts = whatsapp_notes.route_self_notes(session=None)

        assert counts["skipped"] == 1
        assert scan.iter_pending_files(1) == [], "an archived note was re-queued"

    def test_a_dismissed_note_does_not_come_back(self, inbox, notes):
        notes(_note("some idea I decided against keeping"))
        whatsapp_notes.route_self_notes(session=None)
        scan.move_to(scan.iter_pending_files(1)[0], "dismissed", user_id=1)

        assert whatsapp_notes.route_self_notes(session=None)["skipped"] == 1
        assert scan.iter_pending_files(1) == []

    def test_identical_text_sent_twice_is_two_notes(self, inbox, notes):
        """Content hashing must not collapse genuinely separate captures. The
        message id and timestamp are inside the hashed content precisely so
        "call the plumber" typed twice a month apart stays two items."""
        notes(
            _note("call the plumber about the utility room", when=BASE, mid="A"),
            _note("call the plumber about the utility room",
                  when=BASE + timedelta(days=30), mid="B"),
        )

        counts = whatsapp_notes.route_self_notes(session=None)

        assert counts["routed"] == 2
        assert len(scan.iter_pending_files(1)) == 2

    def test_sidecar_carries_the_hash_dedup_reads(self, inbox, notes):
        """Regression guard on the coupling: `find_by_hash` reads
        `sidecar["sha256"]`. Drop that field and every note is re-created on
        every sweep, forever, with no error anywhere."""
        notes(_note("Craft project in Claude — look at the vault"))
        whatsapp_notes.route_self_notes(session=None)

        path = scan.iter_pending_files(1)[0]
        digest = json.loads(scan.sidecar_path(path).read_text())["sha256"]
        assert scan.find_by_hash(digest, 1) == path


class TestRecencyCutoff:
    """Enabling this against an existing chat must not backfill its history.

    The first live run routed four months of notes in one sweep — including a
    pasted password, a base64 key and two hashes, which became files in the queue.
    A triage queue is for things still worth acting on.

    Bounds *routing* only. `whatsapp/sync.py` has no equivalent cutoff on purpose:
    search should reach every note ever written, and an old note being findable
    costs nothing while an old note demanding triage costs attention.
    """

    @pytest.fixture
    def aged(self, monkeypatch):
        def _set(days: int):
            import app.plugin.config_store as config_store

            class _Cfg:
                inbox_whatsapp_note_max_age_days = days

            monkeypatch.setattr(config_store, "plugin_config", lambda name: _Cfg())

        return _set

    def test_old_notes_are_left_alone(self, inbox, notes, aged):
        aged(7)
        now = datetime.now(timezone.utc)
        notes(
            _note("a note from this morning worth acting on", when=now - timedelta(hours=3), mid="NEW"),
            _note("a note from four months ago, long since handled", when=now - timedelta(days=120), mid="OLD"),
        )

        counts = whatsapp_notes.route_self_notes(session=None)

        assert counts["routed"] == 1
        bodies = [p.read_text() for p in scan.iter_pending_files(1)]
        assert len(bodies) == 1
        assert "this morning" in bodies[0]

    def test_zero_means_no_limit(self, inbox, notes, aged):
        """The escape hatch, for a deliberate one-off backfill."""
        aged(0)
        now = datetime.now(timezone.utc)
        notes(_note("a note from four months ago", when=now - timedelta(days=120), mid="OLD"))

        assert whatsapp_notes.route_self_notes(session=None)["routed"] == 1

    def test_a_junk_config_value_does_not_break_the_sweep(self, inbox, notes, monkeypatch):
        """A hand-edited config value must degrade to 'no limit', never raise —
        the sweep runs on a cron with nobody watching it fail."""
        import app.plugin.config_store as config_store

        class _Cfg:
            inbox_whatsapp_note_max_age_days = "seven"

        monkeypatch.setattr(config_store, "plugin_config", lambda name: _Cfg())
        notes(_note("a perfectly ordinary note about the gate", mid="A"))

        assert whatsapp_notes.route_self_notes(session=None)["routed"] == 1
