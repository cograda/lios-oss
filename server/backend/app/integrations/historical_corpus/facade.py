"""historical_corpus's declared facade — capability `corpus.ingest` (V4
chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.historical_corpus`. Two consumers:
  - `attachments` (parses WhatsApp/email attachments itself, then reuses
    the corpus's own upsert + parser dispatch so the result lands in the
    same `historical_corpus` embedding source with no extra wiring)
  - `inbox` (routes vault Inbox files straight through `ingest_path`, the
    corpus's own suffix-dispatched ingest entrypoint)

`upsert_document`/the parser modules/`DocMeta` were previously imported as
package internals (`_upsert_document`, `app.integrations.historical_corpus.
parsers.*`) — re-exported here under their real names so this facade is a
thin, explicit surface rather than a second copy of the logic.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.integrations.historical_corpus.ingest import (
    _upsert_document,
    default_project_tags,
    ingest_path,
)
from app.integrations.historical_corpus.models import HistoricalDocument
from app.integrations.historical_corpus.parsers import boq, docx, pdf
from app.integrations.historical_corpus.parsers.types import ChunkRecord, DocMeta


class HistoricalCorpusFacade:
    # Parser modules + shared types, re-exported for callers that dispatch
    # by mimetype/suffix themselves (e.g. attachments/ingest.py's
    # MIME_TO_PARSER table).
    boq_parser = boq
    docx_parser = docx
    pdf_parser = pdf
    DocMeta = DocMeta
    ChunkRecord = ChunkRecord

    def upsert_document(
        self,
        session: Session,
        *,
        source_path: str,
        meta: DocMeta,
        chunks: list[ChunkRecord],
        project_tags: list[str] | None = None,
        owner_user_id: int | None = None,
    ) -> tuple[HistoricalDocument, bool, int]:
        """Upsert a document + its chunks. Returns (doc, created, chunks_enqueued).

        `project_tags=None` applies this deployment's configured default
        (`historical_corpus.default_project_tag`) rather than a hardcoded
        project name — that's what callers with no opinion should pass.

        `owner_user_id=None` (default) keeps the document household-shared;
        pass a user id only for something that is genuinely one person's —
        see models.py's ownership note. Attachments and inbox items stay
        shared: that is the corpus's contract with both users.
        """
        return _upsert_document(
            session,
            source_path=source_path,
            meta=meta,
            chunks=chunks,
            project_tags=project_tags or default_project_tags(),
            owner_user_id=owner_user_id,
        )

    def ingest_path(
        self, session: Session, path: Path, *,
        project_tags: list[str] | None = None,
        owner_user_id: int | None = None,
    ) -> dict:
        return ingest_path(session, path, project_tags=project_tags, owner_user_id=owner_user_id)


FACADE = HistoricalCorpusFacade()
