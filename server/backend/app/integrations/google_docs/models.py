"""DocExport — tracks a Google Doc comar created and owns.

Household-shared, like `sheets`' `SheetExport` (no `UserOwnedMixin`) — the
export isn't "owned" by whoever triggered the write, only the underlying
Google Doc is (`owner_account_email`), which supplies the OAuth credentials
for API calls.

Only *comar-created* docs get a row here. A doc a human made by hand in
Google Docs is still readable/appendable/replaceable through this
integration's tools (the Docs API reaches it), but it cannot be
whole-document overwritten — see `writer.py::write_markdown` for why that
asymmetry exists and is a scope constraint rather than an oversight.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class DocExport(Base):
    __tablename__ = "doc_exports"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable identifier for what this doc holds, e.g. "renovation-brief".
    key: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))

    document_id: Mapped[str] = mapped_column(String(100))
    document_url: Mapped[str] = mapped_column(Text)

    # Whose Google OAuth token owns/writes this document.
    owner_account_email: Mapped[str] = mapped_column(String(200))
    # JSON-encoded list of emails the doc was shared with at creation time.
    shared_with: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
