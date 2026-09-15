"""Data management routes — embedding stats, reindex, purge."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.auth.ui_session import require_admin
from fastapi.responses import JSONResponse
from sqlalchemy import func, text, delete

from app.db import get_db
from app.models.users import User  # noqa: F401 — also used by purge
from app.services.embedding import EmbeddingService, EmbeddingQueue, Embedding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])

# Map integration names to their tables and date columns for purge.
PURGE_MAP: dict[str, list[tuple[str, str | None]]] = {
    "google_calendar": [("calendar_events", "start_time")],
    "google_mail": [("mail_messages", "date")],
    "lastfm": [("scrobbles", "played_at")],
    "whatsapp": [("whatsapp_messages", "timestamp"), ("whatsapp_contacts", None)],
    # F-security: was "weather_forecasts" (plural) — that table has never
    # existed (the model's __tablename__ is "weather_forecast", singular),
    # so purging "weather" always errored on the second DELETE before this fix.
    "weather": [("weather_current", None), ("weather_forecast", None)],
    "finance": [("transactions", "date")],
    "obsidian": [("vault_chunks", None)],
}

# Tables with no `user_id` column — household-shared, no UserOwnedMixin.
# Every other table named in PURGE_MAP is per-user. Kept as an explicit set
# (rather than introspecting the model) so this route's household/per-user
# split is visible in one place and reviewable on its own.
SHARED_TABLES = {
    "calendar_events",
    "whatsapp_contacts",
    "weather_current",
    "weather_forecast",
    "transactions",
}


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
async def reindex_embeddings(
    source: str | None = None, _admin: User = Depends(require_admin),
):
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
async def purge_integration(
    integration: str,
    before: str | None = None,
    user: str | None = None,
    household: bool = False,
    _admin: User = Depends(require_admin),
):
    """Purge cached data for an integration.

    If 'before' is given (ISO date like 2025-01-01), only purge data
    older than that date. Otherwise purges all data for the integration.

    Admin only (`require_admin`, 2026-09-06). F-security (household-wide
    DELETE with no user_id): the signed-in admin is bound as
    `current_user_id()` now, but a purge must never quietly default to the
    *admin's* rows — the target user is still named explicitly (see
    `app/auth/context.py` on why defaulting a user id is banned everywhere
    else). So:

    - A table with a `user_id` column (`mail_messages`, `scrobbles`,
      `whatsapp_messages`, `vault_chunks`) is only ever purged for the user
      named by `?user=<name>` — required whenever the integration owns any
      such table, resolved against the `users` table, never guessed.
    - A table with no `user_id` column at all (`calendar_events`,
      `whatsapp_contacts`, `weather_current`/`weather_forecast`,
      `transactions` — genuinely shared, not per-user data with the column
      missing) is only purged when the caller passes `?household=true`,
      naming the fact that this deletes it for everyone.
    - Neither flag deletes anything for the tables it doesn't cover; the
      response says which tables were purged and which were skipped and why.
    """
    if integration not in PURGE_MAP:
        return {"error": f"Cannot purge '{integration}'. Valid: {list(PURGE_MAP.keys())}"}

    cutoff = None
    if before:
        try:
            cutoff = datetime.fromisoformat(before).replace(tzinfo=timezone.utc)
        except ValueError:
            return {"error": f"Invalid date format: '{before}'. Use ISO format (YYYY-MM-DD)."}

    tables = PURGE_MAP[integration]
    owned_tables = [(t, c) for t, c in tables if t not in SHARED_TABLES]
    shared_tables = [(t, c) for t, c in tables if t in SHARED_TABLES]

    db = get_db()
    user_id: int | None = None

    if owned_tables:
        if not user:
            return JSONResponse(
                {
                    "error": (
                        f"'{integration}' has per-user data "
                        f"({', '.join(t for t, _ in owned_tables)}). "
                        "Pass ?user=<name> naming whose rows to purge."
                    )
                },
                status_code=400,
            )
        with db.session() as session:
            user_row = session.query(User).filter_by(name=user).first()
            if not user_row:
                return JSONResponse(
                    {"error": f"unknown user '{user}' — must exist in users table"},
                    status_code=400,
                )
            user_id = user_row.id

    if shared_tables and not household and not owned_tables:
        # Nothing this call is allowed to touch — refuse outright rather
        # than report success having deleted zero rows.
        return JSONResponse(
            {
                "error": (
                    f"'{integration}' purges household-shared data "
                    f"({', '.join(t for t, _ in shared_tables)}). "
                    "Pass ?household=true to confirm — this deletes it for everyone."
                )
            },
            status_code=400,
        )

    total_deleted = 0
    purged_tables: list[str] = []
    skipped_tables: list[str] = [t for t, _ in shared_tables] if not household else []

    with db.session() as session:
        for table_name, date_col in owned_tables:
            params: dict = {"uid": user_id}
            where = "user_id = :uid"
            if cutoff and date_col:
                where += f" AND {date_col} < :cutoff"
                params["cutoff"] = cutoff
            result = session.execute(
                text(f"DELETE FROM {table_name} WHERE {where}"), params
            )
            total_deleted += result.rowcount
            purged_tables.append(table_name)

        if household:
            for table_name, date_col in shared_tables:
                if cutoff and date_col:
                    result = session.execute(
                        text(f"DELETE FROM {table_name} WHERE {date_col} < :cutoff"),
                        {"cutoff": cutoff},
                    )
                else:
                    result = session.execute(text(f"DELETE FROM {table_name}"))
                total_deleted += result.rowcount
                purged_tables.append(table_name)

        # Also clean up related embeddings — scoped to the same user_id as
        # the source rows just purged (Embedding.user_id NULL means
        # household-shared, so it's never touched by a per-user purge).
        source_map = {
            "google_mail": "email",
            "obsidian": "vault",
            "whatsapp": "whatsapp",
        }
        emb_source = source_map.get(integration)
        if emb_source and user_id is not None:
            emb_deleted = (
                session.query(Embedding)
                .filter_by(source=emb_source, user_id=user_id)
                .delete(synchronize_session=False)
            )
            session.query(EmbeddingQueue).filter_by(
                source=emb_source, user_id=user_id
            ).delete(synchronize_session=False)
            total_deleted += emb_deleted

        session.commit()

    logger.info(
        f"Purge {integration}: deleted {total_deleted} rows "
        f"(before={before}, user={user}, household={household}, "
        f"purged={purged_tables}, skipped={skipped_tables})"
    )
    return {
        "status": "ok",
        "integration": integration,
        "deleted": total_deleted,
        "before": before,
        "user": user,
        "household": household,
        "purged_tables": purged_tables,
        "skipped_tables": skipped_tables,
    }
