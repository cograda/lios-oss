"""Golden-output snapshot tests for read-only tool handlers (db tier).

Pins current serialization behaviour ahead of a shared-helpers refactor of
~7,000 LOC across 18 integrations' tools.py files. For every integration in
scope, we seed deterministic fixture rows directly via the ORM (fixed ids /
timestamps, both users where the model is user-owned), invoke each read-only
tool handler through the exact dispatch shape the MCP layer uses —
`tool_def["handler"](session, arguments)` — and diff the normalized JSON
output against a snapshot file committed under tests/snapshots/.

In scope: lastfm, obsidian, whatsapp, google_mail, coffee, finance,
apple_health, media, attachments, homeassistant, snags.

Skipped (see module docstrings / notes below for why):
  - Live-API tools (gmail_unread, gmail_thread, weather_*, rail_*): openWorldHint,
    reach an external service — not this suite's concern.
  - Write/destructive tools: out of scope by design (golden-output is for reads).
  - lastfm_backfill/enrich, gmail_backfill/embed, whatsapp_embed, coffee_embed/log/
    brew/delete_brew/rate, finance_import_csv/register_fingerprint/add_rule,
    media_sync/fetch/export, attachments_scan/ingest, snag_capture/add/update/render,
    vault_transfer: writes or admin ops, not read-only.

Nondeterminism handling:
  - All timestamps seeded at a fixed anchor (2026-01-15T10:00:00Z) or explicit
    absolute dates — never rely on server_default=func.now().
  - apple_health (health_workouts/trends/summary/exercise_status) and
    homeassistant (ha_history's `since` bound) call datetime.now()/date.today()
    directly with no override param — monkeypatched to the fixed anchor via the
    `frozen_health_clock` / `frozen_ha_clock` fixtures.
  - finance tools use absolute "YYYY-MM" period strings (never "this_month" etc)
    so resolve_period()'s date.today() call doesn't leak into query bounds.
  - coffee_recommend's "stale_favourite_roasters" (>60 days since last purchase)
    uses a seeded created_at far enough in the past (2020-01-01) that it stays
    stale forever relative to any real test-run date — no patch needed.
  - Semantic-search tools (vault_search, whatsapp_semantic_search,
    gmail_semantic_search, coffee_similar) stub app.services.embedding.get_model
    so the query embedding is deterministic, and seed exactly one matching
    embedding row per test (identical vector → score always 1.0) rather than
    engineering a multi-row ranking, which would be far more fragile.

Regenerate all snapshots after an intentional behaviour change:
    UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest tests/test_tool_snapshots.py -m db
"""

from __future__ import annotations

import difflib
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.auth.context import use_user

pytestmark = pytest.mark.db

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

FIXED_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
FIXED_TODAY = date(2026, 1, 15)  # a Thursday

VECTOR_DIM = 384


def _one_hot(index: int) -> list[float]:
    v = [0.0] * VECTOR_DIM
    v[index] = 1.0
    return v


FIXED_QUERY_VEC = _one_hot(0)


# ---------------------------------------------------------------------------
# Snapshot comparison helpers
# ---------------------------------------------------------------------------

def _normalize(obj):
    """Round floats and sort dict keys recursively for stable comparison."""
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _normalize(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def assert_snapshot(name: str, output) -> None:
    """Compare a tool's output against tests/snapshots/<name>.json.

    `output` may be the raw JSON string a handler returns, or an
    already-parsed object. Writes (or rewrites) the snapshot file when
    UPDATE_SNAPSHOTS=1 or when the file doesn't exist yet.
    """
    parsed = json.loads(output) if isinstance(output, str) else output
    normalized = _normalize(parsed)
    path = SNAPSHOT_DIR / f"{name}.json"

    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
        return

    expected = json.loads(path.read_text())
    if normalized != expected:
        got = json.dumps(normalized, indent=2, sort_keys=True).splitlines()
        want = json.dumps(expected, indent=2, sort_keys=True).splitlines()
        diff = "\n".join(
            difflib.unified_diff(want, got, fromfile="expected", tofile="actual", lineterm="")
        )
        pytest.fail(f"snapshot mismatch for tool {name!r}:\n{diff}")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_embedding_model(monkeypatch):
    """Deterministic query embedding for every semantic-search test.

    Always returns FIXED_QUERY_VEC regardless of the query text — paired
    with seeding exactly one Embedding row with the same vector, so the
    cosine distance is always exactly 0 (score 1.0). Avoids engineering a
    fragile multi-row similarity ranking.
    """
    import numpy as np

    from app.services import embedding as emb_mod

    class _FakeModel:
        def embed(self, texts):
            return [np.array(FIXED_QUERY_VEC, dtype=np.float32) for _ in texts]

    monkeypatch.setattr(emb_mod, "get_model", lambda: _FakeModel())


@pytest.fixture
def frozen_health_clock(monkeypatch):
    """Pin apple_health.tools' `_today()` and `datetime.now()` to FIXED_TODAY/NOW."""
    from app.integrations.apple_health import tools as ah_tools

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED_NOW if tz else FIXED_NOW.replace(tzinfo=None)

    monkeypatch.setattr(ah_tools, "_today", lambda: FIXED_TODAY)
    monkeypatch.setattr(ah_tools, "datetime", _FrozenDT)


@pytest.fixture
def frozen_ha_clock(monkeypatch):
    """Pin homeassistant.tools' `datetime.now()` to FIXED_NOW (ha_history's
    `since` bound + `_freshness()` staleness calc both call it directly)."""
    from app.integrations.homeassistant import tools as ha_tools

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED_NOW if tz else FIXED_NOW.replace(tzinfo=None)

    monkeypatch.setattr(ha_tools, "datetime", _FrozenDT)


def _embedding_row(*, source, source_id, user_id, chunk_text, metadata=None):
    from app.services.embedding import Embedding

    return Embedding(
        source=source,
        source_id=source_id,
        user_id=user_id,
        chunk_index=0,
        chunk_text=chunk_text,
        embedding=FIXED_QUERY_VEC,
        content_hash="fixed-hash",
        metadata_json=json.dumps(metadata) if metadata is not None else None,
        created_at=FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# lastfm
# ---------------------------------------------------------------------------

class TestLastfmSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.lastfm.models import ArtistTag, Scrobble

        db_session.add_all([
            Scrobble(
                user_id=1, track_name="Weird Fishes", artist_name="Radiohead",
                album_name="In Rainbows", album_art_url="https://example.com/inrainbows.jpg",
                played_at=FIXED_NOW, mbid=None, loved=True,
            ),
            Scrobble(
                user_id=1, track_name="Fake Plastic Trees", artist_name="Radiohead",
                album_name="The Bends", album_art_url=None,
                played_at=FIXED_NOW - timedelta(hours=1), mbid=None, loved=False,
            ),
            ArtistTag(
                artist_name="Radiohead", artist_name_lower="radiohead",
                tag="alternative rock", weight=50, fetched_at=FIXED_NOW,
            ),
        ])
        db_session.commit()

    def test_lastfm_recent(self, db_session):
        from app.integrations.lastfm.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "lastfm_recent")
        with use_user(1):
            out = tool["handler"](db_session, {})
        assert_snapshot("lastfm_recent", out)

    def test_lastfm_search(self, db_session):
        from app.integrations.lastfm.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "lastfm_search")
        with use_user(1):
            out = tool["handler"](db_session, {"query": "Radiohead"})
        assert_snapshot("lastfm_search", out)

    def test_lastfm_stats(self, db_session):
        from app.integrations.lastfm.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "lastfm_stats")
        with use_user(1):
            out = tool["handler"](db_session, {"period": "all_time"})
        assert_snapshot("lastfm_stats", out)


# ---------------------------------------------------------------------------
# obsidian / vault
# ---------------------------------------------------------------------------

class TestObsidianSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.obsidian.models import VaultChunk

        db_session.add_all([
            VaultChunk(
                path="Household/Renovation/Snags.md", file_hash="hash-1",
                modified_at=FIXED_NOW, indexed_at=FIXED_NOW,
            ),
            VaultChunk(
                path="Daily Notes/Alex/2026-01-15.md", file_hash="hash-2",
                modified_at=FIXED_NOW - timedelta(hours=2), indexed_at=FIXED_NOW,
            ),
        ])
        db_session.commit()

    def test_vault_recent(self, db_session):
        from app.integrations.obsidian.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "vault_recent")
        out = tool["handler"](db_session, {})
        assert_snapshot("vault_recent", out)

    def test_vault_stats(self, db_session):
        from app.integrations.obsidian.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "vault_stats")
        out = tool["handler"](db_session, {})
        assert_snapshot("vault_stats", out)

    def test_vault_search(self, db_session, stub_embedding_model):
        from app.integrations.obsidian.tools import get_mcp_tools

        db_session.add(_embedding_row(
            source="vault", source_id="Household/Renovation/Snags.md",
            user_id=None, chunk_text="Snag register notes for the renovation.",
        ))
        db_session.commit()

        tool = next(t for t in get_mcp_tools() if t["name"] == "vault_search")
        out = tool["handler"](db_session, {"query": "renovation snags"})
        assert_snapshot("vault_search", out)


# ---------------------------------------------------------------------------
# whatsapp
# ---------------------------------------------------------------------------

class TestWhatsappSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.whatsapp.models import WhatsAppContact, WhatsAppMessage

        db_session.add_all([
            WhatsAppContact(
                jid="353851234567@s.whatsapp.net", name="Sam", notify_name="Sam",
                is_group=False, last_message_at=FIXED_NOW,
                created_at=FIXED_NOW, updated_at=FIXED_NOW,
            ),
            WhatsAppMessage(
                user_id=1, message_id="MSG1", chat_id="353851234567@s.whatsapp.net",
                chat_name="Sam", sender_id="353851234567@s.whatsapp.net",
                sender_name="Sam", is_group=False, timestamp=FIXED_NOW,
                message_type="text", body="Snags list is updated",
                media_caption=None, is_from_me=False, reply_to_id=None,
                raw_json=None, created_at=FIXED_NOW,
                source_id="MSG1", source_ts=FIXED_NOW, synced_at=FIXED_NOW, content_hash="h1",
            ),
            WhatsAppMessage(
                user_id=1, message_id="MSG2", chat_id="353851234567@s.whatsapp.net",
                chat_name="Sam", sender_id="me", sender_name="Alex", is_group=False,
                timestamp=FIXED_NOW - timedelta(minutes=30), message_type="text",
                body="Thanks, will check it tonight", media_caption=None,
                is_from_me=True, reply_to_id=None, raw_json=None,
                created_at=FIXED_NOW, source_id="MSG2", source_ts=FIXED_NOW,
                synced_at=FIXED_NOW, content_hash="h2",
            ),
        ])
        db_session.commit()

    def test_whatsapp_recent(self, db_session):
        from app.integrations.whatsapp.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "whatsapp_recent")
        with use_user(1):
            out = tool["handler"](db_session, {})
        assert_snapshot("whatsapp_recent", out)

    def test_whatsapp_search(self, db_session):
        from app.integrations.whatsapp.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "whatsapp_search")
        with use_user(1):
            out = tool["handler"](db_session, {"query": "snags"})
        assert_snapshot("whatsapp_search", out)

    def test_whatsapp_thread(self, db_session):
        from app.integrations.whatsapp.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "whatsapp_thread")
        with use_user(1):
            out = tool["handler"](db_session, {"chat_id": "353851234567@s.whatsapp.net"})
        assert_snapshot("whatsapp_thread", out)

    def test_whatsapp_contacts(self, db_session):
        from app.integrations.whatsapp.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "whatsapp_contacts")
        with use_user(1):
            out = tool["handler"](db_session, {})
        assert_snapshot("whatsapp_contacts", out)

    def test_whatsapp_stats(self, db_session):
        from app.integrations.whatsapp.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "whatsapp_stats")
        with use_user(1):
            out = tool["handler"](db_session, {})
        assert_snapshot("whatsapp_stats", out)

    def test_whatsapp_semantic_search(self, db_session, stub_embedding_model):
        from app.integrations.whatsapp.tools import get_mcp_tools

        db_session.add(_embedding_row(
            source="whatsapp", source_id="segment-1", user_id=1,
            chunk_text="Sam: Snags list is updated\nAlex: Thanks, will check it tonight",
            metadata={
                "chat_id": "353851234567@s.whatsapp.net", "chat_name": "Sam",
                "is_group": False, "start": FIXED_NOW.isoformat(),
                "end": FIXED_NOW.isoformat(), "message_count": 2,
                "participants": ["Sam", "Alex"],
            },
        ))
        db_session.commit()

        tool = next(t for t in get_mcp_tools() if t["name"] == "whatsapp_semantic_search")
        with use_user(1):
            out = tool["handler"](db_session, {"query": "snags update"})
        assert_snapshot("whatsapp_semantic_search", out)


# ---------------------------------------------------------------------------
# google_mail
# ---------------------------------------------------------------------------

class TestGoogleMailSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.google_mail.models import MailMessage

        db_session.add_all([
            MailMessage(
                user_id=1, google_message_id="gm1", thread_id="th1",
                account_email="alex@example.com", subject="Boiler service reminder",
                sender="Servicer <service@example.com>", to="alex@example.com",
                date=FIXED_NOW, snippet="Your annual boiler service is due",
                labels="INBOX", is_read=True, is_starred=False,
                has_attachments=False, size_estimate=1200,
                synced_at=FIXED_NOW,
            ),
            MailMessage(
                user_id=1, google_message_id="gm2", thread_id="th1",
                account_email="alex@example.com", subject="Re: Boiler service reminder",
                sender="alex@example.com", to="service@example.com",
                date=FIXED_NOW - timedelta(hours=1), snippet="Confirmed for next Tuesday",
                labels="SENT", is_read=True, is_starred=True,
                has_attachments=False, size_estimate=800,
                synced_at=FIXED_NOW,
            ),
        ])
        db_session.commit()

    def test_gmail_recent(self, db_session):
        from app.integrations.google_mail.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "gmail_recent")
        with use_user(1):
            out = tool["handler"](db_session, {})
        assert_snapshot("gmail_recent", out)

    def test_gmail_search(self, db_session):
        from app.integrations.google_mail.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "gmail_search")
        with use_user(1):
            out = tool["handler"](db_session, {"query": "boiler"})
        assert_snapshot("gmail_search", out)

    def test_gmail_stats(self, db_session):
        from app.integrations.google_mail.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "gmail_stats")
        with use_user(1):
            out = tool["handler"](db_session, {})
        assert_snapshot("gmail_stats", out)

    def test_gmail_semantic_search(self, db_session, stub_embedding_model):
        from app.integrations.google_mail.tools import get_mcp_tools

        db_session.add(_embedding_row(
            source="email", source_id="gm1", user_id=1,
            chunk_text="Your annual boiler service is due for renewal next month.",
        ))
        db_session.commit()

        tool = next(t for t in get_mcp_tools() if t["name"] == "gmail_semantic_search")
        with use_user(1):
            out = tool["handler"](db_session, {"query": "boiler service"})
        assert_snapshot("gmail_semantic_search", out)


# ---------------------------------------------------------------------------
# coffee
# ---------------------------------------------------------------------------

class TestCoffeeSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.coffee.models import Coffee, CoffeeBrew, CoffeeEquipmentProfile

        long_ago = datetime(2020, 1, 1, tzinfo=timezone.utc)

        # rating=6 (not >=8): coffee_recommend's favourite_processes /
        # favourite_roasters are built via `list({...})` over favourites —
        # a Python set, whose iteration order is hash-seed-randomized across
        # processes. With two favourites of *different* processes, the
        # snapshot would flap between process runs. Keeping only one
        # favourite (see `favourite` below) collapses those sets to a single
        # element, which is order-stable regardless of hash seed.
        current = Coffee(
            name="Honey Granada", roaster="Hillside Roasters", origin_country="Colombia",
            region_farm="Granada", process="Honey", fermentation=None, variety="Castillo",
            altitude_masl="1700", roast_date=date(2026, 1, 1), purchase_date=date(2026, 1, 5),
            roaster_tasting_notes="Red apple, honey, caramel", category="Light",
            price=16.50, weight_g=250, status="current", photo_url=None,
            notes="Great as filter", rating=6,
            created_at=long_ago, updated_at=long_ago,
        )
        favourite = Coffee(
            name="Yirgacheffe Natural", roaster="Hillside Roasters", origin_country="Ethiopia",
            region_farm="Yirgacheffe", process="Natural", fermentation=None, variety="Heirloom",
            altitude_masl="2000", roast_date=date(2020, 1, 1), purchase_date=date(2020, 1, 3),
            roaster_tasting_notes="Blueberry, stone fruit", category="Light",
            price=15.0, weight_g=250, status="finished", photo_url=None,
            notes="Loved this one", rating=9,
            created_at=long_ago, updated_at=long_ago,
        )
        db_session.add_all([current, favourite])
        db_session.flush()

        equipment = CoffeeEquipmentProfile(
            name="Home V60", method="filter", grinder_name="Wilfa", grinder_type="stepless",
            grinder_setting_label=None, grinder_setting_range=None, brewer_name="Ceado Hoop",
            brewer_type="pour-over", has_pid=False, has_pressure_gauge=False,
            portafilter_mm=None, pressure_range=None,
            default_dose_g=15, default_yield_g=None, default_water_g=250,
            default_temp_c=94, default_time_s=180, default_pressure_bar=None,
            is_default=True, notes=None, created_at=long_ago, updated_at=long_ago,
        )
        db_session.add(equipment)
        db_session.flush()

        brew = CoffeeBrew(
            user_id=1, coffee_id=current.id, equipment_profile_id=equipment.id,
            method="filter", brew_context="home", cafe_name=None, drink_type=None,
            brewed_at=FIXED_NOW, dose_g=15, grind_setting="18", water_temp_c=94,
            yield_g=None, time_s=None, pressure_bar=None,
            water_g=250, brew_time_s=210, filter_ratio=16.67,
            acidity=3, sweetness=4, body=3, bitterness=2, overall=4,
            flavour_notes=["red apple", "honey"], milk_drink=False, milk_type=None,
            milk_temp_c=None, extraction_assessment="good", ai_suggestion=None,
            notes="Nice and clean", created_at=FIXED_NOW, updated_at=FIXED_NOW,
        )
        db_session.add(brew)
        db_session.commit()

        self.current_id = current.id
        self.favourite_id = favourite.id

    def test_coffee_search(self, db_session):
        from app.integrations.coffee.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "coffee_search")
        out = tool["handler"](db_session, {"query": "Ethiopia"})
        assert_snapshot("coffee_search", out)

    def test_coffee_recent_brews(self, db_session):
        from app.integrations.coffee.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "coffee_recent_brews")
        with use_user(1):
            out = tool["handler"](db_session, {})
        assert_snapshot("coffee_recent_brews", out)

    def test_coffee_stats(self, db_session):
        from app.integrations.coffee.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "coffee_stats")
        with use_user(1):
            out = tool["handler"](db_session, {"period": "all_time"})
        assert_snapshot("coffee_stats", out)

    def test_coffee_current(self, db_session):
        from app.integrations.coffee.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "coffee_current")
        out = tool["handler"](db_session, {})
        assert_snapshot("coffee_current", out)

    def test_coffee_recommend(self, db_session):
        from app.integrations.coffee.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "coffee_recommend")
        out = tool["handler"](db_session, {})
        assert_snapshot("coffee_recommend", out)

    def test_coffee_dial_in(self, db_session):
        from app.integrations.coffee.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "coffee_dial_in")
        with use_user(1):
            out = tool["handler"](db_session, {"coffee_id": self.current_id})
        assert_snapshot("coffee_dial_in", out)

    def test_coffee_similar(self, db_session, stub_embedding_model):
        from app.integrations.coffee.tools import get_mcp_tools

        db_session.add(_embedding_row(
            source="coffee", source_id=str(self.favourite_id), user_id=None,
            chunk_text="Yirgacheffe Natural | Hillside Roasters | Ethiopia | Yirgacheffe | Natural",
        ))
        db_session.commit()

        tool = next(t for t in get_mcp_tools() if t["name"] == "coffee_similar")
        out = tool["handler"](db_session, {"query": "fruity natural Ethiopian"})
        assert_snapshot("coffee_similar", out)


# ---------------------------------------------------------------------------
# finance
# ---------------------------------------------------------------------------

class TestFinanceSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.finance.models import Account, Category, Transaction

        account = Account(name="AIB Current", type="AIB")
        groceries = Category(name="Groceries", is_active=True)
        db_session.add_all([account, groceries])
        db_session.flush()

        # January 2026 data (period="2026-01" is absolute — deterministic
        # regardless of the real test-run date).
        db_session.add_all([
            Transaction(
                account_id=account.id, date=date(2026, 1, 5), description="TESCO STORES",
                merchant="Tesco", amount=-45.30, balance=1000.0, currency="EUR",
                category_id=groceries.id, is_manual_category=False,
                is_internal_transfer=False, transaction_type="debit", source_file="test.csv",
            ),
            Transaction(
                account_id=account.id, date=date(2026, 1, 10), description="SALARY",
                merchant=None, amount=2500.0, balance=3500.0, currency="EUR",
                category_id=None, is_manual_category=False,
                is_internal_transfer=False, transaction_type="credit", source_file="test.csv",
            ),
            Transaction(
                account_id=account.id, date=date(2026, 1, 12), description="RANDOM SHOP XYZ",
                merchant=None, amount=-12.99, balance=3487.01, currency="EUR",
                category_id=None, is_manual_category=False,
                is_internal_transfer=False, transaction_type="debit", source_file="test.csv",
            ),
        ])

        # Recurring monthly subscription: 4 charges exactly 30 days apart —
        # avg_interval=30 (monthly), zero variance → confidence 1.0.
        sub_dates = [date(2025, 10, 16), date(2025, 11, 15), date(2025, 12, 15), date(2026, 1, 14)]
        for d in sub_dates:
            db_session.add(Transaction(
                account_id=account.id, date=d, description="SPOTIFY",
                merchant="Spotify", amount=-11.99, balance=None, currency="EUR",
                category_id=None, is_manual_category=False,
                is_internal_transfer=False, transaction_type="debit", source_file="test.csv",
            ))

        db_session.commit()

    def test_finance_summary(self, db_session):
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_summary")
        out = tool["handler"](db_session, {"period": "2026-01"})
        assert_snapshot("finance_summary", out)

    def test_finance_transactions(self, db_session):
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_transactions")
        out = tool["handler"](db_session, {"start_date": "2026-01-01", "end_date": "2026-01-31"})
        assert_snapshot("finance_transactions", out)

    def test_finance_categories(self, db_session):
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_categories")
        out = tool["handler"](db_session, {"period": "2026-01"})
        assert_snapshot("finance_categories", out)

    def test_finance_trends(self, db_session):
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_trends")
        # Data spans 4 distinct months (Oct'25-Jan'26); months=4 pulls exactly
        # those, deterministic regardless of the real test-run date since
        # get_monthly_trend groups existing rows rather than filtering by
        # "today - N months".
        out = tool["handler"](db_session, {"months": 4})
        assert_snapshot("finance_trends", out)

    def test_finance_subscriptions(self, db_session):
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_subscriptions")
        out = tool["handler"](db_session, {"period": "all"})
        assert_snapshot("finance_subscriptions", out)

    def test_finance_top_merchants(self, db_session):
        """get_top_merchants() (services.py) previously did
        `.group_by("merchant", "category")` with bare string labels.
        `Transaction.merchant` is a REAL column (distinct from the `merchant`
        *alias* computed as `lower(trim(description))`), so Postgres bound
        the GROUP BY identifier to the real column, not the SELECT alias —
        then rejected the query because `description` isn't functionally
        dependent on the (non-primary-key) `merchant` column. Fixed by
        grouping on the actual expressions instead of string labels.
        """
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_top_merchants")
        out = tool["handler"](db_session, {"period": "2026-01"})
        assert_snapshot("finance_top_merchants", out)

    def test_finance_compare(self, db_session):
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_compare")
        out = tool["handler"](db_session, {"period_a": "2026-01", "period_b": "2025-12"})
        assert_snapshot("finance_compare", out)

    def test_finance_accounts(self, db_session):
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_accounts")
        out = tool["handler"](db_session, {"period": "all"})
        assert_snapshot("finance_accounts", out)

    def test_finance_uncategorized(self, db_session):
        from app.integrations.finance.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "finance_uncategorized")
        out = tool["handler"](db_session, {})
        assert_snapshot("finance_uncategorized", out)


# ---------------------------------------------------------------------------
# apple_health
# ---------------------------------------------------------------------------

class TestAppleHealthSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.apple_health.models import (
            HealthDailyMetric, HealthSleepSession, HealthWorkout,
        )

        today = FIXED_TODAY
        yesterday = today - timedelta(days=1)

        metrics = []
        for d, steps, hrv in ((today, 8000.0, 55.0), (yesterday, 7200.0, 50.0)):
            for metric_type, value in (
                ("steps", steps), ("distance_km", 6.2), ("active_energy_kcal", 420.0),
                ("resting_hr_bpm", 58.0), ("hr_avg_bpm", 72.0), ("hr_min_bpm", 50.0),
                ("hr_max_bpm", 140.0), ("hrv_ms", hrv),
            ):
                metrics.append(HealthDailyMetric(
                    user_id=1, date=d, metric_type=metric_type, value=value,
                    synced_at=FIXED_NOW,
                ))
        db_session.add_all(metrics)

        sleep_start = datetime(today.year, today.month, today.day, 0, 30, tzinfo=timezone.utc) - timedelta(hours=8)
        db_session.add_all([
            HealthSleepSession(
                user_id=1, uid="sleep-deep-1", start_time=sleep_start,
                end_time=sleep_start + timedelta(hours=1.5), stage="asleepDeep",
                duration_hours=1.5, synced_at=FIXED_NOW,
            ),
            HealthSleepSession(
                user_id=1, uid="sleep-rem-1", start_time=sleep_start + timedelta(hours=1.5),
                end_time=sleep_start + timedelta(hours=3.0), stage="asleepREM",
                duration_hours=1.5, synced_at=FIXED_NOW,
            ),
        ])

        workout_start = datetime(today.year, today.month, today.day, 7, 0, tzinfo=timezone.utc)
        db_session.add(HealthWorkout(
            user_id=1, uid="workout-1", workout_type="strength_training",
            start_time=workout_start, end_time=workout_start + timedelta(minutes=45),
            duration_seconds=2700.0, distance_km=0.0, active_energy_kcal=300.0,
            avg_heart_rate_bpm=125.0, synced_at=FIXED_NOW,
        ))
        db_session.commit()

    def test_health_today(self, db_session):
        from app.integrations.apple_health.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "health_today")
        with use_user(1):
            out = tool["handler"](db_session, {"date": FIXED_TODAY.isoformat()})
        assert_snapshot("health_today", out)

    def test_health_sleep(self, db_session):
        from app.integrations.apple_health.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "health_sleep")
        with use_user(1):
            out = tool["handler"](db_session, {"date": FIXED_TODAY.isoformat()})
        assert_snapshot("health_sleep", out)

    def test_health_workouts(self, db_session, frozen_health_clock):
        from app.integrations.apple_health.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "health_workouts")
        with use_user(1):
            out = tool["handler"](db_session, {"days": 7})
        assert_snapshot("health_workouts", out)

    def test_health_trends(self, db_session, frozen_health_clock):
        from app.integrations.apple_health.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "health_trends")
        with use_user(1):
            out = tool["handler"](db_session, {"days": 2})
        assert_snapshot("health_trends", out)

    def test_health_summary(self, db_session, frozen_health_clock):
        from app.integrations.apple_health.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "health_summary")
        with use_user(1):
            out = tool["handler"](db_session, {})
        assert_snapshot("health_summary", out)

    def test_health_exercise_status(self, db_session, frozen_health_clock):
        from app.integrations.apple_health.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "health_exercise_status")
        with use_user(1):
            out = tool["handler"](db_session, {"week_of": FIXED_TODAY.isoformat()})
        assert_snapshot("health_exercise_status", out)

    def test_health_weekly_summary(self, db_session):
        from app.integrations.apple_health.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "health_weekly_summary")
        # Monday of FIXED_TODAY's week.
        monday = FIXED_TODAY - timedelta(days=FIXED_TODAY.weekday())
        with use_user(1):
            out = tool["handler"](db_session, {"week_start": monday.isoformat()})
        assert_snapshot("health_weekly_summary", out)


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------

class TestMediaSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.media.models import MediaItem

        db_session.add_all([
            MediaItem(
                user_id=1, source="whatsapp", message_ref="WA-MEDIA-1", media_type="image",
                mime_type="image/jpeg", size_bytes=204800, caption="Snag - Kitchen - tile crack",
                sender_name="WindowCo", chat_or_thread="Snags", is_from_me=False,
                message_ts=FIXED_NOW, status="stored", skip_reason=None,
                storage_path="/data/media/wa-media-1.jpg", sha256="fixed-sha-1",
                downloaded_at=FIXED_NOW, detected_at=FIXED_NOW,
            ),
            MediaItem(
                user_id=1, source="whatsapp", message_ref="WA-MEDIA-2", media_type="video",
                mime_type="video/mp4", size_bytes=1048576, caption=None,
                sender_name="Sam", chat_or_thread="Family", is_from_me=False,
                message_ts=FIXED_NOW - timedelta(hours=3), status="indexed", skip_reason=None,
                storage_path=None, sha256=None, downloaded_at=None, detected_at=FIXED_NOW,
            ),
        ])
        db_session.commit()

    def test_media_recent(self, db_session):
        from app.integrations.media.tools import mcp_tools
        tool = next(t for t in mcp_tools() if t["name"] == "media_recent")
        with use_user(1):
            out = tool["handler"](db_session, {"limit": 50})
        assert_snapshot("media_recent", out)


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------

class TestAttachmentsSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.attachments.models import MessageAttachment

        db_session.add_all([
            MessageAttachment(
                user_id=1, source="whatsapp", message_ref="WA-ATT-1", filename="BoQ.pdf",
                mime_type="application/pdf", size_bytes=204800, sender_name="WindowCo",
                chat_or_thread="Snags", message_ts=FIXED_NOW, parse_status="pending",
                skip_reason=None, storage_path=None, historical_doc_id=None,
                detected_at=FIXED_NOW, processed_at=None,
            ),
            MessageAttachment(
                user_id=1, source="whatsapp", message_ref="WA-ATT-2", filename="invoice.pdf",
                mime_type="application/pdf", size_bytes=51200, sender_name="Sam",
                chat_or_thread="Family", message_ts=FIXED_NOW - timedelta(days=1),
                parse_status="ingested", skip_reason=None,
                storage_path="/data/attachments/invoice.pdf", historical_doc_id=None,
                detected_at=FIXED_NOW - timedelta(days=1), processed_at=FIXED_NOW,
            ),
        ])
        db_session.commit()

    def test_attachments_pending(self, db_session):
        from app.integrations.attachments.tools import mcp_tools
        tool = next(t for t in mcp_tools() if t["name"] == "attachments_pending")
        with use_user(1):
            out = tool["handler"](db_session, {"status": "pending"})
        assert_snapshot("attachments_pending", out)

    def test_attachments_search(self, db_session):
        from app.integrations.attachments.tools import mcp_tools
        tool = next(t for t in mcp_tools() if t["name"] == "attachments_search")
        with use_user(1):
            out = tool["handler"](db_session, {"query": "pdf"})
        assert_snapshot("attachments_search", out)


# ---------------------------------------------------------------------------
# homeassistant
# ---------------------------------------------------------------------------

class TestHomeAssistantSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.homeassistant.models import HAEntity, HAStateChange

        synced_at = FIXED_NOW - timedelta(minutes=2)
        db_session.add_all([
            HAEntity(
                entity_id="person.alex", domain="person", friendly_name="Alex",
                area=None, device_class=None, unit=None, state="home",
                attributes={}, last_changed=FIXED_NOW - timedelta(hours=1),
                synced_at=synced_at,
            ),
            HAEntity(
                entity_id="light.kitchen", domain="light", friendly_name="Kitchen Light",
                area="Kitchen", device_class=None, unit=None, state="on",
                attributes={}, last_changed=FIXED_NOW - timedelta(minutes=30),
                synced_at=synced_at,
            ),
        ])
        db_session.add(HAStateChange(
            entity_id="person.alex", old_state="not_home", new_state="home",
            changed_at=FIXED_NOW - timedelta(hours=1), recorded_at=FIXED_NOW,
            attributes={},
        ))
        db_session.commit()

    def test_ha_home_status(self, db_session, frozen_ha_clock):
        from app.integrations.homeassistant.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "ha_home_status")
        out = tool["handler"](db_session, {})
        assert_snapshot("ha_home_status", out)

    def test_ha_entities(self, db_session, frozen_ha_clock):
        from app.integrations.homeassistant.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "ha_entities")
        out = tool["handler"](db_session, {})
        assert_snapshot("ha_entities", out)

    def test_ha_entity(self, db_session, frozen_ha_clock):
        from app.integrations.homeassistant.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "ha_entity")
        out = tool["handler"](db_session, {"entity_ids": ["person.alex"]})
        assert_snapshot("ha_entity", out)

    def test_ha_history(self, db_session, frozen_ha_clock):
        from app.integrations.homeassistant.tools import get_mcp_tools
        tool = next(t for t in get_mcp_tools() if t["name"] == "ha_history")
        out = tool["handler"](db_session, {"entity_id": "person.alex", "days": 7})
        assert_snapshot("ha_history", out)


# ---------------------------------------------------------------------------
# snags
# ---------------------------------------------------------------------------

class TestSnagSnapshots:
    @pytest.fixture(autouse=True)
    def _seed(self, db_session):
        from app.integrations.snags.models import Snag

        db_session.add_all([
            Snag(
                uid="SNAG-0001", title="Cracked kitchen floor tile", description="Corner tile cracked during move-in",
                room="Kitchen", element="Floor tile", trade="windowco", severity="minor",
                status="open", reported_by="Alex", reported_at=FIXED_NOW,
                source_ref=None, reported_to_trade_at=None, external_ref=None,
                resolution_note=None, resolved_at=None,
                created_at=FIXED_NOW, updated_at=FIXED_NOW,
            ),
            Snag(
                uid="SNAG-0002", title="Bathroom extractor fan noisy", description="Rattles on high speed",
                room="Main Bathroom", element="Extractor fan", trade="electrician", severity="cosmetic",
                status="closed", reported_by="Sam", reported_at=FIXED_NOW - timedelta(days=2),
                source_ref=None, reported_to_trade_at=FIXED_NOW - timedelta(days=1),
                external_ref="TICKET-42", resolution_note="Rebalanced fan blade", resolved_at=FIXED_NOW,
                created_at=FIXED_NOW - timedelta(days=2), updated_at=FIXED_NOW,
            ),
        ])
        db_session.commit()

    def test_snag_list_open(self, db_session):
        from app.integrations.snags.tools import mcp_tools
        tool = next(t for t in mcp_tools() if t["name"] == "snag_list")
        out = tool["handler"](db_session, {})
        assert_snapshot("snag_list_open", out)

    def test_snag_list_all(self, db_session):
        from app.integrations.snags.tools import mcp_tools
        tool = next(t for t in mcp_tools() if t["name"] == "snag_list")
        out = tool["handler"](db_session, {"include_closed": True})
        assert_snapshot("snag_list_all", out)
