"""Per-integration config/secret store — V4 chunk 3.3.

Kernel-owned table (not any one integration's own model, since it holds
every integration's config in one place). Secret values are Fernet-encrypted
via `app.auth.encryption` before being written; `app.plugin.config_store`
is the only reader/writer that should touch this table directly.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class IntegrationConfig(Base):
    """One config key for one integration.

    `value` holds the raw string form (Fernet ciphertext when `is_secret`),
    coerced to its declared type by `app.plugin.config_store` using the
    owning integration's manifest `config_schema`.
    """

    __tablename__ = "integration_config"
    __table_args__ = (
        UniqueConstraint("integration", "key", name="uq_integration_config_integration_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    integration: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Nullable: the one-time import command writes rows with no acting user.
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
