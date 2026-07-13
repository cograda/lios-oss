"""WhatsApp media store integration.

Indexes ALL WhatsApp images/videos/audio (metadata rows in media_items),
auto-downloads the recent window (~30 days) into a persistent on-disk store
(HOME_MEDIA_ROOT, volume-mounted), and exposes tools to list, force-fetch,
and export items into the vault for note-embedding (snag evidence, receipts).

Distinct from the attachments integration on purpose: attachments is the
parse-and-embed pipeline for documents (PDF/DOCX/XLSX → historical corpus);
this is the binary store for everything with pixels or audio in it.
"""

from typing import Any

from app.integrations.base import BaseIntegration
from app.integrations.media import tools as media_tools


class MediaIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "media"

    @property
    def display_name(self) -> str:
        return "Media Store"

    def sync(self) -> None:
        """Scan for new media rows + download the recent window (batched)."""
        from app.db import get_db
        from app.integrations.media.scan import scan_whatsapp_media
        from app.integrations.media.store import download_pending

        db = get_db()
        with db.session() as session:
            scan_whatsapp_media(session)
            download_pending(session)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return media_tools.mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from sqlalchemy import func as sa_func
        from app.db import get_db
        from app.integrations.media.models import MediaItem

        db = get_db()
        with db.session() as session:
            by_status = dict(
                session.query(MediaItem.status, sa_func.count(MediaItem.id))
                .group_by(MediaItem.status)
                .all()
            )
            stored_bytes = session.query(
                sa_func.coalesce(sa_func.sum(MediaItem.size_bytes), 0)
            ).filter(MediaItem.status == "stored").scalar()
        return {"by_status": by_status, "stored_bytes": int(stored_bytes or 0)}

    def sync_schedule(self) -> str | None:
        # Offset from WhatsApp's */30 sync so freshly-synced messages are
        # already in whatsapp_messages when the media scan runs.
        return "5,35 * * * *"

    def is_configured(self) -> bool:
        return True
