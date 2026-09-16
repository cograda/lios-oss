"""SQLAlchemy models for the `_template` scaffold integration — V4 chunk 4.3e.

One model, `TemplateItem`, demonstrating the two mixins almost every
integration table reaches for, and the scoping decision you have to make
consciously for every new table (decision 6 in the V4 index: "explicit,
positive sharing — every row has an owner; 'household' is a declared grant,
never the absence of a `user_id` column").

Scoping decision: `UserOwnedMixin` vs household-shared
--------------------------------------------------------
`app/mixins.py::UserOwnedMixin` adds one NOT NULL `user_id` column (FK ->
users.id, RESTRICT on delete, indexed) and is what makes a table per-user.
`TemplateItem` below takes it — this is the **safer default** for any new
table (see user-memory `feedback_useowned_mixin_pattern.md`): if the data
genuinely belongs to one person (a synced mailbox row, a reminder, a scrobble,
a health metric), it needs `user_id`, full stop. The kernel's DSL builders
(`ListTool`/`SearchTool`, via `app.tools.helpers.scoped_query`) auto-detect
`hasattr(model, "user_id")` and inject `WHERE user_id = current_user_id()` —
you get correct per-user scoping for free just by including the mixin, no
manual `WHERE` clause to remember in every handler.

The alternative — a table with NO `user_id` column at all — is for data that
is genuinely household-shared or global: `finance` (joint bank accounts),
`weather_*` (one household, one location), `whatsapp_contacts` (a shared
contact graph), `historical_documents` (a shared corpus). **This is a
positive declaration, not a default** — a table has no `user_id` because
someone consciously decided the data has no single owner, never because
adding the column felt unnecessary at the time. If you're not sure, add the
mixin: it's much cheaper to later prove a table can safely drop per-user
scoping than to discover after the fact that two family members' private
`TemplateItem` rows were silently pooled together. See
`tests/test_user_scoping.py` — the cross-user leak canary that sweeps every
read-only tool over every `UserOwnedMixin` model — for what "wrong" looks
like in practice.

`SourcedRecordMixin` (also used below) is unrelated to scoping: it's the
four housekeeping columns (`source_id`, `source_ts`, `synced_at`,
`content_hash`) that most integrations pulling from an external system want,
for dedup/change-detection/cross-source queries. Not every table needs it —
e.g. a purely local/generated table wouldn't — but it costs nothing to
include when the data does come from an external system, which is the common
case for a `SourceIntegration`.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base
from app.mixins import SourcedRecordMixin, UserOwnedMixin


class TemplateItem(UserOwnedMixin, SourcedRecordMixin, Base):
    """One record pulled from the template external service, scoped per-user.

    Composite uniqueness on a per-user table MUST start with `user_id` (see
    `UserOwnedMixin`'s own docstring) — `external_id` alone could collide
    across two different users' accounts on the same external service. The
    `UniqueConstraint` below is the concrete example of that rule.
    """

    # The placeholder appears here too (not just in manifest.py/__init__.py)
    # so that multiple copies of this scaffold — e.g. several throwaway
    # copies made by tests/test_drop_in_integration.py in one pytest session
    # — never collide on the same SQLAlchemy table name in `Base.metadata`.
    # A real, permanent integration ends up with a normal static table name
    # once you do the rename (e.g. "my_integration_items").
    __tablename__ = "__TEMPLATE_INTEGRATION_NAME___items"
    __table_args__ = (
        UniqueConstraint("user_id", "external_id", name="uq_template_items_user_external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # `external_id` is the source system's own identifier — used for
    # upsert-by-natural-key in sync.py's store_items(), same pattern as
    # google_calendar's google_event_id / lastfm's scrobble uid.
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # synced_at / source_id / source_ts / content_hash inherited from SourcedRecordMixin
    # user_id inherited from UserOwnedMixin

    def __repr__(self) -> str:
        return f"<TemplateItem {self.external_id!r} user={self.user_id}>"
