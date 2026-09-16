"""Per-user preference store.

Kernel-owned, and deliberately separate from `integration_config`. The two
answer different questions:

  * `integration_config` — "how is this *deployment* wired up" (API keys,
    the weather location, the commute route). One row per (integration, key),
    shared by everyone on the server.
  * `user_preferences`   — "how does *this person* want their output shaped"
    (which daily-note sections they get, their weekly strength target, which
    appliances they care about). One row per (user, key).

Conflating them was the tempting shortcut and it breaks immediately: two
people on one server have one commute route but two different daily notes.

`value` holds a JSON-encoded string in every case, including plain strings,
so the round-trip is lossless for the `list_str` keys and there is only one
decode path. `app.services.preferences` is the only module that should read
or write this table directly; it coerces to the declared type from the
registry in that module.

No secrets here by design: preferences are display/shape choices, and
nothing in the registry warrants Fernet. If that ever changes, mirror
`integration_config`'s `is_secret` column rather than storing plaintext.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base

from app.mixins import UserOwnedMixin


class UserPreference(UserOwnedMixin, Base):
    """One preference key for one user."""

    __tablename__ = "user_preferences"
    __table_args__ = (
        # user_id first, per the UserOwnedMixin contract — a bare `key`
        # unique would collide across users on the very first write.
        UniqueConstraint("user_id", "key", name="uq_user_preferences_user_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
