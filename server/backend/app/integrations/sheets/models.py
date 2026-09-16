"""SheetExport — tracks a spreadsheet comar created to mirror a household
table (e.g. the snag register) for a member without MCP/vault access.

Household-shared, like the tables it exports (no UserOwnedMixin) — the
export itself isn't "owned" by whoever triggered the write, only the
underlying Google Sheet is (owner_account_email), which supplies the OAuth
credentials for API calls.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class SheetExport(Base):
    __tablename__ = "sheet_exports"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable identifier for what this export mirrors, e.g. "snags".
    key: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))

    spreadsheet_id: Mapped[str] = mapped_column(String(100))
    spreadsheet_url: Mapped[str] = mapped_column(Text)

    # Whose Google OAuth token owns/writes this sheet.
    owner_account_email: Mapped[str] = mapped_column(String(200))
    # JSON-encoded list of emails the sheet was shared with at creation time.
    shared_with: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
