"""Embedding — cross-source semantic search capability.

Not a producer of embeddable content itself (`manifest.embedding_sources` is
empty) — every integration that owns a source (vault, email, whatsapp,
historical_corpus, coffee) calls into `app.services.embedding.EmbeddingService`
to enqueue/search/delete. This package owns the shared `Embedding`/
`EmbeddingQueue` tables (`models.py`), the queue-processor cron job
(`tasks.py`, wired via the manifest's `background_tasks`), and the two
cross-source tools `search_semantic`/`search_stats` (`tools.py`) that used to
be registered directly inside `app/mcp/server.py` under a synthetic
"embedding" integration name (V4 chunk 3.4).
"""

from typing import Any

from app.integrations.base import BaseIntegration
from app.integrations.embedding.tools import get_mcp_tools


class EmbeddingIntegration(BaseIntegration):
    """Cross-source semantic search: queue, worker, pgvector search."""

    @property
    def name(self) -> str:
        return "embedding"

    @property
    def display_name(self) -> str:
        return "Embedding"

    def sync(self) -> None:
        pass  # No external sync — the queue processor background task does the work.

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from app.db import get_db
        from app.services.embedding import EmbeddingService

        db = get_db()
        with db.session() as session:
            return EmbeddingService.stats(session)

    # is_configured(): default (empty config_schema -> vacuously True) — the
    # embedding pipeline is always available, same as `system`.
