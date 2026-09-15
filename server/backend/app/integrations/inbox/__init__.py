"""Inbox triage integration.

The Tines webhook at /api/inbox/ingest drops files into /inbox/<bucket>/.
This integration is the *triage* layer on top of that drop zone:

  - The hourly worker (`scan.enrich_pending`) sniffs file kind and extracts
    a short preview into the sidecar JSON. Pure enrichment, no routing.
  - MCP tools (`inbox_pending`, `inbox_preview`, `inbox_archive`,
    `inbox_dismiss`, `inbox_to_vault`, `inbox_to_corpus`) let skills surface
    the queue to the user and route each item interactively.

State lives on disk, per-user (F6, 2026-08-08 — added an `InboxItem` DB
ownership ledger, see models.py):
  /inbox/u<user_id>/<bucket>/  — pending (incoming, text, image, audio, file)
  /inbox/u<user_id>/archive/   — handled (moved here when routed)
  /inbox/u<user_id>/dismissed/ — explicitly ignored
  /inbox/<bucket>/             — legacy flat tree (pre-split); lazily
                                  adopted into user 1's subtree by
                                  `scan.adopt_legacy_files()`

Enrichment is split by cost. PDF and plaintext/markdown previews are cheap and
local, so they run inline in the ingest request. Audio and images are billable
per call and depend on a third party, so each has its own bounded cron sweep
(`transcribe_pending`, `describe_pending`) that records its outcome on the
sidecar and never retries a file it has already paid for.

`SourceIntegration` conversion (V4 chunk 4.3, batch A): the "external
system" here is the filesystem drop zone rather than a remote API, so
there's nothing meaningful to fetch-without-writing — `pull()` is a no-op
and `store()` does the actual enrichment pass, exactly as the old `sync()`
did. No multi-account concept — `accounts()` stays at the
`SourceIntegration` default.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.inbox import scan
from app.integrations.inbox.tools import get_mcp_tools
from app.plugin.bases import PullResult, SourceIntegration


class InboxIntegration(SourceIntegration):
    @property
    def name(self) -> str:
        return "inbox"

    @property
    def display_name(self) -> str:
        return "Inbox"

    def pull(self, account: Any, session: Session, cursor: str | None) -> PullResult:
        # Nothing to fetch remotely — the "source" is the local /inbox/
        # drop zone. The actual enrichment work happens in `store()`.
        return PullResult(records=[])

    def store(self, session: Session, records: list[Any]) -> int:
        # Enrichment runs synchronously — fine inside the scheduler's
        # asyncio.to_thread wrapper. Cheap (≤a few hundred files, mostly
        # text reads + first-page PDF extract).
        result = scan.enrich_pending(session)
        return result["enriched"]

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        # Cross-user total — the dashboard is an admin/household view, not a
        # per-caller tool response, so this deliberately doesn't scope to
        # one user (matches the documented exception in server/CLAUDE.md's
        # "Per-user request scoping" section).
        return {"pending": len(scan.iter_all_pending_files())}

    # is_configured(): default (nothing in config_schema is `required`, so
    # this stays vacuously True — matches prior behavior).
