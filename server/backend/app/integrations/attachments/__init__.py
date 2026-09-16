"""Message attachments integration.

Extracts file attachments from live Gmail + WhatsApp messages, on-demand.

The flow is always user-gated:
  1. `scan` — populates `message_attachments` rows. WhatsApp reads
     `raw_json` already in the DB (zero API calls). Gmail does a bounded,
     checkpointed full-format backfill against the live API (see `scan.py`
     for the catch-up/backfill split) since the routine metadata-only mail
     sync strips `payload.parts`.
  2. `pending` lists unprocessed attachments with name/type/size/sender/date
  3. `ingest` downloads, parses via the corpus parsers, and embeds into
     `historical_documents` with source_type='wa_attachment_*' (WhatsApp,
     bytes from the Baileys bridge) or 'gmail_attachment_*' (Gmail, bytes
     from `users.messages.attachments.get` with the owner's token — wired
     2026-09-07).

Ingested attachments show up in `corpus_search` automatically because they
share the historical-corpus embedding source.

`SourceIntegration` conversion (V4 chunk 4.3, batch D): the scheduled sync
(every 30 min) does real work — a lightweight WhatsApp metadata scan — so
this is a pull source, not a pure `CapabilityService`, even though the
manifest's `type` field still says `"capability"` (left as-is; nothing
enforces `type` against the base class, same non-issue noted for
irish_rail in batch A). There's no remote fetch to separate from
persistence (the "external system" is already-cached WhatsApp rows in our
own DB), so `pull()` is a no-op and `store()` does the actual scan — same
shape as `inbox`/`media`/`obsidian` in earlier batches. Gmail scanning
stays tool-gated only (`attachments_scan`), never part of the scheduled
sync — unchanged from before this conversion.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.attachments import tools as attachment_tools
from app.plugin.bases import PullResult, SourceIntegration


class AttachmentsIntegration(SourceIntegration):
    @property
    def name(self) -> str:
        return "attachments"

    @property
    def display_name(self) -> str:
        return "Message Attachments"

    def pull(self, account: Any, session: Session, cursor: str | None) -> PullResult:
        # Nothing to fetch remotely — the scan reads already-cached
        # WhatsApp rows from our own DB. The actual work happens in store().
        return PullResult(records=[])

    def store(self, session: Session, records: list[Any]) -> int:
        from app.integrations.attachments.scan import scan_whatsapp
        # Deliberately unscoped: this is the scheduled sync, which runs with no
        # bound user and attributes each row to its owner via w.user_id.
        result = scan_whatsapp(session)
        return result["new_pending"]

    def mcp_tools(self) -> list[dict[str, Any]]:
        return attachment_tools.mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from app.db import get_db
        from app.integrations.attachments.models import MessageAttachment
        from sqlalchemy import func as sa_func
        db = get_db()
        with db.session() as session:
            by_status = dict(
                session.query(
                    MessageAttachment.parse_status,
                    sa_func.count(MessageAttachment.id),
                ).group_by(MessageAttachment.parse_status).all()
            )
        return {"by_status": by_status}

    # is_configured(): default (empty config_schema -> vacuously True).
