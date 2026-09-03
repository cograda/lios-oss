"""Client log viewer routes — query and tail shipped logs."""

from fastapi import APIRouter

from app.db import get_db
from app.models.clients import ClientLog
from app.models.users import User
from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike

router = APIRouter(prefix="/logs", tags=["logs"])


def _resolve_user_id(session, user_name: str | None) -> int | None:
    """Resolve a `?user=name` query param to a user_id. None if not provided."""
    if not user_name:
        return None
    row = session.query(User).filter_by(name=user_name).first()
    return row.id if row else -1  # -1 → query yields no rows for unknown user


def _serialize(rows: list[tuple[ClientLog, str]]) -> list[dict]:
    return [
        {
            "id": r.id,
            "user": user_name,
            "user_id": r.user_id,
            "level": r.level,
            "logger": r.logger_name,
            "message": r.message,
            "logged_at": r.logged_at.isoformat() if r.logged_at else None,
            "client_version": r.client_version,
        }
        for r, user_name in rows
    ]


@router.get("/")
async def query_logs(
    user: str | None = None,
    level: str | None = None,
    search: str | None = None,
    before: str | None = None,
    limit: int = 100,
):
    """Query client logs with optional filters. Returns newest first."""
    db = get_db()
    with db.session() as session:
        q = (
            session.query(ClientLog, User.name)
            .join(User, ClientLog.user_id == User.id)
            .order_by(ClientLog.logged_at.desc())
        )

        uid = _resolve_user_id(session, user)
        if uid is not None:
            q = q.filter(ClientLog.user_id == uid)
        if level:
            levels = [l.strip().upper() for l in level.split(",")]
            q = q.filter(ClientLog.level.in_(levels))
        if search:
            q = q.filter(
                ClientLog.message.ilike(f"%{escape_ilike(search)}%", escape=ILIKE_ESCAPE_CHAR)
            )
        if before:
            from datetime import datetime, timezone
            try:
                ts = datetime.fromisoformat(before).replace(tzinfo=timezone.utc)
                q = q.filter(ClientLog.logged_at < ts)
            except ValueError:
                pass

        rows = q.limit(min(limit, 500)).all()
        return {"logs": _serialize(rows)}


@router.get("/tail")
async def tail_logs(after_id: int = 0, user: str | None = None, level: str | None = None):
    """Return new log entries since a given ID. For polling-based tail."""
    db = get_db()
    with db.session() as session:
        q = (
            session.query(ClientLog, User.name)
            .join(User, ClientLog.user_id == User.id)
            .filter(ClientLog.id > after_id)
            .order_by(ClientLog.id.asc())
        )

        uid = _resolve_user_id(session, user)
        if uid is not None:
            q = q.filter(ClientLog.user_id == uid)
        if level:
            levels = [l.strip().upper() for l in level.split(",")]
            q = q.filter(ClientLog.level.in_(levels))

        rows = q.limit(200).all()
        return {"logs": _serialize(rows)}


@router.get("/users")
async def log_users():
    """Return distinct users that have shipped logs."""
    from sqlalchemy import distinct
    db = get_db()
    with db.session() as session:
        ids = session.query(distinct(ClientLog.user_id)).all()
        names = (
            session.query(User.name)
            .filter(User.id.in_([i[0] for i in ids if i[0] is not None]))
            .all()
        )
        return {"users": [n[0] for n in names]}
