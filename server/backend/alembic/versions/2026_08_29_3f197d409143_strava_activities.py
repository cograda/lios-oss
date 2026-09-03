"""strava_activities — Strava activity archive.

Per-user (`UserOwnedMixin`): Strava is a personal account, and each athlete
authorises their own grant. Uniqueness is composite on (user_id, strava_id)
rather than on strava_id alone — see app/integrations/strava/models.py.

Units are stored as Strava returns them (metres, seconds, m/s); conversion
happens in tools.py on read.

Revision ID: 3f197d409143
Revises: bd78141d2722

⚠️ Re-parented onto main's head before merge. It was first written against
c3d4e5f6a7b8, the head of the branch it was authored on — which main had
since moved past (ai_usage_ledger, then notification_sends_suppressed_reason).
Merging it unchanged would have given alembic TWO heads and failed the
container at startup, not at review. Same trap as the gmail-attachments
re-parent on 2026-08-28. Always check `alembic heads` against ORIGIN/main,
not the branch you happen to be on.
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3f197d409143"
down_revision: Union[str, Sequence[str], None] = "bd78141d2722"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strava_activities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        # BigInteger is required, not defensive: Strava activity ids passed
        # 2^31 years ago and an Integer column would raise on every insert.
        sa.Column("strava_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("sport_type", sa.String(length=50), nullable=True),
        sa.Column("activity_type", sa.String(length=50), nullable=True),
        sa.Column("start_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone_name", sa.String(length=80), nullable=True),
        sa.Column("utc_offset_seconds", sa.Integer(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("moving_time_s", sa.Integer(), nullable=True),
        sa.Column("elapsed_time_s", sa.Integer(), nullable=True),
        sa.Column("total_elevation_gain_m", sa.Float(), nullable=True),
        sa.Column("average_speed_ms", sa.Float(), nullable=True),
        sa.Column("max_speed_ms", sa.Float(), nullable=True),
        sa.Column("average_cadence", sa.Float(), nullable=True),
        sa.Column("average_heartrate", sa.Float(), nullable=True),
        sa.Column("max_heartrate", sa.Float(), nullable=True),
        sa.Column("average_watts", sa.Float(), nullable=True),
        sa.Column("weighted_average_watts", sa.Float(), nullable=True),
        sa.Column("max_watts", sa.Float(), nullable=True),
        sa.Column("device_watts", sa.Boolean(), nullable=True),
        sa.Column("kilojoules", sa.Float(), nullable=True),
        sa.Column("suffer_score", sa.Float(), nullable=True),
        sa.Column("trainer", sa.Boolean(), nullable=True),
        sa.Column("commute", sa.Boolean(), nullable=True),
        sa.Column("manual", sa.Boolean(), nullable=True),
        sa.Column("private", sa.Boolean(), nullable=True),
        sa.Column("gear_id", sa.String(length=50), nullable=True),
        sa.Column("device_name", sa.String(length=120), nullable=True),
        sa.Column("kudos_count", sa.Integer(), nullable=True),
        sa.Column("achievement_count", sa.Integer(), nullable=True),
        sa.Column("pr_count", sa.Integer(), nullable=True),
        sa.Column("start_lat", sa.Float(), nullable=True),
        sa.Column("start_lng", sa.Float(), nullable=True),
        sa.Column("end_lat", sa.Float(), nullable=True),
        sa.Column("end_lng", sa.Float(), nullable=True),
        sa.Column("map_polyline", sa.Text(), nullable=True),
        # SourcedRecordMixin
        sa.Column("source_id", sa.String(length=500), nullable=True),
        sa.Column("source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("user_id", "strava_id", name="uq_strava_activity_user_id"),
    )
    op.create_index(
        "ix_strava_activities_user_id", "strava_activities", ["user_id"]
    )
    op.create_index(
        "ix_strava_activities_strava_id", "strava_activities", ["strava_id"]
    )
    op.create_index(
        "ix_strava_activities_start_date", "strava_activities", ["start_date"]
    )
    op.create_index(
        "ix_strava_activities_sport_type", "strava_activities", ["sport_type"]
    )
    op.create_index(
        "ix_strava_activities_activity_type", "strava_activities", ["activity_type"]
    )
    op.create_index(
        "ix_strava_activities_source_id", "strava_activities", ["source_id"]
    )
    op.create_index(
        "ix_strava_activities_source_ts", "strava_activities", ["source_ts"]
    )
    # The query every tool actually runs: this user's activities, newest
    # first. Kept alongside the single-column start_date index because the
    # composite serves ordered per-user scans that the single column cannot.
    op.create_index(
        "ix_strava_activities_user_start",
        "strava_activities",
        ["user_id", "start_date"],
    )


def downgrade() -> None:
    # IF EXISTS throughout: a downgrade run against a database where an
    # earlier partial upgrade left some objects behind must not itself fail.
    for index in (
        "ix_strava_activities_user_start",
        "ix_strava_activities_source_ts",
        "ix_strava_activities_source_id",
        "ix_strava_activities_activity_type",
        "ix_strava_activities_sport_type",
        "ix_strava_activities_start_date",
        "ix_strava_activities_strava_id",
        "ix_strava_activities_user_id",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.execute("DROP TABLE IF EXISTS strava_activities")
