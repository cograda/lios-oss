"""Harness smoke tests + migration-chain coverage (db tier).

The baseline alembic revision is an empty stamp (production predates
alembic), so `upgrade head` can't build a schema from an empty database.
What we CAN regression-test:

  1. The production fresh-DB bootstrap path (create_tables + stamp head)
     produces a schema that alembic considers current — and that agrees
     with the ORM (no model change without a migration sneaks through).
  2. The recent migration chain round-trips: downgrade across the newest
     revisions, then upgrade back to head, on a scratch database.
"""

import pytest
from sqlalchemy import inspect, text

pytestmark = pytest.mark.db


def _alembic_config(url: str):
    from pathlib import Path

    from alembic.config import Config

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_schema_is_stamped_at_head(test_db):
    from alembic.script import ScriptDirectory

    cfg = _alembic_config(str(test_db.engine.url))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    with test_db.session() as session:
        current = session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()

    assert current == head


def test_all_orm_tables_exist(test_db):
    from coglib import Base

    existing = set(inspect(test_db.engine).get_table_names())
    missing = {t.name for t in Base.metadata.sorted_tables} - existing
    assert not missing, f"ORM tables missing from migrated schema: {missing}"


def test_users_seeded(db_session):
    from app.models.users import User

    names = {u.id: u.name for u in db_session.query(User).all()}
    assert names == {1: "alex", 2: "sam"}


def test_hnsw_index_present_on_every_vector_space(test_db):
    """Each space carries its own ANN index (Phase 2).

    Driven off VECTOR_MODELS rather than a hardcoded list so adding a third
    space can't quietly ship without an index — the failure mode there is not
    an error but a full scan on every semantic search.
    """
    from app.integrations.embedding.models import VECTOR_MODELS

    inspector = inspect(test_db.engine)
    for provider_id, model in VECTOR_MODELS.items():
        indexes = inspector.get_indexes(model.__tablename__)
        assert any(
            ix.get("dialect_options", {}).get("postgresql_using") == "hnsw"
            or "hnsw" in ix["name"]
            for ix in indexes
        ), f"{provider_id} ({model.__tablename__}) has no HNSW index: {indexes}"


def test_orm_and_migrations_agree(test_db):
    """No drift between models and the stamped schema (alembic check)."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from coglib import Base

    with test_db.engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": False, "compare_server_default": False}
        )
        diffs = compare_metadata(ctx, Base.metadata)

    assert not diffs, f"ORM/schema drift detected: {diffs}"


def test_recent_migration_chain_roundtrip(pg_url, test_db, monkeypatch):
    """Downgrade across the newest revisions and upgrade back to head.

    Runs on a scratch database so a mid-chain failure can't poison the
    shared test schema. Target: the revision just before the Phase 0
    block (2026-06-10), so all five Phase 0 migrations get exercised in
    both directions.
    """
    from alembic import command

    from coglib import Database
    from app.db import _run_migrations

    scratch = "comar_migration_roundtrip"
    with test_db.engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        conn.execute(text(f"CREATE DATABASE {scratch}"))

    scratch_url = pg_url.rsplit("/", 1)[0] + f"/{scratch}"
    # alembic/env.py prefers HOME_DATABASE__URL over the passed config —
    # point it at the scratch DB or the round-trip would run on the shared one.
    monkeypatch.setenv("HOME_DATABASE__URL", scratch_url)
    db = Database(url=scratch_url)
    try:
        with db.session() as session:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _run_migrations(db)

        cfg = _alembic_config(scratch_url)
        command.downgrade(cfg, "b1c2d3e4f5a6")  # pre-Phase-0 (mcp_oauth)
        command.upgrade(cfg, "head")
    finally:
        db.engine.dispose()
        with test_db.engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))


def test_token_hardening_migration_backfills_and_resolves(pg_url, test_db, monkeypatch):
    """V4 chunk 2.3 — `token_hardening` migration on a seeded scratch DB.

    Builds schema up to just before the migration, inserts plaintext-era
    rows directly (the shape client_tokens/mcp_access_tokens had pre-hash),
    upgrades through it, and asserts:
      - the plaintext columns are gone
      - token_hash/access_token_hash equal sha256(plaintext) — i.e. hashing
        the same plaintext again and looking it up by hash still resolves
        the row, which is exactly what app.auth.client_token /
        app.auth.oauth_provider do at request time
      - last4 columns match the tail of the original plaintext
      - client_tokens.expires_at was backfilled (non-null, in the future)
    """
    import hashlib

    from alembic import command

    from coglib import Database
    from app.db import _run_migrations

    scratch = "comar_token_hardening_migration"
    with test_db.engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        conn.execute(text(f"CREATE DATABASE {scratch}"))

    scratch_url = pg_url.rsplit("/", 1)[0] + f"/{scratch}"
    monkeypatch.setenv("HOME_DATABASE__URL", scratch_url)
    db = Database(url=scratch_url)
    try:
        with db.session() as session:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _run_migrations(db)

        cfg = _alembic_config(scratch_url)
        # Land just before this migration — client_tokens.token and
        # mcp_access_tokens.{access_token,refresh_token} are still plaintext.
        command.downgrade(cfg, "b4c5d6e7f8a9")

        plaintext_client_token = "scratch-plaintext-client-token"
        plaintext_access_token = "scratch-plaintext-access-token"
        plaintext_refresh_token = "scratch-plaintext-refresh-token"

        with db.session() as session:
            # client_tokens.user_id and mcp_access_tokens.user_id are FK ->
            # users.id — this scratch DB has no seed data, so the owning
            # user must exist before either insert below.
            session.execute(
                text(
                    "INSERT INTO users (id, name, display_name) "
                    "VALUES (1, 'alex', 'Alex') ON CONFLICT (id) DO NOTHING"
                )
            )
            session.execute(
                text(
                    "INSERT INTO client_tokens "
                    "(user_id, token, label, is_active, created_at) "
                    "VALUES (:uid, :token, 'scratch', true, now())"
                ),
                {"uid": 1, "token": plaintext_client_token},
            )
            session.execute(
                text(
                    "INSERT INTO mcp_access_tokens "
                    "(user_id, access_token, refresh_token, client_id, scopes, "
                    " expires_at, revoked, created_at) "
                    "VALUES (:uid, :access, :refresh, 'scratch-client', '[]', "
                    " now() + interval '1 hour', false, now())"
                ),
                {
                    "uid": 1,
                    "access": plaintext_access_token,
                    "refresh": plaintext_refresh_token,
                },
            )
            session.commit()

        command.upgrade(cfg, "head")

        expected_client_hash = hashlib.sha256(
            plaintext_client_token.encode("utf-8")
        ).hexdigest()
        expected_access_hash = hashlib.sha256(
            plaintext_access_token.encode("utf-8")
        ).hexdigest()
        expected_refresh_hash = hashlib.sha256(
            plaintext_refresh_token.encode("utf-8")
        ).hexdigest()

        with db.session() as session:
            cols = {
                r[0] for r in session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'client_tokens'"
                    )
                )
            }
            assert "token" not in cols
            assert "token_hash" in cols and "token_last4" in cols and "expires_at" in cols

            row = session.execute(
                text(
                    "SELECT token_hash, token_last4, expires_at FROM client_tokens "
                    "WHERE token_hash = :h"
                ),
                {"h": expected_client_hash},
            ).one()
            assert row.token_hash == expected_client_hash
            assert row.token_last4 == plaintext_client_token[-4:]
            assert row.expires_at is not None

            mcp_cols = {
                r[0] for r in session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'mcp_access_tokens'"
                    )
                )
            }
            assert "access_token" not in mcp_cols and "refresh_token" not in mcp_cols
            assert "access_token_hash" in mcp_cols and "refresh_token_hash" in mcp_cols

            mcp_row = session.execute(
                text(
                    "SELECT access_token_hash, access_token_last4, "
                    "refresh_token_hash, refresh_token_last4 FROM mcp_access_tokens "
                    "WHERE access_token_hash = :h"
                ),
                {"h": expected_access_hash},
            ).one()
            assert mcp_row.access_token_hash == expected_access_hash
            assert mcp_row.access_token_last4 == plaintext_access_token[-4:]
            assert mcp_row.refresh_token_hash == expected_refresh_hash
            assert mcp_row.refresh_token_last4 == plaintext_refresh_token[-4:]
    finally:
        db.engine.dispose()
        with test_db.engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))


def test_per_space_vector_migration_moves_existing_vectors(pg_url, test_db, monkeypatch):
    """Phase 2 — vectors survive the move off `embeddings` onto a space table.

    The round-trip test above runs the same migration on an empty database, so
    it proves the DDL is valid and reversible but never touches the
    `INSERT INTO ... SELECT` that carries the data. Production has ~70k vectors
    and exactly one attempt at this, so the copy is seeded and asserted here.

    Also covers the COALESCE: `model_name` was nullable on `embeddings` and is
    NOT NULL on the space tables, so any legacy NULL has to be resolved to the
    model those rows were actually built with — otherwise the migration fails
    partway on a real database and succeeds on every empty test one.
    """
    from alembic import command

    from coglib import Database
    from app.db import _run_migrations

    scratch = "comar_vector_space_migration"
    with test_db.engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        conn.execute(text(f"CREATE DATABASE {scratch}"))

    scratch_url = pg_url.rsplit("/", 1)[0] + f"/{scratch}"
    monkeypatch.setenv("HOME_DATABASE__URL", scratch_url)
    db = Database(url=scratch_url)
    try:
        with db.session() as session:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _run_migrations(db)

        cfg = _alembic_config(scratch_url)
        # Land just before the split, while `embeddings.embedding` still exists.
        command.downgrade(cfg, "a7b8c9d0e1f2")

        vec = "[" + ",".join(["0.5"] * 384) + "]"
        with db.session() as session:
            session.execute(
                text(
                    "INSERT INTO embeddings "
                    "(id, source, source_id, chunk_index, chunk_text, embedding, "
                    " content_hash, model_name, created_at) VALUES "
                    "(1, 'vault', 'named.md', 0, 'has a model', :v, 'h1', "
                    " 'BAAI/bge-small-en-v1.5', now()), "
                    "(2, 'vault', 'legacy.md', 0, 'model_name never set', :v, 'h2', "
                    " NULL, now())"
                ),
                {"v": vec},
            )
            session.commit()

        command.upgrade(cfg, "head")

        with db.session() as session:
            rows = session.execute(
                text(
                    "SELECT embedding_id, model_name FROM embedding_vec_bge_small_384 "
                    "ORDER BY embedding_id"
                )
            ).all()
            assert [r[0] for r in rows] == [1, 2]
            # The legacy NULL resolves to the only model that ever wrote here.
            assert [r[1] for r in rows] == [
                "BAAI/bge-small-en-v1.5", "BAAI/bge-small-en-v1.5",
            ]

            # The vector itself came across intact, not merely a row.
            same = session.execute(
                text(
                    "SELECT count(*) FROM embedding_vec_bge_small_384 "
                    "WHERE embedding <-> :v = 0"
                ),
                {"v": vec},
            ).scalar()
            assert same == 2

            # Chunk identity is untouched and the old column is gone.
            assert session.execute(
                text("SELECT count(*) FROM embeddings")
            ).scalar() == 2
            cols = {
                r[0] for r in session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'embeddings'"
                    )
                ).all()
            }
            assert "embedding" not in cols
            assert "model_name" not in cols

            # Deleting a chunk takes its vectors with it (ON DELETE CASCADE) —
            # the property delete_source() relies on instead of per-space cleanup.
            session.execute(text("DELETE FROM embeddings WHERE id = 1"))
            session.commit()
            assert session.execute(
                text("SELECT count(*) FROM embedding_vec_bge_small_384")
            ).scalar() == 1
    finally:
        db.engine.dispose()
        with test_db.engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))


def test_gmail_attachments_backfill_migration(pg_url, test_db, monkeypatch):
    """`b2c3d4e5f6a7` — pre-existing pending Gmail rows become unsupported.

    Before this migration, `attachments_scan` queued every Gmail attachment
    as 'pending' while `attachments_ingest` unconditionally rejected
    `source='gmail'` — so those rows were pending forever, by construction.
    Seeds the pre-migration shape (a pending gmail row, plus a pending
    whatsapp row and an already-ingested gmail row as controls), upgrades
    through the migration, and asserts:
      - the pending gmail row flips to 'unsupported' with a reason recorded
      - it is NOT deleted (still exists, same message_ref)
      - the whatsapp row and the already-ingested gmail row are untouched
        (this migration must not touch rows outside its narrow WHERE clause)
    """
    from alembic import command

    from coglib import Database
    from app.db import _run_migrations

    scratch = "comar_gmail_attachments_backfill"
    with test_db.engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        conn.execute(text(f"CREATE DATABASE {scratch}"))

    scratch_url = pg_url.rsplit("/", 1)[0] + f"/{scratch}"
    monkeypatch.setenv("HOME_DATABASE__URL", scratch_url)
    db = Database(url=scratch_url)
    try:
        with db.session() as session:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _run_migrations(db)

        cfg = _alembic_config(scratch_url)
        # Land just before this migration.
        command.downgrade(cfg, "f7a2c4e9b1d3")

        with db.session() as session:
            session.execute(
                text(
                    "INSERT INTO users (id, name, display_name) "
                    "VALUES (1, 'alex', 'Alex') ON CONFLICT (id) DO NOTHING"
                )
            )
            session.execute(text(
                "INSERT INTO message_attachments "
                "(user_id, source, message_ref, filename, mime_type, parse_status) "
                "VALUES "
                "(1, 'gmail', 'm_pending', 'invoice.pdf', 'application/pdf', 'pending'), "
                "(1, 'whatsapp', 'm_wa', 'boq.pdf', 'application/pdf', 'pending'), "
                "(1, 'gmail', 'm_ingested', 'old.pdf', 'application/pdf', 'ingested')"
            ))
            session.commit()

        command.upgrade(cfg, "head")

        with db.session() as session:
            rows = {
                r.message_ref: r
                for r in session.execute(text(
                    "SELECT message_ref, source, parse_status, skip_reason "
                    "FROM message_attachments"
                ))
            }

        assert len(rows) == 3  # nothing deleted

        gmail_pending = rows["m_pending"]
        assert gmail_pending.parse_status == "unsupported"
        assert gmail_pending.skip_reason == "source 'gmail' not supported yet"

        # Controls: untouched.
        assert rows["m_wa"].parse_status == "pending"
        assert rows["m_wa"].skip_reason is None
        assert rows["m_ingested"].parse_status == "ingested"
    finally:
        db.engine.dispose()
        with test_db.engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))


def test_whatsapp_chat_name_backfill_migration(pg_url, test_db, monkeypatch):
    """`c3d4e5f6a7b8` — pre-existing null chat_name backfilled from whatsapp_contacts.

    The bridge (`server/whatsapp-bridge/src/index.js`) hardcoded `chatName:
    null` on every message since forever, so `whatsapp_messages.chat_name` is
    null for all pre-existing rows even though the real name was captured
    correctly the whole time in the sibling `whatsapp_contacts` table via a
    separate Baileys event stream. This migration recovers it via a join on
    (chat_id = jid, user_id = user_id).

    The scoping case is the one that matters most: a WhatsApp `@lid` is
    account-scoped, not globally unique, so the SAME jid can legitimately
    name two different conversations under two different comar users. Seeds
    that exact shape — jid `555@lid` is "Alex's Mother" for user 1 and
    "Sam's Book Club" for user 2 — and asserts each user's message gets
    only their own user's name, never the other's. Also asserts the
    migration:
      - never touches a row that already has a chat_name
      - never touches a row whose jid has no matching contact (name stays
        NULL, not deleted)
      - is UPDATE-only (row count unchanged, no deletions)
    """
    from alembic import command

    from coglib import Database
    from app.db import _run_migrations

    scratch = "comar_whatsapp_chat_name_backfill"
    with test_db.engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        conn.execute(text(f"CREATE DATABASE {scratch}"))

    scratch_url = pg_url.rsplit("/", 1)[0] + f"/{scratch}"
    monkeypatch.setenv("HOME_DATABASE__URL", scratch_url)
    db = Database(url=scratch_url)
    try:
        with db.session() as session:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _run_migrations(db)

        cfg = _alembic_config(scratch_url)
        # Land just before this migration.
        command.downgrade(cfg, "b2c3d4e5f6a7")

        with db.session() as session:
            session.execute(text(
                "INSERT INTO users (id, name, display_name) VALUES "
                "(1, 'alex', 'Alex'), (2, 'sam', 'Sam') "
                "ON CONFLICT (id) DO NOTHING"
            ))

            # Contacts: the same jid means something different per user.
            session.execute(text(
                "INSERT INTO whatsapp_contacts "
                "(user_id, jid, name, is_group) VALUES "
                "(1, '555@lid', 'Alex''s Mother', false), "
                "(2, '555@lid', 'Sam''s Book Club', false), "
                "(1, '999@g.us', 'Already Named Group', true), "
                # A GROUP whose contact 'name' is a participant, not the
                # subject. Real case: the school parents group is stored as
                # "Sam", who merely posts in it. Must NOT be backfilled.
                "(1, '777@g.us', 'Sam', true)"
            ))

            # Messages: pending backfill (user1/user1), pending backfill
            # (user2, same jid as user1's row — the scoping case), already
            # has a name (must not be overwritten), and a jid with no
            # matching contact (must stay NULL, not be deleted).
            session.execute(text(
                "INSERT INTO whatsapp_messages "
                "(user_id, message_id, chat_id, chat_name, sender_id, "
                " is_group, timestamp, message_type, is_from_me) VALUES "
                "(1, 'm_u1', '555@lid', NULL, '555@lid', false, NOW(), 'text', false), "
                "(2, 'm_u2', '555@lid', NULL, '555@lid', false, NOW(), 'text', false), "
                "(1, 'm_has_name', '999@g.us', 'Old Manual Name', '999@g.us', true, NOW(), 'text', false), "
                "(1, 'm_unknown', 'unknown@lid', NULL, 'unknown@lid', false, NOW(), 'text', false), "
                "(1, 'm_group', '777@g.us', NULL, '777@g.us', true, NOW(), 'text', false)"
            ))
            session.commit()

        command.upgrade(cfg, "head")

        with db.session() as session:
            rows = {
                r.message_id: r
                for r in session.execute(text(
                    "SELECT message_id, user_id, chat_id, chat_name "
                    "FROM whatsapp_messages"
                ))
            }

        assert len(rows) == 5  # nothing deleted

        # The scoping case: same jid, two users, two different names — and
        # each message must get ONLY its own user's name.
        assert rows["m_u1"].chat_name == "Alex's Mother"
        assert rows["m_u2"].chat_name == "Sam's Book Club"

        # Groups are excluded: whatsapp_contacts.name for an @g.us jid is a
        # participant's name, not the group subject, so backfilling it would
        # replace a visible null with a confident wrong answer. Null is the
        # correct outcome here until the bridge consumes group metadata.
        assert rows["m_group"].chat_name is None

        # Pre-existing name is never overwritten.
        assert rows["m_has_name"].chat_name == "Old Manual Name"

        # No matching contact — left NULL, not deleted.
        assert rows["m_unknown"].chat_name is None
    finally:
        db.engine.dispose()
        with test_db.engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
