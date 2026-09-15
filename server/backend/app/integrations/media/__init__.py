"""WhatsApp media store integration.

Indexes ALL WhatsApp images/videos/audio (metadata rows in media_items),
auto-downloads the recent window (~30 days) into a persistent on-disk store
(HOME_MEDIA_ROOT, volume-mounted), and exposes tools to list, force-fetch,
and export items into the vault for note-embedding (snag evidence, receipts).

Distinct from the attachments integration on purpose: attachments is the
parse-and-embed pipeline for documents (PDF/DOCX/XLSX → historical corpus);
this is the binary store for everything with pixels or audio in it.

`SourceIntegration` conversion (V4 chunk 4.3, batch A): scanning and
downloading are both local/idempotent DB operations rather than a clean
fetch-without-writing step, so `pull()` is a no-op and `store()` runs the
same scan-then-download sequence the old hand-rolled `sync()` did. No
multi-account concept — `accounts()` stays at the `SourceIntegration`
default.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.media import tools as media_tools
from app.plugin.bases import PullResult, SourceIntegration


class MediaIntegration(SourceIntegration):
    @property
    def name(self) -> str:
        return "media"

    @property
    def display_name(self) -> str:
        return "Media Store"

    def pull(self, account: Any, session: Session, cursor: str | None) -> PullResult:
        # Nothing to fetch without writing — scanning inserts index rows and
        # downloading writes files + updates rows. Both happen in `store()`.
        return PullResult(records=[])

    def store(self, session: Session, records: list[Any]) -> int:
        """Scan for new media rows + download the recent window (batched)."""
        from app.integrations.media.scan import scan_whatsapp_media
        from app.integrations.media.store import download_pending

        # Deliberately unscoped: the scheduled sync runs with no bound user and
        # attributes each row to its owner via w.user_id / MediaItem.user_id.
        scan_result = scan_whatsapp_media(session)
        download_result = download_pending(session)
        return scan_result["new_indexed"] + download_result["stored"]

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

    # is_configured(): default (empty config_schema -> vacuously True).
