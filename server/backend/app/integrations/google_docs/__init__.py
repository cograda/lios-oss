"""Google Docs — read, write and edit Google Docs.

The companion to `sheets`, and shaped by the same create-once/overwrite-on-
write contract, but a different kind of integration: `sheets` is a
`CapabilityService` with no tools of its own (one consumer, `snags`, calls
its facade), whereas this one carries five MCP tools because reading and
editing a document is something a person asks for directly. It is an
`ActionIntegration` — tools that hit an external system straight from the
handler, nothing to poll, no cached copy of any document in Postgres.
`doc_exports` is a registry of documents comar owns, not a mirror of them.

Package named `google_docs`, not `docs`, deliberately: `google_calendar` and
`google_mail` are the convention for Google integrations, and a package
called `docs` in a repo that also has `server/docs/` makes every grep
ambiguous. `sheets` predates that convention. The *capability* is still
`docs.write` and the tools are still `docs_*` — capability and tool names
follow `calendar.query`/`calendar_*`, which likewise drop the provider.

Two asymmetries in this package are worth knowing before changing anything,
both explained where they live:

  - Whole-document writes go through Drive (upload HTML, let Google convert)
    while reads and targeted edits go through the Docs API. See
    `markup.py`'s docstring — it is about avoiding index arithmetic.
  - Whole-document overwrite only works on documents comar created;
    read/append/replace work on any document the account can see. See
    `manifest.py`'s note on the `drive.file` scope — it is a per-file grant.
"""

from typing import Any

from app.integrations.google_docs.tools import get_mcp_tools
from app.plugin.bases import ActionIntegration


class GoogleDocsIntegration(ActionIntegration):
    @property
    def name(self) -> str:
        return "google_docs"

    @property
    def display_name(self) -> str:
        return "Google Docs"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """How many documents comar owns, and the most recently written.

        Deliberately not a document list — the dashboard panel is a health
        summary, and the full list is what `docs_list` is for.
        """
        from app.db import get_db
        from app.integrations.google_docs.models import DocExport

        db = get_db()
        with db.session() as session:
            rows = (
                session.query(DocExport)
                .order_by(DocExport.last_synced_at.desc().nullslast())
                .all()
            )
            latest = next((row for row in rows if row.last_synced_at), None)
            return {
                "documents": len(rows),
                "last_written_at": latest.last_synced_at.isoformat() if latest else None,
                "last_written_title": latest.title if latest else None,
            }
