"""Cross-user read grants — the one way a user sees another user's vault.

Vaults are per-user directories (`/vaults/<user_name>`) and every vault query
is scoped by `current_user_id()`. That isolation is deliberate and predates
this table: the old `Shared/` namespace was **removed** on 2026-06-05 when
Sam moved to her own vault, because a shared writable namespace made "whose
note is this?" unanswerable.

This table does not bring that back. A grant is:

  **one-directional** — `grantee` may read `owner`; never the reverse, and
  never transitive.
  **read-only** — enforced at dispatch against each tool's own
  `read_only_hint` annotation, not against a list of tool names.
  **scoped** — to one integration (`obsidian` today). A grant is not a
  general impersonation; it does not reach Gmail, WhatsApp or health data.
  **revocable in one row** — `DELETE` and the access is gone at the next
  call, with no credential to rotate and nothing to redeploy.

🔑 **Why this exists at all.** The lios agent host runs as its own comar user
so its access is revocable and attributable in `ai_usage`. That same
separation left it unable to read the knowledge layer it exists to operate
against — its own vault is genuinely empty. The alternatives were worse: a
symlink would re-index one vault under two users (duplicate embeddings,
duplicate cost, two write paths into one directory), and an rsync would make
a second editable copy of the thing the monorepo exists to de-duplicate.

⚠️ **A grant is not a credential and must not be treated as one.** It grants
`grantee` read of `owner`'s vault *whoever is holding the grantee's bearer*.
Revoking a compromised bearer is still a separate act; this row only decides
what a legitimately-authenticated grantee may look at.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


# The integrations a grant may cover. Deliberately an allowlist, not "every
# read-only tool": `read_only_hint` says a tool does not *write*, which is a
# much weaker claim than "this data is in scope". `gmail_search` is read-only
# and emphatically out of scope.
GRANTABLE_SCOPES = frozenset({"obsidian"})


class VaultReadGrant(Base):
    __tablename__ = "vault_read_grants"
    __table_args__ = (
        # One row per (grantee, owner, scope) — re-granting is idempotent
        # rather than an audit trail of duplicates.
        UniqueConstraint(
            "grantee_user_id", "owner_user_id", "scope",
            name="uq_vault_read_grant",
        ),
        Index("ix_vault_read_grants_grantee", "grantee_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Who may read. FK'd so deleting a user takes their grants with them —
    # a dangling grant would be a grant nobody can see to revoke.
    grantee_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    # Whose data may be read.
    owner_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )

    # Integration name, matching `BaseIntegration.name` — checked against
    # GRANTABLE_SCOPES at grant time *and* at dispatch, so a scope removed
    # from the allowlist stops working without needing the rows deleted.
    scope: Mapped[str] = mapped_column(String(50), nullable=False)

    # Why this grant exists. Required by the service layer rather than the
    # column, because a grant whose reason nobody recorded is a grant nobody
    # will feel able to revoke.
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<VaultReadGrant grantee={self.grantee_user_id} "
            f"owner={self.owner_user_id} scope={self.scope!r}>"
        )
