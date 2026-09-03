"""Tests for Last.fm multi-user fan-out.

Last.fm went from a single `lastfm_username` (with `user_id=1` hardcoded in
the upsert) to one account per entry in the `lastfm_usernames` config map,
fanned out by `SourceIntegration.sync()`. The interesting cases are all about
attribution: that each account's scrobbles land under the right `user_id`,
that the "since" watermark is per-user (a global MAX would stop a newly-added
listener from ever backfilling), and that a bad config entry degrades to
skipping one person rather than failing everyone.

db tier throughout — `resolve_accounts` resolves comar user *names* against
real `users` rows, so mocking the session would test nothing.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.integrations.lastfm import (
    LastfmAccount,
    LastfmIntegration,
    resolve_accounts,
)
from app.integrations.lastfm.models import Scrobble
from app.integrations.lastfm.sync import pull_recent_scrobbles, store_scrobbles
from app.plugin import config_store


def _track(name: str, played_at: datetime) -> dict:
    return {
        "track_name": name,
        "artist_name": "Artist",
        "album_name": "Album",
        "album_art_url": None,
        "played_at_uts": int(played_at.timestamp()),
        "mbid": None,
        "loved": False,
    }


@pytest.mark.db
class TestResolveAccounts:
    def test_map_resolves_each_user(self, real_db, db_session):
        config_store.set_config_value(
            "lastfm", "lastfm_usernames", {"alex": "alex-fm", "sam": "sam-fm"}
        )
        accounts = {a.user_name: a for a in resolve_accounts(db_session)}

        assert set(accounts) == {"alex", "sam"}
        assert accounts["alex"].user_id == 1
        assert accounts["alex"].username == "alex-fm"
        assert accounts["sam"].user_id == 2
        assert accounts["sam"].username == "sam-fm"

    def test_unknown_user_is_skipped_not_fatal(self, real_db, db_session):
        """One bad entry must not stop the other person syncing."""
        config_store.set_config_value(
            "lastfm", "lastfm_usernames", {"sam": "sam-fm", "nobody": "ghost-fm"}
        )
        accounts = resolve_accounts(db_session)

        assert [a.user_name for a in accounts] == ["sam"]

    def test_blank_username_is_skipped(self, real_db, db_session):
        config_store.set_config_value(
            "lastfm", "lastfm_usernames", {"alex": "alex-fm", "sam": "   "}
        )
        assert [a.user_name for a in resolve_accounts(db_session)] == ["alex"]

    def test_legacy_single_username_attributed_to_lowest_id_user(
        self, real_db, db_session
    ):
        """A pre-upgrade deployment keeps syncing with no config edit."""
        config_store.set_config_value("lastfm", "lastfm_username", "legacy-fm")
        accounts = resolve_accounts(db_session)

        assert len(accounts) == 1
        assert accounts[0].user_id == 1
        assert accounts[0].username == "legacy-fm"

    def test_map_takes_precedence_over_legacy_key(self, real_db, db_session):
        config_store.set_config_value("lastfm", "lastfm_username", "legacy-fm")
        config_store.set_config_value(
            "lastfm", "lastfm_usernames", {"sam": "sam-fm"}
        )
        accounts = resolve_accounts(db_session)

        assert [a.username for a in accounts] == ["sam-fm"]

    def test_no_config_means_no_accounts(self, real_db, db_session):
        assert resolve_accounts(db_session) == []


@pytest.mark.db
class TestIsConfigured:
    def test_needs_api_key(self, real_db, db_session):
        config_store.set_config_value(
            "lastfm", "lastfm_usernames", {"alex": "alex-fm"}
        )
        assert LastfmIntegration().is_configured() is False

    def test_api_key_plus_map(self, real_db, db_session):
        config_store.set_config_value("lastfm", "lastfm_api_key", "k")
        config_store.set_config_value(
            "lastfm", "lastfm_usernames", {"alex": "alex-fm"}
        )
        assert LastfmIntegration().is_configured() is True

    def test_api_key_plus_legacy_username(self, real_db, db_session):
        """The deprecated form still counts as configured."""
        config_store.set_config_value("lastfm", "lastfm_api_key", "k")
        config_store.set_config_value("lastfm", "lastfm_username", "legacy-fm")
        assert LastfmIntegration().is_configured() is True

    def test_api_key_alone_is_not_enough(self, real_db, db_session):
        config_store.set_config_value("lastfm", "lastfm_api_key", "k")
        assert LastfmIntegration().is_configured() is False


@pytest.mark.db
class TestStoreAttribution:
    def test_records_land_under_their_own_user(self, real_db, db_session):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        alex = _track("Alex Song", now - timedelta(minutes=5))
        alex["user_id"] = 1
        sam = _track("Sam Song", now - timedelta(minutes=4))
        sam["user_id"] = 2

        assert store_scrobbles(db_session, [alex, sam]) == 2

        rows = {s.track_name: s.user_id for s in db_session.query(Scrobble).all()}
        assert rows == {"Alex Song": 1, "Sam Song": 2}

    def test_same_track_same_instant_for_two_users_is_not_deduped(
        self, real_db, db_session
    ):
        """Dedup is per-user: two people can legitimately play the same track."""
        played = datetime.now(timezone.utc).replace(microsecond=0)
        a = _track("Shared Song", played)
        a["user_id"] = 1
        b = _track("Shared Song", played)
        b["user_id"] = 2

        assert store_scrobbles(db_session, [a, b]) == 2
        assert db_session.query(Scrobble).count() == 2

    def test_reinserting_same_user_track_is_deduped(self, real_db, db_session):
        played = datetime.now(timezone.utc).replace(microsecond=0)
        rec = _track("Once Only", played)
        rec["user_id"] = 2

        assert store_scrobbles(db_session, [rec]) == 1
        assert store_scrobbles(db_session, [dict(rec)]) == 0
        assert db_session.query(Scrobble).count() == 1


@pytest.mark.db
class TestPullWatermarkIsPerUser:
    def test_second_user_is_not_capped_by_first_users_latest_play(
        self, real_db, db_session
    ):
        """The regression this whole change turns on.

        A global MAX(played_at) would hand user 2 user 1's watermark, so a
        newly-added listener would request only scrobbles newer than the
        established user's last play — and silently never backfill.
        """
        recent = datetime.now(timezone.utc).replace(microsecond=0)
        alex = _track("Alex Recent", recent)
        alex["user_id"] = 1
        store_scrobbles(db_session, [alex])

        captured = {}

        def _fake_fetch(*, api_key, username, from_timestamp, page=1):
            captured["from_timestamp"] = from_timestamp
            return {"tracks": [], "pagination": {"total_pages": 1}}

        with patch(
            "app.integrations.lastfm.sync.fetch_recent_tracks", _fake_fetch
        ):
            pull_recent_scrobbles(
                db_session, None, username="sam-fm", user_id=2
            )

        # Sam has no scrobbles yet, so there is no watermark for her at all.
        assert captured["from_timestamp"] is None

    def test_own_watermark_is_used(self, real_db, db_session):
        played = datetime.now(timezone.utc).replace(microsecond=0)
        rec = _track("Sam Song", played)
        rec["user_id"] = 2
        store_scrobbles(db_session, [rec])

        captured = {}

        def _fake_fetch(*, api_key, username, from_timestamp, page=1):
            captured["from_timestamp"] = from_timestamp
            return {"tracks": [], "pagination": {"total_pages": 1}}

        with patch(
            "app.integrations.lastfm.sync.fetch_recent_tracks", _fake_fetch
        ):
            pull_recent_scrobbles(
                db_session, None, username="sam-fm", user_id=2
            )

        assert captured["from_timestamp"] == int(played.timestamp()) + 1

    def test_pull_stamps_user_id_onto_records(self, real_db, db_session):
        played = datetime.now(timezone.utc).replace(microsecond=0)

        def _fake_fetch(*, api_key, username, from_timestamp, page=1):
            return {
                "tracks": [_track("Fresh", played)],
                "pagination": {"total_pages": 1},
            }

        with patch(
            "app.integrations.lastfm.sync.fetch_recent_tracks", _fake_fetch
        ):
            result = pull_recent_scrobbles(
                db_session, None, username="sam-fm", user_id=2
            )

        assert [r["user_id"] for r in result.records] == [2]


@pytest.mark.db
class TestIntegrationFanOut:
    def test_accounts_exposes_user_id_and_label(self, real_db, db_session):
        config_store.set_config_value(
            "lastfm", "lastfm_usernames", {"sam": "sam-fm"}
        )
        integration = LastfmIntegration()
        accounts = integration.accounts(db_session)

        assert len(accounts) == 1
        account = accounts[0]
        assert isinstance(account, LastfmAccount)
        assert integration.account_user_id(account) == 2
        assert "sam-fm" in integration.account_label(account)
