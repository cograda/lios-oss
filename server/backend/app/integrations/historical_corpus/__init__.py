"""Historical corpus integration — one-shot backfill of static renovation archive.

Unlike live integrations, there's no automatic sync schedule. Ingestion is
triggered manually via the CLI (scripts/ingest_historical_corpus.py) or a REST
endpoint (/api/integrations/historical_corpus/ingest). The MCP tools query the
resulting embeddings via the unified pipeline.
"""

from typing import Any

from app.integrations.base import BaseIntegration
from app.integrations.historical_corpus import tools as corpus_tools


class HistoricalCorpusIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "historical_corpus"

    @property
    def display_name(self) -> str:
        return "Historical Corpus"

    def sync(self) -> None:
        """No-op — corpus is static, ingested manually via CLI."""

    def mcp_tools(self) -> list[dict[str, Any]]:
        return corpus_tools.mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from app.db import get_db
        from app.integrations.historical_corpus.models import (
            HistoricalDocument, HistoricalDocumentChunk,
        )
        from sqlalchemy import func as sa_func

        db = get_db()
        with db.session() as session:
            docs = session.query(sa_func.count(HistoricalDocument.id)).scalar() or 0
            chunks = session.query(sa_func.count(HistoricalDocumentChunk.id)).scalar() or 0
        return {"documents": docs, "chunks": chunks}

    def sync_schedule(self) -> str | None:
        return None

    def is_configured(self) -> bool:
        return True
