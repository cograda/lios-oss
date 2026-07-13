"""SQLAlchemy models."""

from app.models.users import User
from app.models.tokens import OAuthToken, SyncState, SyncHistory
from app.models.clients import ClientToken, ClientLog, InstallCode
from app.models.tool_calls import ToolCall
from app.models.oauth_clients import (
    OAuthClient,
    OAuthLoginSession,
    OAuthAuthorizationCode,
    McpAccessToken,
)
from app.integrations.google_calendar.models import CalendarEvent
from app.integrations.apple_reminders.models import Reminder, ReminderCommand
from app.integrations.finance.models import (
    Account,
    AccountFingerprint,
    Category,
    CategorizationRule,
    Transaction,
    ImportHistory,
    MonthlySummary,
)
from app.integrations.google_mail.models import MailMessage
from app.integrations.obsidian.models import VaultChunk
from app.integrations.weather.models import WeatherCurrent, WeatherForecast
from app.integrations.lastfm.models import Scrobble, ArtistTag
from app.integrations.whatsapp.models import WhatsAppMessage, WhatsAppContact
from app.integrations.apple_health.models import HealthDailyMetric, HealthWorkout, HealthSleepSession
from app.integrations.historical_corpus.models import HistoricalDocument, HistoricalDocumentChunk
from app.integrations.attachments.models import MessageAttachment
from app.integrations.media.models import MediaItem
from app.integrations.snags.models import Snag, SnagMedia, SnagSourceMessage
from app.integrations.coffee.models import Coffee, CoffeeBrew, CoffeeEquipmentProfile
from app.integrations.homeassistant.models import HAEntity, HAStateChange
from app.services.embedding import EmbeddingQueue, Embedding

__all__ = [
    "User",
    "OAuthToken",
    "SyncState",
    "SyncHistory",
    "ClientToken",
    "ClientLog",
    "InstallCode",
    "ToolCall",
    "OAuthClient",
    "OAuthLoginSession",
    "OAuthAuthorizationCode",
    "McpAccessToken",
    "CalendarEvent",
    "MailMessage",
    "Reminder",
    "ReminderCommand",
    "Account",
    "AccountFingerprint",
    "Category",
    "CategorizationRule",
    "Transaction",
    "ImportHistory",
    "MonthlySummary",
    "VaultChunk",
    "WeatherCurrent",
    "WeatherForecast",
    "Scrobble",
    "ArtistTag",
    "WhatsAppMessage",
    "WhatsAppContact",
    "HealthDailyMetric",
    "HealthWorkout",
    "HealthSleepSession",
    "HistoricalDocument",
    "HistoricalDocumentChunk",
    "MessageAttachment",
    "Coffee",
    "CoffeeBrew",
    "CoffeeEquipmentProfile",
    "HAEntity",
    "HAStateChange",
    "EmbeddingQueue",
    "Embedding",
]
