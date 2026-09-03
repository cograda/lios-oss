"""transcription — audio to text, as a capability with no queue of its own.

A `CapabilityService`, like `sheets`: no external system to poll, no tables, no
MCP tools. It exists to be called by whoever is holding an audio file — see
`facade.py` (`transcription.audio`) and `manifest.py` for why the boundary is
drawn there.
"""

from typing import Any

from app.plugin.bases import CapabilityService


class TranscriptionIntegration(CapabilityService):
    @property
    def name(self) -> str:
        return "transcription"

    @property
    def display_name(self) -> str:
        return "Transcription"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []  # pure facade — nothing here for a model to call directly

    async def dashboard_data(self) -> dict[str, Any]:
        """Whether transcription is wired up, for the integration panel.

        No counts: this package keeps no records. How many transcripts exist is a
        question for whoever owns the queue (`inbox`).
        """
        from app.integrations.transcription.facade import FACADE

        return {"configured": FACADE.available()}
