"""Integration registry. Import and register all integrations here."""

from app.integrations.base import BaseIntegration

# Registry of all active integrations.
INTEGRATIONS: dict[str, BaseIntegration] = {}


def register(integration: BaseIntegration) -> None:
    """Register an integration instance."""
    INTEGRATIONS[integration.name] = integration


def get_all() -> dict[str, BaseIntegration]:
    """Return all registered integrations."""
    return INTEGRATIONS


def get(name: str) -> BaseIntegration | None:
    """Get a specific integration by name."""
    return INTEGRATIONS.get(name)


def register_all() -> None:
    """Register all available integrations. Called at startup.

    V4 chunk 1.2: thin wrapper over `app.plugin.discovery.discover_integrations()`,
    which walks `app/integrations/*` on disk and instantiates each package's
    `BaseIntegration` subclass — no more hand-maintained import+register list.
    Adding a new integration package with a valid `manifest.py` requires zero
    edits here.
    """
    from app.plugin.discovery import discover_integrations

    for integration in discover_integrations():
        register(integration)
