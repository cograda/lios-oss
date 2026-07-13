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
    """Register all available integrations. Called at startup."""
    from app.integrations.google_calendar import GoogleCalendarIntegration
    from app.integrations.google_mail import GoogleMailIntegration
    from app.integrations.apple_reminders import AppleRemindersIntegration
    from app.integrations.finance import FinanceIntegration
    from app.integrations.lastfm import LastfmIntegration
    from app.integrations.obsidian import ObsidianIntegration
    from app.integrations.weather import WeatherIntegration
    from app.integrations.irish_rail import IrishRailIntegration
    from app.integrations.whatsapp import WhatsAppIntegration
    from app.integrations.system import SystemIntegration
    from app.integrations.apple_health import AppleHealthIntegration
    from app.integrations.historical_corpus import HistoricalCorpusIntegration
    from app.integrations.attachments import AttachmentsIntegration
    from app.integrations.coffee import CoffeeIntegration
    from app.integrations.inbox import InboxIntegration
    from app.integrations.homeassistant import HomeAssistantIntegration
    from app.integrations.media import MediaIntegration
    from app.integrations.snags import SnagsIntegration

    register(GoogleCalendarIntegration())
    register(GoogleMailIntegration())
    register(AppleRemindersIntegration())
    register(FinanceIntegration())
    register(LastfmIntegration())
    register(ObsidianIntegration())
    register(WeatherIntegration())
    register(IrishRailIntegration())
    register(WhatsAppIntegration())
    register(SystemIntegration())
    register(AppleHealthIntegration())
    register(HistoricalCorpusIntegration())
    register(AttachmentsIntegration())
    register(CoffeeIntegration())
    register(InboxIntegration())
    register(HomeAssistantIntegration())
    register(MediaIntegration())
    register(SnagsIntegration())
