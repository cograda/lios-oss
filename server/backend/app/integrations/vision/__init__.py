"""vision — image to text, as a capability with no queue of its own.

A `CapabilityService`, like `transcription` and `sheets`: no external system to
poll, no tables, no MCP tools. It exists to be called by whoever is holding an
image — see `facade.py` (`vision.image`) and `manifest.py` for the boundary.

Why this is a server integration rather than something the client daemon does:
the inbox scan runs on the server, and the daemon "deliberately does no parsing,
holds no key and makes no decisions" (root CLAUDE.md). macOS has excellent free
on-device OCR via Apple's Vision framework, but the files are on a Linux box by
the time anything looks at them, and routing them back to whichever Mac happens
to be awake would make enrichment depend on a laptop being open.
"""

from typing import Any

from app.plugin.bases import CapabilityService


class VisionIntegration(CapabilityService):
    @property
    def name(self) -> str:
        return "vision"

    @property
    def display_name(self) -> str:
        return "Vision"

    def mcp_tools(self) -> list[dict[str, Any]]:
        return []  # pure facade — nothing here for a model to call directly

    async def dashboard_data(self) -> dict[str, Any]:
        """Whether vision is wired up, for the integration panel.

        No counts: this package keeps no records. How many images have been
        described is a question for whoever owns the queue (`inbox`).
        """
        from app.integrations.vision.facade import FACADE

        return {"configured": FACADE.available()}
