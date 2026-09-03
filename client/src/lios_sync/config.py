"""Client configuration — reads from ~/.config/lios/config.toml (was ~/.config/comar until 2026-09-02).

Created by `lios-sync setup`, loaded by the daemon and CLI commands.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-untyped]

CONFIG_DIR = Path.home() / ".config" / "lios"
CONFIG_FILE = CONFIG_DIR / "config.toml"
# Where the config lived until 2026-09-02 (lios W10 rename). Read once, copied,
# never deleted: an old `comar` daemon may still be running from it until the
# machine is re-installed, and it must keep its own config to do so.
LEGACY_CONFIG_DIR = Path.home() / ".config" / "comar"


def migrate_legacy_config() -> bool:
    """Copy ~/.config/comar/{config.toml,ca.pem} to ~/.config/lios/ if the new
    location is empty. Returns True if a copy happened. Called from the CLI
    entry point, so a plain `lios-sync status` on a machine that only ever ran
    `comar` finds its settings without a re-setup."""
    import shutil

    if CONFIG_FILE.exists() or not (LEGACY_CONFIG_DIR / "config.toml").exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("config.toml", "ca.pem"):
        src = LEGACY_CONFIG_DIR / name
        if src.exists():
            shutil.copy2(src, CONFIG_DIR / name)
    return True


@dataclass
class ServerConfig:
    urls: list[str] = field(default_factory=lambda: ["localhost:9443"])
    ca_cert: Path = field(default_factory=lambda: CONFIG_DIR / "ca.pem")
    token: str = ""

    @property
    def url(self) -> str:
        """Primary URL (first in the list). For backwards compat."""
        return self.urls[0] if self.urls else "localhost:9443"


@dataclass
class VaultConfig:
    path: Path = field(default_factory=lambda: Path.home() / "Desktop" / "Code" / "lios" / "vault")


@dataclass
class RemindersConfig:
    # Map EKSource identifier (UUID) → email. When the daemon sees a reminder
    # whose calendar.source.sourceIdentifier is in here, it stamps the reminder
    # with that email; otherwise it falls back to the source title.
    source_emails: dict[str, str] = field(default_factory=dict)


@dataclass
class VoiceMemosConfig:
    """Voice-memo capture. Off by default.

    Opt-in because it reads a private macOS container (needs Full Disk Access)
    and because every uploaded memo costs a server-side transcription — neither
    is a reasonable thing to start doing to someone silently on upgrade.
    """

    enabled: bool = False
    # Override only for tests or a non-standard container location.
    recordings_path: Path | None = None


@dataclass
class ClientConfig:
    user: str = ""
    # Local health-check port (Phase 4, 2026-07-14: this used to be the local
    # MCP server's port; the daemon is a pure side-car now and this is just
    # /health). Still read from the `mcp_port` config key so installed
    # machines don't need re-configuring — `health_port` is an alias.
    mcp_port: int = 9400
    auto_update: bool = True
    server: ServerConfig = field(default_factory=ServerConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)
    reminders: RemindersConfig = field(default_factory=RemindersConfig)
    voice_memos: VoiceMemosConfig = field(default_factory=VoiceMemosConfig)

    @property
    def health_port(self) -> int:
        """Alias for mcp_port — the health-check port (Phase 4)."""
        return self.mcp_port


def load_config() -> ClientConfig:
    """Load config from TOML file, falling back to defaults."""
    if not CONFIG_FILE.exists():
        return ClientConfig()

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    config = ClientConfig(
        user=data.get("user", ""),
        # `health_port` (new name) takes precedence if set; otherwise fall
        # back to the legacy `mcp_port` key so existing configs keep working.
        mcp_port=data.get("health_port", data.get("mcp_port", 9400)),
        auto_update=data.get("auto_update", True),
    )

    if "server" in data:
        s = data["server"]
        # Support both `urls = [...]` (preferred) and `url = "..."` (legacy)
        if "urls" in s:
            urls = s["urls"]
        elif "url" in s:
            urls = [s["url"]]
        else:
            urls = config.server.urls
        config.server = ServerConfig(
            urls=urls,
            ca_cert=Path(s["ca_cert"]) if "ca_cert" in s else config.server.ca_cert,
            token=s.get("token", ""),
        )

    if "vault" in data:
        v = data["vault"]
        config.vault = VaultConfig(
            path=Path(v["path"]).expanduser() if "path" in v else config.vault.path,
        )

    if "reminders" in data:
        r = data["reminders"]
        config.reminders = RemindersConfig(
            source_emails=dict(r.get("source_emails", {})),
        )

    if "voice_memos" in data:
        vm = data["voice_memos"]
        config.voice_memos = VoiceMemosConfig(
            enabled=bool(vm.get("enabled", False)),
            recordings_path=(
                Path(vm["recordings_path"]).expanduser()
                if vm.get("recordings_path") else None
            ),
        )

    return config
