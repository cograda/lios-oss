"""google_docs' declared facade — capability `docs.write`.

The only surface another integration is allowed to import from
`app.integrations.google_docs`. Mirrors `sheets`' facade one-for-one so a
caller that already mirrors a table into a Sheet can mirror a narrative
document the same way, with the same create-once/overwrite-on-write
contract.

Credential note (2026-09-06): a caller passes the account whose token does
the work explicitly (`owner_account_email`/`owner_user_id` on creation,
`account_email`/`user_id` on every other call). The tool handlers always
pass the calling user's own account — Alex's decision that no tool may act
with another user's credentials. This facade has no consumer today; a
future *scheduled* job (one that runs with no bound user, so there is no
"caller" and no "other user") may pass the document creator's account here
with a comment saying why — that is the only shape in which a non-caller
token is acceptable, and it must never be reachable from a tool call.
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
        self, session: Session, export: DocExport, *,
        account_email: str, user_id: int, markdown: str,
    ) -> None:
        _writer.write_markdown(
            session, export, account_email=account_email, user_id=user_id, markdown=markdown,
        )

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
