"""Dashboard data routes — aggregates from all integrations."""

import logging

from fastapi import APIRouter

from app.integrations import get_all

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
async def dashboard_summary():
    """Aggregated dashboard data from all active integrations."""
    integrations = get_all()
    data = {}

    for name, integration in integrations.items():
        if integration.is_configured():
            try:
                data[name] = await integration.dashboard_data()
            except Exception:
                logger.exception(f"Failed to get dashboard data for {name}")
                data[name] = {"error": f"Failed to load {integration.display_name}"}
        else:
            data[name] = {"status": "not_configured"}

    # Embedding queue stats (cross-integration) — reshape for frontend
    try:
        from app.db import get_db
        from app.services.embedding import EmbeddingService
        db = get_db()
        with db.session() as session:
            raw = EmbeddingService.stats(session)
            data["embedding_queue"] = {
                "done": raw.get("total_embeddings", 0),
                "pending": raw.get("queue_pending", 0),
                "processing": 0,  # Not tracked separately yet
                "errored": raw.get("queue_errors", 0),
                "sources": raw.get("by_source", {}),
            }
    except Exception:
        logger.exception("Failed to get embedding queue stats")
        data["embedding_queue"] = {"error": "Failed to load embedding stats"}

    # Surfacing dead OAuth tokens at the top of the dashboard. Stays on its own
    # key (not nested under any single integration) because one revoked token
    # typically breaks multiple syncs — calendar and mail share an account.
    try:
        from app.db import get_db
        from app.models.tokens import OAuthToken
        from app.models.users import User
        db = get_db()
        with db.session() as session:
            rows = (
                session.query(OAuthToken, User)
                .join(User, OAuthToken.user_id == User.id)
                .filter(OAuthToken.needs_reauth_at.isnot(None))
                .all()
            )
            data["reauth_needed"] = [
                {
                    "provider": t.provider,
                    "account_email": t.account_email,
                    "flagged_at": t.needs_reauth_at.isoformat() if t.needs_reauth_at else None,
                    "reason": t.needs_reauth_reason,
                    # google/login now requires ?user= — thread the owning
                    # user's name through so this link doesn't 422.
                    "reauth_url": f"/api/auth/google/login?account={t.account_email}&user={u.name}",
                }
                for t, u in rows
            ]
    except Exception:
        logger.exception("Failed to load reauth_needed list")
        data["reauth_needed"] = []

    return data
