"""Home Assistant integration — cached home status + state history.

`SourceIntegration` conversion (V4 chunk 4.3, batch D). `sync_home_assistant()`
fetches all states AND upserts them in one pass — its history-diffing logic
(should_record_transition) needs the existing row to compare against while
iterating the fetch, so there's no clean fetch-without-writing step to
extract into `pull()` the way google_calendar's OAuth-scoped fan-out has.
`tests/test_homeassistant.py::test_sync_home_assistant` calls
`sync_home_assistant()` directly and pins its combined behaviour, so that
function is left untouched — `pull()` is a no-op passthrough and `store()`
runs the same `sync_home_assistant()` call the old `sync()` made (same
shape as `inbox`/`media`/`attachments` earlier in this chunk, for the same
reason: nothing meaningful to fetch separately from persistence). The WS
listener (`events.py::run_listener_task`) is already a declared
`background_tasks` startup task since chunk 3.1 — no change needed here.
"""

import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.homeassistant.models import HAEntity
from app.integrations.homeassistant.sync import sync_home_assistant
from app.integrations.homeassistant.tools import get_mcp_tools
from app.plugin.bases import PullResult, SourceIntegration

logger = logging.getLogger(__name__)


class HomeAssistantIntegration(SourceIntegration):
    @property
    def name(self) -> str:
        return "homeassistant"

    @property
    def display_name(self) -> str:
        return "Home Assistant"

    def pull(self, account: Any, session: Session, cursor: str | None) -> PullResult:
        # Nothing to fetch-without-writing — sync_home_assistant() needs the
        # existing rows in hand while it iterates the fetched states to do
        # its history-diffing. The actual work happens in store().
        return PullResult(records=[])

    def store(self, session: Session, records: list[Any]) -> int:
        sync_home_assistant(session)
        return session.query(HAEntity).count()

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Headline counts for the web dashboard.

        P8 (hardening-2026-08.md): this used to `session.query(HAEntity).all()`
        — materialising every column (including the JSONB `attributes` blob)
        of every entity, on every dashboard render, only to reduce it to five
        counts. Every count below is now a `COUNT(*)`/`MAX()` aggregate done
        in Postgres; nothing but the aggregates crosses the wire.
        """
        db = get_db()
        with db.session() as session:
            entity_count = session.query(func.count(HAEntity.id)).scalar() or 0
            if entity_count == 0:
                return {"status": "no_data"}

            offline_count = (
                session.query(func.count(HAEntity.id))
                .filter(HAEntity.state.in_(("unavailable", "unknown")))
                .scalar() or 0
            )
            lights_on = (
                session.query(func.count(HAEntity.id))
                .filter(HAEntity.domain.in_(("light", "switch")), HAEntity.state == "on")
                .scalar() or 0
            )
            # Matches the old `(e.state or "") not in (...)` truthiness:
            # a NULL state coalesces to "" here too, and "" isn't in the
            # excluded-states set, so a media_player with no state yet
            # still counts as "playing" (same as before).
            media_playing = (
                session.query(func.count(HAEntity.id))
                .filter(
                    HAEntity.domain == "media_player",
                    func.coalesce(HAEntity.state, "").notin_(
                        ("off", "idle", "standby", "unavailable", "unknown")
                    ),
                )
                .scalar() or 0
            )
            latest = session.query(func.max(HAEntity.synced_at)).scalar()

            from app.integrations.homeassistant import events as ha_events
            listener = ha_events.current_listener

            return {
                "entity_count": entity_count,
                "offline_count": offline_count,
                "lights_on": lights_on,
                "media_playing": media_playing,
                "event_stream_connected": bool(listener and listener.connected),
                "synced_at": latest.isoformat() if latest else None,
            }

    # is_configured(): default — True iff ha_url and ha_token are both set
    # (both required in this integration's config_schema).
