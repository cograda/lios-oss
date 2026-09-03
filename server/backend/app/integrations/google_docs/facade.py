"""google_docs' declared facade — capability `docs.write`.

The only surface another integration is allowed to import from
`app.integrations.google_docs`. Mirrors `sheets`' facade one-for-one so a
caller that already mirrors a table into a Sheet can mirror a narrative
document the same way, with the same create-once/overwrite-on-write
contract.

Credential delegation note (V4 chunk 2.4, deliberately on hold): a caller
passes `owner_account_email`/`owner_user_id` straight through, exactly as
`snags` does for the Sheets export. There is no declared "X may write using
Y's credentials" grant yet; this facade is the wiring boundary chunk 2.4
will attach enforcement to. No new mechanism is invented here.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.integrations.google_docs import writer as _writer
from app.integrations.google_docs.models import DocExport


class DocsFacade:
    #: ORM class, re-exported for callers that need to look up an existing
    #: export row directly (to render its URL, say).
    Export = DocExport

    def ensure_export(
        self,
        session: Session,
        *,
        key: str,
        title: str,
        markdown: str,
        owner_account_email: str,
        owner_user_id: int,
        share_with: list[str],
    ) -> DocExport | None:
        return _writer.ensure_export(
            session,
            key=key,
            title=title,
            markdown=markdown,
            owner_account_email=owner_account_email,
            owner_user_id=owner_user_id,
            share_with=share_with,
        )

    def write_markdown(
        self, session: Session, export: DocExport, *, owner_user_id: int, markdown: str,
    ) -> None:
        _writer.write_markdown(session, export, owner_user_id=owner_user_id, markdown=markdown)

    def read_markdown(
        self, session: Session, *, document: str, account_email: str, user_id: int,
    ) -> dict:
        return _writer.read_markdown(
            session, document=document, account_email=account_email, user_id=user_id,
        )

    def append_text(
        self, session: Session, *, document: str, text: str, account_email: str, user_id: int,
    ) -> dict:
        return _writer.append_text(
            session, document=document, text=text, account_email=account_email, user_id=user_id,
        )


FACADE = DocsFacade()
