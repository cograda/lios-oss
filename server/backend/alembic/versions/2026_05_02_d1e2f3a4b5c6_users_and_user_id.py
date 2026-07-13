"""multi-user foundation: users table + user_id FK on per-user tables

Phase A of multi-user-e2e (.claude/plans/multi-user-e2e.md).

Creates the `users` table and adds a `user_id` foreign key to every per-user
data table. Backfills all existing rows to user_id=1 (Alex). Sam seeded as
user_id=2 — no data attributed yet, but schema is ready for her client to
connect.

Per-user tables (gain user_id):
    client_tokens, client_logs           (had free-text 'user' string → drop)
    oauth_tokens                         (had no user column)
    reminders, reminder_commands         (had no user column; reminders also
                                          gains `account_email` for EventKit
                                          multi-account routing)
    mail_messages
    scrobbles
    whatsapp_messages                    (server_default='1' retained so the
                                          single-user Node bridge keeps writing
                                          without code changes; second sidecar
                                          in Phase F will INSERT user_id=2
                                          explicitly)
    coffee_brews
    message_attachments
    health_daily_metrics, health_workouts, health_sleep_sessions
                                          (had free-text 'user' string → drop)

Shared / unchanged: finance, vault, historical_corpus, weather, irish_rail,
artist_tags, coffees, coffee_equipment_profiles, whatsapp_contacts.

Composite unique constraints rebuilt to start with user_id; indexes added on
user_id (and on common (user_id, ts) lookup pairs).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_user_id(
    table: str,
    *,
    keep_default: bool = False,
    backfill_from_name: bool = False,
) -> None:
    """Add user_id column, backfill, set NOT NULL + FK + index.

    Args:
        table: table name
        keep_default: leave server_default='1' on the column after backfill
            (used for whatsapp_messages so the Node bridge keeps inserting
            without code changes until Phase F)
        backfill_from_name: existing free-text "user" column maps onto
            users.name; backfill via a join then drop the old column
    """
    # 1. Add nullable with server_default=1 so existing rows + concurrent
    #    inserts during the migration get attributed to Alex.
    op.add_column(
        table,
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
            server_default="1",
        ),
    )

    # 2. Backfill.
    if backfill_from_name:
        # Map existing free-text user string onto users.name. Quote "user"
        # because it's a reserved word in some SQL dialects.
        op.execute(
            f'UPDATE {table} SET user_id = users.id '
            f'FROM users '
            f'WHERE {table}."user" = users.name'
        )
        # Anything that didn't match (typos, blanks) → fallback to Alex.
        op.execute(f"UPDATE {table} SET user_id = 1 WHERE user_id IS NULL")
    else:
        op.execute(f"UPDATE {table} SET user_id = 1 WHERE user_id IS NULL")

    # 3. Lock down: NOT NULL + index. Drop server_default unless caller
    #    wants it kept (whatsapp bridge case).
    op.alter_column(table, "user_id", nullable=False)
    if not keep_default:
        op.alter_column(table, "user_id", server_default=None)
    op.create_index(f"ix_{table}_user_id", table, ["user_id"])

    # 4. Drop the now-redundant string user column if present.
    if backfill_from_name:
        # Drop the matching index too. SQLAlchemy's index=True default name
        # is ix_<table>_user.
        op.drop_index(f"ix_{table}_user", table_name=table)
        op.drop_column(table, "user")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. users table + seed
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_users_name"),
    )
    op.create_index("ix_users_name", "users", ["name"])

    op.execute(
        "INSERT INTO users (id, name, display_name, is_active) VALUES "
        "(1, 'alex', 'Alex', true), "
        "(2, 'sam', 'Sam', true)"
    )
    # Bump the sequence past the seed so future inserts don't collide.
    op.execute("SELECT setval('users_id_seq', 2, true)")

    # ------------------------------------------------------------------
    # 2. Identity-adjacent tables (had free-text 'user' string column)
    # ------------------------------------------------------------------
    _add_user_id("client_tokens", backfill_from_name=True)
    _add_user_id("client_logs", backfill_from_name=True)

    # ------------------------------------------------------------------
    # 3. OAuth tokens — no user column existed; backfill all to Alex.
    # ------------------------------------------------------------------
    _add_user_id("oauth_tokens")
    op.create_unique_constraint(
        "uq_oauth_user_provider_account",
        "oauth_tokens",
        ["user_id", "provider", "account_email"],
    )

    # ------------------------------------------------------------------
    # 4. Reminders — also gains account_email; unique key swaps from bare
    #    `uid` to (user_id, uid).
    # ------------------------------------------------------------------
    _add_user_id("reminders")
    op.add_column(
        "reminders",
        sa.Column("account_email", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_reminders_account_email", "reminders", ["account_email"]
    )
    # Pre-existing uniqueness lived as either a `reminders_uid_key`
    # constraint OR a UNIQUE INDEX named `ix_reminders_uid` (depending on
    # whether the original mapped_column carried unique=True alongside
    # index=True). Drop both shapes if present, then recreate the lookup
    # index as non-unique so the new (user_id, uid) composite is the only
    # uniqueness gate.
    op.execute("ALTER TABLE reminders DROP CONSTRAINT IF EXISTS reminders_uid_key")
    op.execute("DROP INDEX IF EXISTS ix_reminders_uid")
    op.execute("CREATE INDEX IF NOT EXISTS ix_reminders_uid ON reminders (uid)")
    op.create_unique_constraint(
        "uq_reminders_user_uid", "reminders", ["user_id", "uid"]
    )
    op.create_index(
        "ix_reminders_user_due", "reminders", ["user_id", "due_date"]
    )

    _add_user_id("reminder_commands")

    # ------------------------------------------------------------------
    # 5. Mail messages
    # ------------------------------------------------------------------
    _add_user_id("mail_messages")
    op.execute(
        "ALTER TABLE mail_messages DROP CONSTRAINT IF EXISTS mail_messages_google_message_id_key"
    )
    op.execute("DROP INDEX IF EXISTS ix_mail_messages_google_message_id")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mail_messages_google_message_id ON mail_messages (google_message_id)"
    )
    op.create_unique_constraint(
        "uq_mail_user_msg",
        "mail_messages",
        ["user_id", "google_message_id"],
    )

    # ------------------------------------------------------------------
    # 6. Scrobbles
    # ------------------------------------------------------------------
    _add_user_id("scrobbles")
    op.execute("ALTER TABLE scrobbles DROP CONSTRAINT IF EXISTS uq_scrobble")
    op.create_unique_constraint(
        "uq_scrobble",
        "scrobbles",
        ["user_id", "track_name", "artist_name", "played_at"],
    )
    op.create_index(
        "ix_scrobbles_user_played_at",
        "scrobbles",
        ["user_id", "played_at"],
    )

    # ------------------------------------------------------------------
    # 7. WhatsApp messages — keep server_default for the Node bridge.
    # ------------------------------------------------------------------
    _add_user_id("whatsapp_messages", keep_default=True)
    op.execute(
        "ALTER TABLE whatsapp_messages DROP CONSTRAINT IF EXISTS whatsapp_messages_message_id_key"
    )
    op.execute("DROP INDEX IF EXISTS ix_whatsapp_messages_message_id")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_whatsapp_messages_message_id ON whatsapp_messages (message_id)"
    )
    op.create_unique_constraint(
        "uq_wa_user_msg",
        "whatsapp_messages",
        ["user_id", "message_id"],
    )

    # ------------------------------------------------------------------
    # 8. Coffee brews
    # ------------------------------------------------------------------
    _add_user_id("coffee_brews")
    op.drop_index("ix_coffee_brews_brewed_at", table_name="coffee_brews")
    op.create_index(
        "ix_coffee_brews_user_brewed_at",
        "coffee_brews",
        ["user_id", "brewed_at"],
    )

    # ------------------------------------------------------------------
    # 9. Message attachments
    # ------------------------------------------------------------------
    _add_user_id("message_attachments")
    op.execute(
        "ALTER TABLE message_attachments DROP CONSTRAINT IF EXISTS uq_msg_attachment"
    )
    op.create_unique_constraint(
        "uq_msg_attachment",
        "message_attachments",
        ["user_id", "source", "message_ref", "filename"],
    )

    # ------------------------------------------------------------------
    # 10. Health tables — had free-text 'user' string; back it from name.
    # ------------------------------------------------------------------
    # health_daily_metrics has uq_health_daily(user, date, metric_type).
    op.execute(
        "ALTER TABLE health_daily_metrics DROP CONSTRAINT IF EXISTS uq_health_daily"
    )
    _add_user_id("health_daily_metrics", backfill_from_name=True)
    op.create_unique_constraint(
        "uq_health_daily",
        "health_daily_metrics",
        ["user_id", "date", "metric_type"],
    )

    _add_user_id("health_workouts", backfill_from_name=True)
    # Same constraint-vs-unique-index ambiguity as reminders.
    op.execute(
        "ALTER TABLE health_workouts DROP CONSTRAINT IF EXISTS health_workouts_uid_key"
    )
    op.execute("DROP INDEX IF EXISTS ix_health_workouts_uid")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_health_workouts_uid ON health_workouts (uid)"
    )
    op.create_unique_constraint(
        "uq_health_workout_user_uid",
        "health_workouts",
        ["user_id", "uid"],
    )

    _add_user_id("health_sleep_sessions", backfill_from_name=True)
    op.execute(
        "ALTER TABLE health_sleep_sessions DROP CONSTRAINT IF EXISTS health_sleep_sessions_uid_key"
    )
    op.execute("DROP INDEX IF EXISTS ix_health_sleep_sessions_uid")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_health_sleep_sessions_uid ON health_sleep_sessions (uid)"
    )
    op.create_unique_constraint(
        "uq_health_sleep_user_uid",
        "health_sleep_sessions",
        ["user_id", "uid"],
    )


def downgrade() -> None:
    # Reverse-order teardown: re-add string `user` columns where needed,
    # restore old unique keys, drop user_id everywhere, drop users table.

    # Health workouts + sleep — explicit per-table constraint names.
    for table, new_uq in (
        ("health_sleep_sessions", "uq_health_sleep_user_uid"),
        ("health_workouts", "uq_health_workout_user_uid"),
    ):
        op.drop_constraint(new_uq, table, type_="unique")
        op.create_unique_constraint(f"{table}_uid_key", table, ["uid"])
        op.add_column(
            table,
            sa.Column(
                "user", sa.String(100), nullable=True, server_default="alex"
            ),
        )
        op.execute(
            f'UPDATE {table} SET "user" = users.name '
            f'FROM users WHERE {table}.user_id = users.id'
        )
        op.alter_column(table, "user", nullable=False, server_default=None)
        op.create_index(f"ix_{table}_user", table, ["user"])
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_column(table, "user_id")

    op.drop_constraint(
        "uq_health_daily", "health_daily_metrics", type_="unique"
    )
    op.add_column(
        "health_daily_metrics",
        sa.Column("user", sa.String(100), nullable=True, server_default="alex"),
    )
    op.execute(
        'UPDATE health_daily_metrics SET "user" = users.name '
        'FROM users WHERE health_daily_metrics.user_id = users.id'
    )
    op.alter_column(
        "health_daily_metrics", "user", nullable=False, server_default=None
    )
    op.create_index(
        "ix_health_daily_metrics_user", "health_daily_metrics", ["user"]
    )
    op.drop_index(
        "ix_health_daily_metrics_user_id", table_name="health_daily_metrics"
    )
    op.drop_column("health_daily_metrics", "user_id")
    op.create_unique_constraint(
        "uq_health_daily",
        "health_daily_metrics",
        ["user", "date", "metric_type"],
    )

    # Message attachments
    op.drop_constraint(
        "uq_msg_attachment", "message_attachments", type_="unique"
    )
    op.create_unique_constraint(
        "uq_msg_attachment",
        "message_attachments",
        ["source", "message_ref", "filename"],
    )
    op.drop_index(
        "ix_message_attachments_user_id", table_name="message_attachments"
    )
    op.drop_column("message_attachments", "user_id")

    # Coffee brews
    op.drop_index(
        "ix_coffee_brews_user_brewed_at", table_name="coffee_brews"
    )
    op.create_index(
        "ix_coffee_brews_brewed_at", "coffee_brews", ["brewed_at"]
    )
    op.drop_index("ix_coffee_brews_user_id", table_name="coffee_brews")
    op.drop_column("coffee_brews", "user_id")

    # WhatsApp
    op.drop_constraint(
        "uq_wa_user_msg", "whatsapp_messages", type_="unique"
    )
    op.create_unique_constraint(
        "whatsapp_messages_message_id_key",
        "whatsapp_messages",
        ["message_id"],
    )
    op.drop_index(
        "ix_whatsapp_messages_user_id", table_name="whatsapp_messages"
    )
    op.drop_column("whatsapp_messages", "user_id")

    # Scrobbles
    op.drop_index("ix_scrobbles_user_played_at", table_name="scrobbles")
    op.drop_constraint("uq_scrobble", "scrobbles", type_="unique")
    op.create_unique_constraint(
        "uq_scrobble", "scrobbles", ["track_name", "artist_name", "played_at"]
    )
    op.drop_index("ix_scrobbles_user_id", table_name="scrobbles")
    op.drop_column("scrobbles", "user_id")

    # Mail
    op.drop_constraint("uq_mail_user_msg", "mail_messages", type_="unique")
    op.create_unique_constraint(
        "mail_messages_google_message_id_key",
        "mail_messages",
        ["google_message_id"],
    )
    op.drop_index("ix_mail_messages_user_id", table_name="mail_messages")
    op.drop_column("mail_messages", "user_id")

    # Reminders
    op.drop_index("ix_reminder_commands_user_id", table_name="reminder_commands")
    op.drop_column("reminder_commands", "user_id")

    op.drop_index("ix_reminders_user_due", table_name="reminders")
    op.drop_constraint("uq_reminders_user_uid", "reminders", type_="unique")
    op.create_unique_constraint("reminders_uid_key", "reminders", ["uid"])
    op.drop_index("ix_reminders_account_email", table_name="reminders")
    op.drop_column("reminders", "account_email")
    op.drop_index("ix_reminders_user_id", table_name="reminders")
    op.drop_column("reminders", "user_id")

    # OAuth
    op.drop_constraint(
        "uq_oauth_user_provider_account", "oauth_tokens", type_="unique"
    )
    op.drop_index("ix_oauth_tokens_user_id", table_name="oauth_tokens")
    op.drop_column("oauth_tokens", "user_id")

    # Client logs + tokens — restore string user columns
    for table in ("client_logs", "client_tokens"):
        op.add_column(
            table,
            sa.Column("user", sa.String(50), nullable=True, server_default="alex"),
        )
        op.execute(
            f'UPDATE {table} SET "user" = users.name '
            f'FROM users WHERE {table}.user_id = users.id'
        )
        op.alter_column(table, "user", nullable=False, server_default=None)
        op.create_index(f"ix_{table}_user", table, ["user"])
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_column(table, "user_id")

    # Drop the users table last.
    op.drop_index("ix_users_name", table_name="users")
    op.drop_table("users")
