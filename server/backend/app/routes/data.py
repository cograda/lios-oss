"""Data management routes — embedding stats, reindex, purge."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import func, text, delete

from app.db import get_db
from app.services.embedding import EmbeddingService, EmbeddingQueue, Embedding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])


@router.get("/stats")
async def data_stats():
    """Per-integration row counts, DB size, and embedding coverage."""
    db = get_db()
    with db.session() as session:
        # Embedding stats from the service
        emb = EmbeddingService.stats(session)

        # Per-table row counts (approximate, fast)
        rows = session.execute(text(
            "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC"
        )).fetchall()
        tables = {r[0]: r[1] for r in rows}

        # DB size
        db_size = session.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar()

    return {
        "embeddings": emb,
        "tables": tables,
        "db_size_mb": round(db_size / (1024**2), 1) if db_size else 0,
    }


@router.post("/reindex-embeddings")
async def reindex_embeddings(source: str | None = None):
    """Re-queue embeddings for processing.

    If source is given, only re-queue items from that source.
    Deletes existing embeddings and re-queues the source content.
    """
    db = get_db()
    with db.session() as session:
        if source:
            # Delete existing embeddings for this source
            deleted = (
                session.query(Embedding)
                .filter_by(source=source)
                .delete(synchronize_session=False)
            )
            # Reset any error queue items back to pending
            session.query(EmbeddingQueue).filter_by(
                source=source, status="error"
            ).update({"status": "pending"}, synchronize_session=False)
            session.commit()
            logger.info(f"Reindex: cleared {deleted} embeddings for source={source}")
            return {
                "status": "ok",
                "message": f"Cleared {deleted} embeddings for '{source}'. "
                           "Content will be re-embedded on next sync cycle.",
                "cleared": deleted,
            }
        else:
            # Reset all error queue items
            reset = session.query(EmbeddingQueue).filter_by(
                status="error"
            ).update({"status": "pending"}, synchronize_session=False)
            session.commit()
            logger.info(f"Reindex: reset {reset} errored queue items to pending")
            return {
                "status": "ok",
                "message": f"Reset {reset} errored items to pending.",
                "reset": reset,
            }


@router.delete("/purge/{integration}")
async def purge_integration(integration: str, before: str | None = None):
    """Purge cached data for an integration.

    If 'before' is given (ISO date like 2025-01-01), only purge data
    older than that date. Otherwise purges all data for the integration.
    """
    # Map integration names to their tables and date columns
    PURGE_MAP: dict[str, list[tuple[str, str | None]]] = {
        "google_calendar": [("calendar_events", "start_time")],
        "google_mail": [("mail_messages", "date")],
        "lastfm": [("scrobbles", "played_at")],
        "whatsapp": [("whatsapp_messages", "timestamp"), ("whatsapp_contacts", None)],
        "weather": [("weather_current", None), ("weather_forecasts", None)],
        "finance": [("transactions", "date")],
        "obsidian": [("vault_chunks", None)],
    }

    if integration not in PURGE_MAP:
        return {"error": f"Cannot purge '{integration}'. Valid: {list(PURGE_MAP.keys())}"}

    cutoff = None
    if before:
        try:
            cutoff = datetime.fromisoformat(before).replace(tzinfo=timezone.utc)
        except ValueError:
            return {"error": f"Invalid date format: '{before}'. Use ISO format (YYYY-MM-DD)."}

    db = get_db()
    total_deleted = 0

    with db.session() as session:
        for table_name, date_col in PURGE_MAP[integration]:
            if cutoff and date_col:
                result = session.execute(
                    text(f"DELETE FROM {table_name} WHERE {date_col} < :cutoff"),
                    {"cutoff": cutoff},
                )
            else:
                result = session.execute(text(f"DELETE FROM {table_name}"))
            total_deleted += result.rowcount

        # Also clean up related embeddings
        source_map = {
            "google_mail": "email",
            "obsidian": "vault",
            "whatsapp": "whatsapp",
        }
        emb_source = source_map.get(integration)
        if emb_source:
            emb_deleted = (
                session.query(Embedding)
                .filter_by(source=emb_source)
                .delete(synchronize_session=False)
            )
            session.query(EmbeddingQueue).filter_by(
                source=emb_source
            ).delete(synchronize_session=False)
            total_deleted += emb_deleted

        session.commit()

    logger.info(f"Purge {integration}: deleted {total_deleted} rows (before={before})")
    return {
        "status": "ok",
        "integration": integration,
        "deleted": total_deleted,
        "before": before,
    }
