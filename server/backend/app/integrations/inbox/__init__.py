"""Inbox triage integration.

The automation webhook at /api/inbox/ingest drops files into /inbox/<bucket>/.
This integration is the *triage* layer on top of that drop zone:

  - The hourly worker (`scan.enrich_pending`) sniffs file kind and extracts
    a short preview into the sidecar JSON. Pure enrichment, no routing.
  - MCP tools (`inbox_pending`, `inbox_preview`, `inbox_archive`,
    `inbox_dismiss`, `inbox_to_vault`, `inbox_to_corpus`) let skills surface
    the queue to the user and route each item interactively.

State lives on disk, not in Postgres (same model as csv-inbox/archive):
  /inbox/<bucket>/       — pending (incoming, text, image, audio, file)
  /inbox/archive/        — handled (moved here when routed)
  /inbox/dismissed/      — explicitly ignored

V1 supports PDF + plaintext/markdown enrichment. Images/audio land but get
only size/dimension previews — no OCR or transcription yet.
"""

from typing import Any

from app.integrations.base import BaseIntegration
from app.integrations.inbox.scan import enrich_pending
from app.integrations.inbox.tools import get_mcp_tools


class InboxIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "inbox"

    @property
    def display_name(self) -> str:
        return "Inbox"

    def sync(self) -> None:
        # Enrichment runs synchronously — fine inside the scheduler's
        # asyncio.to_thread wrapper. Cheap (≤a few hundred files, mostly
        # text reads + first-page PDF extract).
        from app.db import get_db
        with get_db().session() as session:
            enrich_pending(session)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from app.integrations.inbox.scan import count_pending
        return {"pending": count_pending()}

    def sync_schedule(self) -> str | None:
        # Every hour at :07 — offset from other integrations so we don't all
        # wake at :00 and contend on the same DB/file resources.
        return "7 * * * *"

    def is_configured(self) -> bool:
        return True
