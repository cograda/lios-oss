"""Home Assistant integration — cached home status + state history."""

import logging
from typing import Any

from app.config import settings
from app.db import get_db
from app.integrations.base import BaseIntegration
from app.integrations.homeassistant.models import HAEntity
from app.integrations.homeassistant.sync import sync_home_assistant
from app.integrations.homeassistant.tools import get_mcp_tools

logger = logging.getLogger(__name__)


class HomeAssistantIntegration(BaseIntegration):
    @property
    def name(self) -> str:
        return "homeassistant"

    @property
    def display_name(self) -> str:
        return "Home Assistant"

    def sync(self) -> None:
        """Fetch all entity states from HA and upsert into Postgres."""
        db = get_db()
        with db.session() as session:
            sync_home_assistant(session)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Headline counts for the web dashboard."""
        db = get_db()
        with db.session() as session:
            entities = session.query(HAEntity).all()
            if not entities:
                return {"status": "no_data"}

            offline = [
                e for e in entities if (e.state or "") in ("unavailable", "unknown")
            ]
            lights_on = [
                e
                for e in entities
                if e.domain in ("light", "switch") and e.state == "on"
            ]
            media_playing = [
                e
                for e in entities
                if e.domain == "media_player"
                and (e.state or "") not in ("off", "idle", "standby", "unavailable", "unknown")
            ]
            latest = max(
                (e.synced_at for e in entities if e.synced_at), default=None
            )

            from app.integrations.homeassistant import events as ha_events
            listener = ha_events.current_listener

            return {
                "entity_count": len(entities),
                "offline_count": len(offline),
                "lights_on": len(lights_on),
                "media_playing": len(media_playing),
                "event_stream_connected": bool(listener and listener.connected),
                "synced_at": latest.isoformat() if latest else None,
            }

    def sync_schedule(self) -> str | None:
        return "*/5 * * * *"

    def is_configured(self) -> bool:
        return bool(settings.ha_url and settings.ha_token)
