"""sheets's declared facade — capability `sheets.write` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.sheets`. One consumer today: `snags`, mirroring the snag
register into a shared Google Sheet on every write.

Credential delegation note (V4 chunk 2.4, deliberately on hold): today a
caller just passes `owner_account_email`/`owner_user_id` straight through to
`ensure_export`/`write_rows`, exactly as `snags/tools.py` did before this
facade existed — there's no declared "X is allowed to write using Y's
credentials" grant yet. Chunk 2.4 is where that becomes a real, checkable
delegation declaration (part of sam-rollout Phase B-D); this facade is
just the wiring boundary that chunk will attach enforcement to. No new
mechanism is being invented here.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.integrations.sheets import writer as _writer
from app.integrations.sheets.models import SheetExport


class SheetsFacade:
    #: ORM class, re-exported for callers that need to look up an existing
    #: export row directly (e.g. snags' `_sheet_url`).
    Export = SheetExport

    def ensure_export(
        self,
        session: Session,
        *,
        key: str,
        title: str,
        owner_account_email: str,
        owner_user_id: int,
        share_with: list[str],
    ) -> SheetExport | None:
        return _writer.ensure_export(
            session,
            key=key,
            title=title,
            owner_account_email=owner_account_email,
            owner_user_id=owner_user_id,
            share_with=share_with,
        )

    def write_rows(
        self,
        session: Session,
        export: SheetExport,
        *,
        owner_user_id: int,
        headers: list[str],
        rows: list[list],
    ) -> None:
        _writer.write_rows(session, export, owner_user_id=owner_user_id, headers=headers, rows=rows)


FACADE = SheetsFacade()
