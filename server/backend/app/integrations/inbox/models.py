"""SQLAlchemy model for inbox per-user ownership (security finding F6).

Before 2026-08-08 the inbox had no DB presence at all — state lived purely
on disk (`/inbox/<bucket>/`) and any valid bearer could list, preview, or
route any file via `inbox_pending`/`inbox_preview`/etc. The primary fix is
the on-disk partition itself (`scan.py::user_root()` — one subtree per
user, and every read/route function takes a `user_id` and stays inside it).

This table is a secondary, DB-backed ownership record layered on top: one
row per ingested item (written at ingest time and by the legacy-tree
adoption step), independent of the filesystem layout. It is NOT consulted
by reads or by dedup (`scan.find_by_hash` walks the caller's own subtree
directly, same as before F6, just rooted per-user) — it exists so ownership
survives even if a file is later moved by hand outside the tool surface,
and as an audit trail of who ingested what.

No backfill dance here (contrast `alembic/versions/*_vault_chunks_user_scope.py`
or `*_whatsapp_contacts_per_user.py`, which add `user_id` to tables that
already held rows): `inbox_items` is a brand-new table, so it starts empty
and `user_id` is `NOT NULL` from creation — there is nothing to backfill at
the SQL level. The equivalent of a "legacy backfill" for inbox is a
filesystem-and-data operation, not a schema one: pre-existing flat-tree
files (ingested before this table existed) get adopted into user 1's
subtree AND get an `InboxItem` row inserted for them, lazily and
idempotently, by `scan.py::adopt_legacy_files()` — see its docstring.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import UserOwnedMixin


class InboxItem(UserOwnedMixin, Base):
    """One row per inbox item, recording who owns it.

    `relative_path` is `<bucket>/<filename>` under that user's own inbox
    subtree (`scan.user_root(user_id)`) — NOT an absolute path, so a row
    stays valid if the inbox root ever moves (e.g. a different Docker
    volume mount).
    """

    __tablename__ = "inbox_items"
    __table_args__ = (
        UniqueConstraint("user_id", "relative_path", name="uq_inbox_items_user_relpath"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Content hash, when known — powers per-user-scoped dedup in
    # `scan.find_by_hash`. Nullable because older sidecars (pre content-hash
    # dedup) may not carry one.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
