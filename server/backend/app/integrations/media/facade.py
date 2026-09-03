"""media's declared facade — capability `media.store` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.media`. One consumer: `snags`, which attaches/renders
WhatsApp media items as snag evidence — it needs the ORM class itself (for
its own queries/joins against `snag_media`), plus `download_item` to
materialize an item that hasn't been fetched yet before copying it into the
vault as evidence.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.integrations.media import store as _store
from app.integrations.media.models import MediaItem


class MediaFacade:
    #: ORM class, re-exported for callers with a declared dependency on the
    #: media store that need to query/join against `media_items` directly
    #: (e.g. snags' evidence attach/render flows).
    Item = MediaItem

    def download_item(self, session: Session, item: MediaItem) -> bool:
        # `_store.download_item`, not a direct `from store import
        # download_item` — module-attribute access so tests that
        # monkeypatch `app.integrations.media.store.download_item` (the
        # normal pattern here) actually take effect at call time.
        return _store.download_item(session, item)


FACADE = MediaFacade()
