"""Google Docs read/write primitives — the whole external-facing surface.

Four operations, deliberately split across two Google APIs because each API
is only good at some of them:

  Drive API (`drive.file`)   whole-document create + overwrite, by uploading
                             HTML and letting Google convert it. Reaches only
                             documents comar created.
  Docs API (`documents`)     read, append, find/replace. Reaches any document
                             the account can see, including hand-made ones.

`ensure_export` + `write_markdown` mirror `sheets`' `ensure_export` +
`write_rows` exactly: create the file once per `key` (tracked in
`doc_exports`), share it with the given collaborators at creation time, then
overwrite its contents wholesale on every subsequent call. Same
create-once/overwrite-on-write contract, same reason — no incremental
diffing for a small document — and, importantly, the same *file id*, so the
URL and everybody's access survive a rewrite. That is the property that
makes this usable for a document revised repeatedly over years.

Failures raise (classified Transient/Permanent) rather than swallow, exactly
as `sheets/writer.py` does — a caller mirroring its own data into a doc
should wrap these in a try/except so a Docs outage never blocks its
primary write.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from googleapiclient.http import MediaInMemoryUpload
from sqlalchemy.orm import Session

from app.errors import PermanentError
from app.integrations.google_docs.client import (
    _classify, get_docs_service, get_drive_service,
)
from app.integrations.google_docs.markup import (
    document_to_markdown, end_index, markdown_to_html,
)
from app.integrations.google_docs.models import DocExport

logger = logging.getLogger(__name__)

DOC_MIME = "application/vnd.google-apps.document"
HTML_MIME = "text/html"

_DOC_URL_ID = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")


def document_id_from(reference: str) -> str:
    """Accept either a bare document id or any Google Docs URL.

    Callers paste URLs far more often than ids, and a URL silently used as
    an id produces a 404 that reads like a permissions problem. Normalising
    here means every tool handler accepts both without each one re-deriving
    the rule.
    """
    reference = (reference or "").strip()
    match = _DOC_URL_ID.search(reference)
    if match:
        return match.group(1)
    return reference


def _docs(session: Session, account_email: str, user_id: int):
    """Docs service for `account_email`, or a PermanentError naming it.

    The three Docs-API entry points below all need this and all fail the
    same way, so the message lives here once. It is a `PermanentError`
    rather than a returned `None` because a missing token does not fix
    itself on retry — unlike `ensure_export`, which returns `None` so a
    caller mirroring its own data can treat "not configured" as a skip.
    """
    service = get_docs_service(account_email, session, user_id=user_id)
    if service is None:
        raise PermanentError(
            f"no Google credentials stored for {account_email} — reconnect the account"
        )
    return service


# ---------------------------------------------------------------------------
# Whole-document create / overwrite (Drive)
# ---------------------------------------------------------------------------


def _create_document(
    drive_service, title: str, html: str, share_with: list[str],
) -> tuple[str, str]:
    """Create a Google Doc from HTML, share it, return (document_id, url)."""
    media = MediaInMemoryUpload(html.encode("utf-8"), mimetype=HTML_MIME, resumable=False)
    result = drive_service.files().create(
        body={"name": title, "mimeType": DOC_MIME},
        media_body=media,
        fields="id,webViewLink",
    ).execute()
    document_id = result["id"]
    url = result.get("webViewLink") or f"https://docs.google.com/document/d/{document_id}/edit"

    for email in share_with:
        try:
            drive_service.permissions().create(
                fileId=document_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=True,
                fields="id",
            ).execute()
        except Exception:
            # Non-fatal — the doc exists even if one share invite fails; log
            # it so it surfaces rather than silently leaving someone out.
            logger.exception(f"[google_docs] failed to share {document_id} with {email}")

    return document_id, url


def ensure_export(
    session: Session,
    *,
    key: str,
    title: str,
    markdown: str,
    owner_account_email: str,
    owner_user_id: int,
    share_with: list[str],
) -> DocExport | None:
    """Get the DocExport row for `key`, creating the document on first call.

    Unlike `sheets`' equivalent this takes the initial `markdown` — a Google
    Doc is created *from* content in one Drive call (there is no "create
    empty, then fill" that isn't strictly more work and one more round trip).
    On an existing key the markdown is ignored here; `write_markdown` is what
    updates content.

    Returns None (not an error) if the owner account has no valid Google
    credentials yet — callers should treat that as "export not configured".
    """
    export = session.query(DocExport).filter_by(key=key).one_or_none()
    if export is not None:
        return export

    drive_service = get_drive_service(owner_account_email, session, user_id=owner_user_id)
    if drive_service is None:
        return None

    html = markdown_to_html(markdown)
    try:
        document_id, url = _create_document(drive_service, title, html, share_with)
    except Exception as exc:
        raise _classify(exc, f"create document for {key!r}") from exc

    export = DocExport(
        key=key,
        title=title,
        document_id=document_id,
        document_url=url,
        owner_account_email=owner_account_email,
        shared_with=json.dumps(share_with),
    )
    session.add(export)
    session.commit()
    logger.info(f"[google_docs] created export {key!r} -> {url}")
    return export


def write_markdown(
    session: Session,
    export: DocExport,
    *,
    account_email: str,
    user_id: int,
    markdown: str,
) -> None:
    """Replace the document's entire contents with `markdown`, in place.

    Same document id, so the URL and every existing share survive. Revision
    history survives too — Docs records this as one revision, which means a
    bad overwrite is recoverable from File > Version history rather than
    being final.

    `account_email`/`user_id` name whose token does the write. Until
    2026-09-06 this read `export.owner_account_email` (the creating account)
    with a caller-supplied `owner_user_id`; a tool call now always passes the
    caller's own account (see tools._caller_account). The export's
    `owner_account_email` is kept as a record of who created the document
    and is no longer what selects a credential here. Raises `PermanentError`
    when that account has no stored token — a missing credential is the
    caller's to fix, never a silent no-op.
    """
    drive_service = get_drive_service(account_email, session, user_id=user_id)
    if drive_service is None:
        raise PermanentError(
            f"no Google credentials stored for {account_email} — reconnect the account"
        )

    media = MediaInMemoryUpload(
        markdown_to_html(markdown).encode("utf-8"), mimetype=HTML_MIME, resumable=False,
    )
    try:
        drive_service.files().update(
            fileId=export.document_id, media_body=media, fields="id",
        ).execute()
    except Exception as exc:
        raise _classify(exc, f"write document for {export.key!r}") from exc

    export.last_synced_at = datetime.now(timezone.utc)
    session.commit()


# ---------------------------------------------------------------------------
# Read / append / replace (Docs API — works on any visible document)
# ---------------------------------------------------------------------------


def read_markdown(
    session: Session, *, document: str, account_email: str, user_id: int,
) -> dict:
    """Fetch a document and return `{title, document_id, markdown}`."""
    document_id = document_id_from(document)
    docs_service = _docs(session, account_email, user_id)
    try:
        doc = docs_service.documents().get(documentId=document_id).execute()
    except Exception as exc:
        raise _classify(exc, f"read document {document_id}") from exc

    return {
        "document_id": document_id,
        "title": doc.get("title", ""),
        "url": f"https://docs.google.com/document/d/{document_id}/edit",
        "markdown": document_to_markdown(doc),
    }


def append_text(
    session: Session, *, document: str, text: str, account_email: str, user_id: int,
) -> dict:
    """Append plain text to the end of a document.

    Plain text, not markdown, and that is a real limitation rather than an
    unfinished edge: `insertText` inserts characters, so "## Heading" would
    land as the literal characters `## Heading`. Making an appended heading a
    *real* heading needs a second `updateParagraphStyle` request against the
    range the insert just created — index arithmetic this package otherwise
    avoids entirely. Use `docs_write` for formatted content; use this for
    appending a line to a running log.
    """
    document_id = document_id_from(document)
    docs_service = _docs(session, account_email, user_id)
    try:
        doc = docs_service.documents().get(
            documentId=document_id, fields="body(content(endIndex))",
        ).execute()
        index = end_index(doc)
        docs_service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{
                "insertText": {"location": {"index": index}, "text": text},
            }]},
        ).execute()
    except Exception as exc:
        raise _classify(exc, f"append to document {document_id}") from exc

    return {"document_id": document_id, "inserted_at_index": index, "characters": len(text)}


def replace_text(
    session: Session,
    *,
    document: str,
    find: str,
    replace: str,
    match_case: bool = True,
    account_email: str,
    user_id: int,
) -> dict:
    """Replace every occurrence of `find` with `replace`.

    Index-free — `replaceAllText` is resolved server-side, so this is the one
    targeted edit that needs no offset bookkeeping at all, and it is safe to
    run against a document being edited concurrently.
    """
    document_id = document_id_from(document)
    docs_service = _docs(session, account_email, user_id)
    try:
        result = docs_service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{
                "replaceAllText": {
                    "containsText": {"text": find, "matchCase": match_case},
                    "replaceText": replace,
                },
            }]},
        ).execute()
    except Exception as exc:
        raise _classify(exc, f"replace text in document {document_id}") from exc

    replies = result.get("replies") or [{}]
    occurrences = (replies[0].get("replaceAllText") or {}).get("occurrencesChanged", 0)
    return {"document_id": document_id, "occurrences_changed": int(occurrences)}
