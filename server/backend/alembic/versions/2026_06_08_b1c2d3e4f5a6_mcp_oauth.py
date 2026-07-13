"""mcp_oauth: OAuth 2.1 authorization-server tables for MCP connector sign-in

Lets claude.ai's custom-connector flow (discovery → DCR → auth-code → PKCE)
authenticate against Comar's MCP. Four tables back the provider in
app/auth/oauth_provider.py:

  oauth_clients              — DCR-registered clients (global, not per-user)
  oauth_login_sessions       — transient parked /authorize requests (the funnel)
  oauth_authorization_codes  — single-use codes, user-scoped
  mcp_access_tokens          — issued access + refresh tokens, user-scoped

See vault Plans/mcp-oauth.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("client_id"),
    )

    op.create_table(
        "oauth_login_sessions",
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )

    op.create_table(
        "oauth_authorization_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_oauth_authorization_codes_code"),
    )
    op.create_index(
        "ix_oauth_authorization_codes_code", "oauth_authorization_codes", ["code"], unique=False
    )
    op.create_index(
        "ix_oauth_authorization_codes_client_id", "oauth_authorization_codes", ["client_id"], unique=False
    )
    op.create_index(
        "ix_oauth_authorization_codes_user_id", "oauth_authorization_codes", ["user_id"], unique=False
    )

    op.create_table(
        "mcp_access_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("access_token", sa.String(128), nullable=False),
        sa.Column("refresh_token", sa.String(128), nullable=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_token", name="uq_mcp_access_tokens_access_token"),
        sa.UniqueConstraint("refresh_token", name="uq_mcp_access_tokens_refresh_token"),
    )
    op.create_index(
        "ix_mcp_access_tokens_access_token", "mcp_access_tokens", ["access_token"], unique=False
    )
    op.create_index(
        "ix_mcp_access_tokens_refresh_token", "mcp_access_tokens", ["refresh_token"], unique=False
    )
    op.create_index(
        "ix_mcp_access_tokens_client_id", "mcp_access_tokens", ["client_id"], unique=False
    )
    op.create_index(
        "ix_mcp_access_tokens_user_id", "mcp_access_tokens", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_access_tokens_user_id", table_name="mcp_access_tokens")
    op.drop_index("ix_mcp_access_tokens_client_id", table_name="mcp_access_tokens")
    op.drop_index("ix_mcp_access_tokens_refresh_token", table_name="mcp_access_tokens")
    op.drop_index("ix_mcp_access_tokens_access_token", table_name="mcp_access_tokens")
    op.drop_table("mcp_access_tokens")

    op.drop_index("ix_oauth_authorization_codes_user_id", table_name="oauth_authorization_codes")
    op.drop_index("ix_oauth_authorization_codes_client_id", table_name="oauth_authorization_codes")
    op.drop_index("ix_oauth_authorization_codes_code", table_name="oauth_authorization_codes")
    op.drop_table("oauth_authorization_codes")

    op.drop_table("oauth_login_sessions")
    op.drop_table("oauth_clients")
