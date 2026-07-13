"""Client bearer-token auth for the V3 HTTP API.

Validates an `Authorization: Bearer <token>` header against the
`client_tokens` table and returns the associated User row. Updates the
token's `last_seen_at` on every authenticated request.

Returns the full User instance so handlers can use either `.id` (for
filtering per-user data) or `.name` (for vault folder paths, log lines,
display strings) without re-querying.
"""

from datetime import datetime, timezone

from fastapi import Header, HTTPException, status

from app.db import get_db
from app.models.clients import ClientToken
from app.models.users import User


def resolve_token_to_user(token: str) -> User | None:
    """Resolve a raw bearer string to a detached User row, or None if invalid.

    Used by both the FastAPI dependency (`get_current_user`) and the MCP SSE
    transport in `app/mcp/server.py`, which can't use FastAPI's Depends.
    """
    if not token:
        return None

    db = get_db()
    with db.session() as session:
        row = (
            session.query(ClientToken)
            .filter_by(token=token, is_active=True)
            .first()
        )
        if not row:
            return None
        row.last_seen_at = datetime.now(timezone.utc)
        user = session.query(User).filter_by(id=row.user_id).first()
        if not user:
            return None
        # Snapshot attributes BEFORE commit — sessionmaker uses
        # expire_on_commit=True (default), so accessing user.id after
        # commit/close would trigger a refresh against a closed session.
        uid, uname, udisplay = user.id, user.name, user.display_name
        session.commit()

    return User(id=uid, name=uname, display_name=udisplay)


def get_current_user(authorization: str = Header(default="")) -> User:
    """Validate the Bearer token; return the authenticated User row.

    Raises HTTPException(401) on missing or invalid tokens.
    """
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = resolve_token_to_user(token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
