"""WhatsApp integration — reads from bridge-populated DB tables.

The Node.js bridge (whatsapp-bridge container) maintains the WhatsApp
connection and writes messages to Postgres. This integration reads
those messages and exposes them via MCP tools.

No sync schedule — the bridge handles real-time message capture.
Embedding is triggered manually or could be scheduled.
"""

from typing import Any

from app.db import get_db
from app.integrations.base import BaseIntegration
from app.integrations.whatsapp.tools import get_mcp_tools


class WhatsAppIntegration(BaseIntegration):

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def display_name(self) -> str:
        return "WhatsApp"

    def sync(self) -> None:
        """No-op — the bridge writes messages directly to the DB.

        This could optionally trigger embedding of new messages.
        """
        from app.integrations.whatsapp.sync import embed_messages

        db = get_db()
        with db.session() as session:
            embed_messages(session, batch_size=200)

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

    def sync_schedule(self) -> str | None:
        """Run embedding every 30 minutes to chunk new messages."""
        return "*/30 * * * *"

    def is_configured(self) -> bool:
        """Always configured — the bridge is a separate container.

        If the bridge isn't running, the tables will just be empty.
        """
        return True
