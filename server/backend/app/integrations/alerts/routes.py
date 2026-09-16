"""`POST /api/v1/alerts/events` — Alertmanager's webhook receiver inlet.

Producer is Alertmanager (`deploy/monitoring/`), not a person, so — same
reasoning as `signals.routes` — this does NOT go through the dashboard
session or a per-user bearer. It authenticates with a per-route shared
secret instead, checked as a standard `Authorization: Bearer <key>` header
(Alertmanager's webhook receiver supports this natively via
`http_config.authorization`, so no `?key=` query-string fallback is needed
here the way `signals` needed one for a UniFi Protect webhook action that
couldn't set a custom header).

Nothing sensitive lands in the uvicorn access log for this route: the
access log format is method + path + status, and the bearer token never
appears in either (unlike `signals`, whose secret rode along in the query
string and needed its own redaction filter — see that module's docstring).

Every accepted delivery is stored whatever its shape (`parsing.parse_alerts`
skips only entries missing what the schema requires), matching
`signals.routes`'s "store first, ask questions later" stance — a fingerprint/
label shape this table doesn't have a named column for still lands in
`labels`/`annotations` verbatim.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.alerts.models import AlertEvent
from app.integrations.alerts.parsing import parse_alerts

# Imported as a module, never `from ... import plugin_config` — see
# `signals/routes.py`'s docstring for why: a test that patches
# `app.plugin.config_store.plugin_config` must reach the same name this
# module calls through.
from app.plugin import config_store as _config_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])


def _check_bearer(authorization: str | None) -> None:
    cfg = _config_store.plugin_config("alerts")
    configured = (cfg.alerts_inlet_key or "").strip()
    if not configured:
        # Fail closed, but distinctly from "wrong key" — an unconfigured
        # inlet is a deployment state, not an authentication failure, and
        # 503 lets the deploy/monitoring side tell the two apart.
        raise HTTPException(status_code=503, detail="alerts inlet not configured")
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[len("bearer "):].strip()
    if not presented or presented != configured:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def store_alert_events(session: Session, rows: list[dict]) -> int:
    """Upsert `rows` (from `parsing.parse_alerts`) with `ON CONFLICT DO
    NOTHING` on `(fingerprint, status, starts_at)` — the same triple
    Alertmanager repeats verbatim on every `repeat_interval` resend of a
    still-firing alert, so a resend is silently absorbed rather than
    creating a duplicate row. Returns how many rows were actually inserted
    (fewer than `len(rows)` on a repeat delivery)."""
    if not rows:
        return 0
    stmt = pg_insert(AlertEvent.__table__).values(rows).on_conflict_do_nothing(
        index_elements=["fingerprint", "status", "starts_at"],
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount or 0


@router.post("/events")
async def ingest(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    _check_bearer(authorization)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    rows = parse_alerts(body)

    db = get_db()
    with db.session() as session:
        inserted = store_alert_events(session, rows)

    return {"ok": True, "received": len(rows), "inserted": inserted}
