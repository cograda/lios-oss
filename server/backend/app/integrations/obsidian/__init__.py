"""Obsidian vault integration — semantic search via pgvector + fastembed.

Indexes markdown files from the vault (synced via rclone to /obsidian in Docker).
Provides MCP tools for semantic search, recent files, and index stats.

`SourceIntegration` conversion (V4 chunk 4.3, batch C). `tools.py` was
already DSL-native. The "external system" here is per-user local vault
directories, not a remote fetch — same shape as batch A's inbox/media:
`accounts()` resolves the active users who actually have a vault directory
on disk (identical filter/logging to the old hand-rolled `sync()` loop —
a missing vault is normal for a not-yet-onboarded user and is skipped, not
raised), `pull()` is a no-op passthrough (nothing to fetch without writing —
`index_vault` itself is scan+enqueue in one step), and `store()` runs
`index_vault` inside the same `use_user()` context the old loop used, so
`user_id` threading through the embedding enqueue (sam-rollout A1,
commit f36a3ba) is unchanged.
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.auth.context import use_user
from app.config import settings
from app.db import get_db
from app.integrations.obsidian.sync import index_vault
from app.integrations.obsidian.tools import get_mcp_tools
from app.models.users import User
from app.plugin.bases import PullResult, SourceIntegration
from app.services import vault_paths

logger = logging.getLogger(__name__)


class ObsidianIntegration(SourceIntegration):
    @property
    def name(self) -> str:
        return "obsidian"

    @property
    def display_name(self) -> str:
        return "Obsidian Vault"

    def accounts(self, session: Session) -> list[User]:
        """Active users who have a vault directory on disk — a missing
        vault is normal for a user who hasn't onboarded yet, logged and
        skipped rather than raised (matches the pre-conversion behavior)."""
        users = session.query(User).filter_by(is_active=True).order_by(User.id).all()
        result = []
        for user in users:
            vault_dir = vault_paths.user_vault_path(user.name)
            if not vault_dir.is_dir():
                logger.info(f"Vault index: no vault for {user.name}, skipping")
                continue
            result.append(user)
        return result

    def account_user_id(self, account: User) -> int | None:
        return account.id

    def account_label(self, account: User) -> str:
        return account.name

    def pull(self, account: User, session: Session, cursor: str | None) -> PullResult:
        """No outbound I/O to do without writing — `index_vault` (called from
        `store()`) is scan-and-enqueue in a single step for this local
        filesystem source, same reasoning as inbox/media in batch A."""
        return PullResult(records=[account])

    def store(self, session: Session, records: list[User]) -> int:
        total = 0
        for user in records:
            vault_dir = vault_paths.user_vault_path(user.name)
            with use_user(user.id):
                result = index_vault(session, str(vault_dir), user.id)
            logger.info(f"Vault index [{user.name}]: {result}")
            total += result.get("enqueued", 0) if isinstance(result, dict) else 0
        return total

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return vault index stats for dashboard.

        Deliberately unscoped — the dashboard is the household admin view
        (same convention as the other integrations' dashboard_data). Adds a
        per-user file count so a stalled vault is visible at a glance.
        """
        db = get_db()
        with db.session() as session:
            from sqlalchemy import func
            from app.integrations.obsidian.models import VaultChunk

            total = session.query(func.count(VaultChunk.id)).scalar() or 0
            latest = session.query(func.max(VaultChunk.indexed_at)).scalar()
            by_user = dict(
                session.query(User.name, func.count(VaultChunk.id))
                .join(VaultChunk, VaultChunk.user_id == User.id)
                .group_by(User.name)
                .all()
            )
            return {
                "total_files": total,
                "by_user": by_user,
                "last_indexed": latest.isoformat() if latest else None,
            }

    def is_configured(self) -> bool:
        # The per-user tree is what sync() walks; the legacy single-vault
        # bind mount still satisfies this for a not-yet-migrated deployment.
        return (
            Path(settings.vaults_root_path).is_dir()
            or Path(settings.obsidian_vault_path).is_dir()
        )
