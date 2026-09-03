"""Google Sheets export — a `CapabilityService` (V4 chunk 4.2): no external
sync (nothing pulls data *from* Sheets), no MCP tools of its own — just a
reusable "push a table of rows to a Sheet" writer other integrations call
through `app.integrations.sheets.facade` (capability `sheets.write`). See
`writer.py` for the create-once/overwrite-on-write contract.

Before this chunk, `sheets` had no `BaseIntegration` subclass at all — a
manifest-less library `app.plugin.validate`/`discovery` explicitly skipped
(see those modules' docstrings). It's real, discoverable capability package
now: `provides=["sheets.write"]`, owns `sheet_exports`, still zero MCP tools.
"""

from typing import Any

from app.plugin.bases import CapabilityService


class SheetsIntegration(CapabilityService):
    @property
    def name(self) -> str:
        return "sheets"

    @property
    def display_name(self) -> str:
        return "Google Sheets Export"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []  # no tools of its own — pure facade for other integrations
