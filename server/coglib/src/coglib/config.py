"""Base settings classes. Subclass CogSettings per project."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseConfig(BaseSettings):
    """Database connection config. Override url per project."""

    url: str = "postgresql://localhost:54322/postgres"  # Supabase local default
    echo: bool = False
    pool_size: int = 5


class LoggingConfig(BaseSettings):
    """Logging config. Override level/file per project."""

    level: str = "INFO"
    file: Path | None = None


class CogSettings(BaseSettings):
    """Subclass this per project. Provides DB + logging config out of the box.

    Example::

        class AppSettings(CogSettings):
            model_config = SettingsConfigDict(env_prefix="MYAPP_")
            app_name: str = "myapp"
            some_flag: bool = False
    """

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "myapp"
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
