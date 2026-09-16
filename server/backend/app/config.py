"""Project settings. Subclasses coglib.CogSettings."""

from pathlib import Path

from pydantic_settings import SettingsConfigDict

from coglib import CogSettings

# .env lives at project root (one level above backend/).
# In Docker, env vars are injected directly via docker-compose env_file,
# so the .env file may not exist — that's fine.
_env_file = Path(__file__).resolve().parent.parent.parent / ".env"


class HomeSettings(CogSettings):
    """Kernel/bootstrap settings only (V4 chunk 3.3).

    Every field here is either infra plumbing the app needs before it can
    even reach the database (db connection lives in coglib's CogSettings;
    ports, tokens gating auth middleware itself, the encryption key that
    guards the config table this settles values *into*) or a Docker volume
    mount path. Everything that used to live here per-integration (API
    keys, feature toggles, per-account visibility maps, ...) now lives in
    the `integration_config` DB table, declared by each integration's
    manifest `config_schema` and read via `app.plugin.config_store.plugin_config()`.
    See server/.env.example for the current full list of HOME_* env vars,
    including the ones that only matter for the one-time
    `python -m app.plugin.import_config` copy into that table.
    """

    model_config = SettingsConfigDict(
        env_prefix="HOME_",
        env_nested_delimiter="__",
        env_file=str(_env_file) if _env_file.exists() else None,
        extra="ignore",
    )

    app_name: str = "lios-core"
    debug: bool = False
    port: int = 8400

    # UI access token (simple auth for the web dashboard, and the auth
    # middleware that gates every /api/* route — must be readable before any
    # integration-specific config lookup could even happen).
    # `ui_token` (HOME_UI_TOKEN) was removed 2026-09-06: the dashboard signs
    # in with the per-user bearer and holds a server-side session
    # (`app/auth/ui_session.py`). There is no shared dashboard password.

    # Obsidian vault path (inside container).
    # LEGACY: bind-mounted to /vaults/alex. New code should prefer vaults_root_path
    # below + user-aware resolution via app.services.vault_paths.user_vault_path().
    obsidian_vault_path: str = "/obsidian"

    # Root of the per-user vault tree (inside container).
    #   /vaults/<user>/  — that user's personal vault
    # Logical paths from MCP callers resolve to /vaults/<current_user>/<path>
    # (single-user vaults; no cross-user Shared/ namespace).
    vaults_root_path: str = "/vaults"

    # Google OAuth (shared app registration for every Google-scoped
    # integration — calendar, gmail, sheets/drive — hence kernel, not any
    # one integration's own config)
    google_client_id: str = ""
    google_client_secret: str = ""

    # OAuth redirect base URL override (Google rejects private IPs like 192.168.x.x).
    # Set to "http://localhost:8400" and use an SSH tunnel for auth.
    oauth_redirect_base: str = ""

    # OAuth token / integration_config secret encryption (Fernet key,
    # base64-encoded). REQUIRED — app/auth/encryption.py is fail-closed
    # (V4 chunk 3.3): unset, any encrypt/decrypt raises rather than passing
    # secrets through in plaintext. Generate one:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    oauth_encryption_key: str = ""

    # MCP OAuth 2.1 authorization server (claude.ai connector sign-in).
    # Public issuer URL (the Tailscale host, e.g.
    # https://ubuntudockerbox.tail78010b.ts.net). Empty → OAuth routes disabled.
    oauth_issuer: str = ""

    # `oauth_phase0_enable` (HOME_OAUTH_PHASE0_ENABLE) was deleted 2026-09-06
    # along with the Phase-0 login it gated — a shared-UI-token form that
    # minted an OAuth session as user_id=1. See app/auth/oauth_wire.py.

    # Inbox (capture ingestion pipeline) — Docker volume mount path. The
    # route authenticates with the per-user `client_tokens` bearer only; the
    # shared `inbox_token` webhook secret was removed 2026-09-06.
    inbox_path: str = "/inbox"

    # Cloudflare Access, edge-side defence in depth for the one public route
    # (`/api/inbox/ingest`, behind `ingest.comar.ie` — see
    # deploy/docs/cloudflare-tunnel.md). lios#139: a `CF-Access-Client-Id`
    # allow-list shipped and was reverted the same evening (PR #64/#65) —
    # Access does not forward the service token's client id to the origin at
    # all. What it DOES forward is a signed JWT in `Cf-Access-Jwt-Assertion`,
    # so `app/auth/cf_access.py` verifies that instead: signature against the
    # team's JWKS (`https://<team>/cdn-cgi/access/certs`, cached), `iss` ==
    # `https://<team_domain>`, `aud` contains this value, `exp`/`nbf`
    # respected.
    #
    # Both unset (the default) is a no-op — current behaviour, logged once at
    # startup. Setting both turns the check on; a request to the ingest route
    # with a missing or invalid header then gets a 401 before the route's own
    # bearer is even looked at. This is additive: it never replaces the
    # per-user `client_tokens` bearer check below it.
    #
    # `cf_access_team_domain` is the `<team>` in `https://<team>.
    # cloudflareaccess.com` — Cloudflare Zero Trust dashboard → Settings →
    # Custom Pages (or any Access policy page) shows it as the "Team domain".
    # `cf_access_aud` is the Access application's AUD tag — Zero Trust →
    # Access → Applications → (the ingest app) → Overview, "Application Audience
    # (AUD) Tag". Real values for this deployment live in the gitignored
    # `deploy/certs/cloudflare-access-tokens.env` — never commit them.
    cf_access_team_domain: str = ""
    cf_access_aud: str = ""

    # Media store (WhatsApp images/videos/audio) — volume-mounted host dir
    media_root: str = "/data/media"

    # Server-side Syncthing — the comar app talks to it over the docker
    # bridge gateway (so the REST API isn't exposed on Tailscale). Kernel,
    # not any one integration's config: used both by the kernel's own
    # /api/v1/syncthing/* pairing routes and by obsidian's vault_transfer
    # tool. API key is generated by Syncthing on first run (its config.xml).
    syncthing_url: str = "http://172.21.0.1:8384"
    syncthing_api_key: str = ""
    # Folder ID that the server's Syncthing shares with every paired Mac.
    # Cross-device contract; never rename without manually updating each
    # client.
    syncthing_folder_id: str = "vault"

    # Embedding provider(s) (V4 chunk 3.4; extended to a list in Phase 2) —
    # governs the shared embedding infrastructure
    # (app/plugin/embedding_provider.py) that every embedding-producing
    # integration feeds into, so it's a kernel setting rather than any one
    # integration's config_schema entry.
    #
    # Comma-separated and ORDERED: the first entry is the search default, the
    # rest are fallbacks tried in order when it's unavailable. Every listed
    # provider is written to, so each has full coverage and can actually serve
    # as a fallback; each has its own table (see
    # app/integrations/embedding/models.py) because pgvector fixes the
    # dimension in the column type.
    #
    # Valid ids: "fastembed-bge-small" (384d, local, no key), "gemini-embedding-2"
    # (1536d, needs embedding.gemini_api_key). Default stays local-only so a
    # fresh deployment needs no API key; production sets
    # HOME_EMBEDDING_PROVIDER="gemini-embedding-2,fastembed-bge-small".
    embedding_provider: str = "fastembed-bge-small"

    # POST /api/v1/batch (lios "one data layer" plan §5 step 1) — kernel
    # setting, not an integration's, since batch dispatches across every
    # integration's tools alike.
    #
    # Cap on items per batch request. Guards against one request fanning out
    # into an unbounded number of concurrent tool dispatches (each of which
    # can itself take up to `TOOL_TIMEOUT_SECONDS`).
    batch_max_items: int = 25
    # Overall wall-clock budget for one batch request, covering every item
    # dispatched concurrently under it. An item still running when the
    # budget expires is reported back as its own per-item `timeout` error —
    # the budget bounds the *request*, not any single item.
    batch_timeout_seconds: float = 30.0


settings = HomeSettings()
