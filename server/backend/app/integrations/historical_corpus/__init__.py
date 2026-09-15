"""Historical corpus integration — one-shot backfill of static renovation archive.

Unlike live integrations, there's no automatic sync schedule. Ingestion is
triggered manually via the CLI (scripts/ingest_historical_corpus.py) or a REST
endpoint (/api/integrations/historical_corpus/ingest). The MCP tools query the
resulting embeddings via the unified pipeline.
"""

from typing import Any

from app.integrations.historical_corpus import tools as corpus_tools
from app.plugin.bases import CapabilityService


class HistoricalCorpusIntegration(CapabilityService):
    """No external system polled on a schedule — the corpus is static and
    ingested manually via CLI (scripts/ingest_historical_corpus.py,
    scripts/ingest_claude_export.py) or the admin
    `/integrations/historical_corpus/ingest` route. `sync()` is fully
    inherited (no-op) from CapabilityService, matching the manifest's own
    `schedule=None`; this package is a pure tool surface over its own
    tables, same shape as `coffee`/`snags`."""

    @property
    def name(self) -> str:
        return "historical_corpus"

    @property
    def display_name(self) -> str:
        return "Historical Corpus"

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

    # is_configured(): default (empty config_schema -> vacuously True).
