"""Obsidian vault integration — semantic search via pgvector + fastembed.

Indexes markdown files from the vault (synced via rclone to /obsidian in Docker).
Provides MCP tools for semantic search, recent files, and index stats.
"""

import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import get_db
from app.integrations.base import BaseIntegration
from app.integrations.obsidian.sync import index_vault
from app.integrations.obsidian.tools import get_mcp_tools

logger = logging.getLogger(__name__)


class ObsidianIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "obsidian"

    @property
    def display_name(self) -> str:
        return "Obsidian Vault"

    def sync(self) -> None:
        """Re-index the vault (incremental — only changed files are re-embedded)."""
        db = get_db()
        with db.session() as session:
            result = index_vault(session, settings.obsidian_vault_path)
            logger.info(f"Vault index: {result}")

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return vault index stats for dashboard."""
        db = get_db()
        with db.session() as session:
            from sqlalchemy import func
            from app.integrations.obsidian.models import VaultChunk

            total = session.query(func.count(VaultChunk.id)).scalar() or 0
            latest = session.query(func.max(VaultChunk.indexed_at)).scalar()
            return {
                "total_files": total,
                "last_indexed": latest.isoformat() if latest else None,
            }

    def sync_schedule(self) -> str | None:
        return "*/30 * * * *"  # Every 30 min (matches rclone sync)

    def is_configured(self) -> bool:
        return Path(settings.obsidian_vault_path).is_dir()
