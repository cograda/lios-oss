"""Client configuration — reads from ~/.config/comar/config.toml.

Created by `comar setup`, loaded by the daemon and CLI commands.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-untyped]

CONFIG_DIR = Path.home() / ".config" / "comar"
CONFIG_FILE = CONFIG_DIR / "config.toml"


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
    path: Path = field(default_factory=lambda: Path.home() / "Desktop" / "Code" / "comar" / "vault")


@dataclass
class RemindersConfig:
    # Map EKSource identifier (UUID) → email. When the daemon sees a reminder
    # whose calendar.source.sourceIdentifier is in here, it stamps the reminder
    # with that email; otherwise it falls back to the source title.
    source_emails: dict[str, str] = field(default_factory=dict)


@dataclass
class ClientConfig:
    user: str = ""
    mcp_port: int = 9400
    auto_update: bool = True
    server: ServerConfig = field(default_factory=ServerConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)
    reminders: RemindersConfig = field(default_factory=RemindersConfig)


def load_config() -> ClientConfig:
    """Load config from TOML file, falling back to defaults."""
    if not CONFIG_FILE.exists():
        return ClientConfig()

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    config = ClientConfig(
        user=data.get("user", ""),
        mcp_port=data.get("mcp_port", 9400),
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

    return config
