"""Project settings. Subclasses coglib.CogSettings."""

from pathlib import Path

from pydantic_settings import SettingsConfigDict

from coglib import CogSettings

# .env lives at project root (one level above backend/).
# In Docker, env vars are injected directly via docker-compose env_file,
# so the .env file may not exist — that's fine.
_env_file = Path(__file__).resolve().parent.parent.parent / ".env"


class HomeSettings(CogSettings):
    model_config = SettingsConfigDict(
        env_prefix="HOME_",
        env_nested_delimiter="__",
        env_file=str(_env_file) if _env_file.exists() else None,
        extra="ignore",
    )

    app_name: str = "comar-server"
    debug: bool = False
    port: int = 8400

    # MCP bearer token
    mcp_token: str = ""

    # UI access token (simple auth for the web dashboard)
    ui_token: str = ""

    # Obsidian vault path (inside container).
    # LEGACY: bind-mounted to /vaults/alex. New code should prefer vaults_root_path
    # below + user-aware resolution via app.services.vault_paths.user_vault_path().
    obsidian_vault_path: str = "/obsidian"

    # Root of the per-user vault tree (inside container).
    #   /vaults/<user>/  — that user's personal vault
    # Logical paths from MCP callers resolve to /vaults/<current_user>/<path>
    # (single-user vaults; no cross-user Shared/ namespace).
    vaults_root_path: str = "/vaults"

    # Google OAuth (shared app for Calendar, Gmail, Photos)
    google_client_id: str = ""
    google_client_secret: str = ""

    # OAuth redirect base URL override (Google rejects private IPs like 192.168.x.x).
    # Set to "http://localhost:8400" and use an SSH tunnel for auth.
    oauth_redirect_base: str = ""

    # Health Auto Export push token (separate from UI token for least-privilege)
    health_push_token: str = ""

    # Home Assistant
    ha_url: str = ""
    ha_token: str = ""
    # Record numeric→numeric transitions in ha_state_changes (off: HA's own
    # recorder keeps numeric series; comar keeps the meaningful transitions)
    ha_record_numeric_history: bool = False

    # Last.fm
    lastfm_api_key: str = ""
    lastfm_username: str = ""

    # Enable Banking
    banking_api_key: str = ""

    # Anthropic (for Haiku-powered task matching)
    anthropic_api_key: str = ""

    # OAuth token encryption (Fernet key, base64-encoded)
    oauth_encryption_key: str = ""

    # Shared secret sent as X-Bridge-Secret to the whatsapp-bridge's
    # /download/:messageId endpoint. Must match BRIDGE_SHARED_SECRET on that
    # container. Empty = no header sent (matches the bridge's dev-mode default).
    wa_bridge_shared_secret: str = ""

    # MCP OAuth 2.1 authorization server (claude.ai connector sign-in).
    # Public issuer URL (the Tailscale host, e.g.
    # https://your-server.your-tailnet.ts.net). Empty → OAuth routes disabled.
    oauth_issuer: str = ""

    # Inbox (automation webhook ingestion pipeline)
    inbox_path: str = "/inbox"
    inbox_token: str = ""

    # Media store (WhatsApp images/videos/audio) — volume-mounted host dir
    media_root: str = "/data/media"

    # Server-side Syncthing — the comar app talks to it over the docker bridge
    # gateway (so the REST API isn't exposed on Tailscale). API key is generated
    # by Syncthing on first run and read from its config.xml.
    syncthing_url: str = "http://172.21.0.1:8384"
    syncthing_api_key: str = ""
    # Folder ID that the server's Syncthing shares with every paired Mac.
    # Cross-device contract; never rename without manually updating each client.
    syncthing_folder_id: str = "vault"

    # Calendar visibility per account: "full" | "busy" | "hidden"
    # Unrecognised accounts default to "full". The hidden example is an
    # extended-family calendar managed separately.
    calendar_visibility: dict[str, str] = {
        "alex@example.com": "full",
        "alex@work.com": "busy",
        "nana@example.com": "hidden",
    }

    # Weather location for the Open-Meteo integration (defaults: Dublin)
    weather_latitude: float = 53.3498
    weather_longitude: float = -6.2603

    # Irish Rail home station (code + display name), e.g. Malahide
    rail_station_code: str = "MHIDE"
    rail_station_name: str = "Malahide"

    # Account-holder names as they appear on bank statements, comma-separated.
    # Used by finance transfer detection to recognise "Transfer to/from <NAME>"
    # between the household's own Revolut accounts as internal.
    transfer_match_names: str = ""


settings = HomeSettings()
