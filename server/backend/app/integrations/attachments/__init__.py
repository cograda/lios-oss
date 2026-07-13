"""Message attachments integration.

Extracts file attachments from live Gmail + WhatsApp messages, on-demand.

The flow is always user-gated:
  1. `scan` — populates `message_attachments` rows from `raw_json`.
     WhatsApp only: Gmail attachment scanning is not implemented (see
     `scan.py` for why — a full-format Gmail backfill is a deferred feature).
  2. `pending` lists unprocessed attachments with name/type/size/sender/date
  3. `ingest` downloads, parses via the corpus parsers, and embeds into
     `historical_documents` with source_type='wa_attachment'

Ingested attachments show up in `renovation_context` automatically because they
share the historical-corpus embedding source.
"""

from typing import Any

from app.integrations.base import BaseIntegration
from app.integrations.attachments import tools as attachment_tools


class AttachmentsIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "attachments"

    @property
    def display_name(self) -> str:
        return "Message Attachments"

    def sync(self) -> None:
        """Lightweight scan of WhatsApp messages → populates metadata rows.

        No downloads or parsing happen here — that's user-gated via the
        attachments_ingest tool. Gmail is not scanned; see scan.py for why.
        """
        from app.db import get_db
        from app.integrations.attachments.scan import scan_whatsapp
        db = get_db()
        with db.session() as session:
            scan_whatsapp(session)

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

    def sync_schedule(self) -> str | None:
        # Cheap metadata-only scan; piggy-backs on WhatsApp's 30-min sync cadence
        return "*/30 * * * *"

    def is_configured(self) -> bool:
        return True
