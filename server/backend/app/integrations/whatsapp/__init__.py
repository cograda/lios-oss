"""WhatsApp integration — reads from bridge-populated DB tables.

The Node.js bridge (whatsapp-bridge container) maintains the WhatsApp
connection and writes messages to Postgres. This integration reads
those messages and exposes them via MCP tools.

`PushSourceIntegration` conversion (V4 chunk 4.3, batch B). The bridge is the
"push" here — it writes rows directly, out of band — so the base's own
`sync()` (raises `NotImplementedError`) doesn't fit: the manifest still
declares a `*/30 * * * *` schedule (unchanged from before this chunk) that
drives periodic embedding of newly-arrived messages, not a pull from an
external system, so `sync()` is overridden to keep that exact behaviour
(`tests/test_scheduler_jobs.py` pins `sync_whatsapp` staying in the job set).
`probe()` is new — it's the bridge health heartbeat
(`app.integrations.whatsapp.heartbeat`, scheduled via the manifest's
`background_tasks` as before) formalized onto the base class interface.
"""

from typing import Any

from app.db import get_db
from app.integrations.whatsapp import heartbeat as _heartbeat
from app.integrations.whatsapp.tools import get_mcp_tools
from app.plugin.bases import PushSourceIntegration


class WhatsAppIntegration(PushSourceIntegration):

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def display_name(self) -> str:
        return "WhatsApp"

    def sync(self) -> None:
        """No-op for new messages (the bridge writes them directly to the
        DB) — this is the periodic embedding pass for messages that have
        arrived since the last run."""
        from app.integrations.whatsapp.sync import embed_messages

        db = get_db()
        with db.session() as session:
            embed_messages(session, batch_size=200)

    async def probe(self) -> bool:
        """Liveness check for the Baileys bridge sidecar — same HTTP health
        check the `whatsapp_bridge_heartbeat` background task runs."""
        import asyncio

        return await asyncio.to_thread(_heartbeat._check_bridge_blocking)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        from sqlalchemy import func
        from app.integrations.whatsapp.models import WhatsAppMessage, WhatsAppContact

        db = get_db()
        with db.session() as session:
            total = session.query(func.count(WhatsAppMessage.id)).scalar() or 0
            contacts = session.query(func.count(WhatsAppContact.id)).scalar() or 0
            latest = session.query(func.max(WhatsAppMessage.timestamp)).scalar()

        return {
            "total_messages": total,
            "total_contacts": contacts,
            "latest_message": latest.isoformat() if latest else None,
        }

    # is_configured(): default (empty config_schema -> vacuously True — the
    # bridge is a separate container; if it isn't running, tables are just
    # empty rather than this integration reporting "not configured").
