"""`POST /api/v1/signals/{source}` — the generic camera/sensor inlet.

Producer is a device (UniFi Protect's Alarm Manager), not a person, so this
does NOT go through the dashboard session or the per-user bearer
(`get_current_user`) — it authenticates with a per-SOURCE shared secret
instead, the same shape `inbox`'s retired `inbox_token` used to be, but kept
narrowly scoped to this one route rather than becoming a second general
credential. Accepted as `?key=` query param OR `X-Signal-Key` header, because
Protect's webhook action may not let Alex set a custom header.

Every accepted hit is stored, whatever its shape — see `protect.py`'s
module docstring for why an unrecognised payload is `kind="unknown"`, not a
rejection. The HTTP response never waits on vision: dispatch to watchers
happens via a `BackgroundTasks` entry.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.integrations.signals.models import SignalEvent
from app.integrations.signals.protect import normalize_device_key, parse_protect

# Imported as a module, never `from ... import plugin_config` — see
# vision/facade.py's module docstring for why: a test that patches
# `app.plugin.config_store.plugin_config` must reach the same name this
# module calls through.
from app.plugin import config_store as _config_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/signals", tags=["signals"])

_PARSERS = {
    "protect": parse_protect,
}


def normalize_devices_config(devices: dict | None) -> dict[str, str]:
    """`HOME_SIGNALS_DEVICES`/`signals_devices` config, keyed the same way
    payload device identifiers now are (`normalize_device_key`) — applied at
    every load, not just once at startup, since config can change without a
    restart via the `integration_config` table. A colon-form config entry
    (`a8:9c:6c:b0:3b:50`, copied from HA's device registry) and a colonless
    payload MAC (`A89C6CB03B50`, as Protect actually sends it) only match
    once both sides go through the same normaliser."""
    if not devices:
        return {}
    out: dict[str, str] = {}
    for raw_key, name in devices.items():
        key = normalize_device_key(raw_key)
        if key is not None:
            out[key] = name
    return out


def resolve_device_name(device_key: str | None, devices: dict) -> str | None:
    """`devices` (already normalised via `normalize_devices_config`) maps a
    device key to its human name. `None` in, `None` out — a payload shape we
    don't recognise may carry no identifiable device at all."""
    if not device_key:
        return None
    return devices.get(device_key)


# Matches the uvicorn access log's request line for this integration's own
# routes, e.g. `GET /api/v1/signals/protect?key=s3cr3t HTTP/1.1` — anything
# after `key=` up to the next `&`, space, or quote.
_KEY_QUERY_RE = re.compile(r"key=[^&\s\"']+")


class _RedactSignalKeyFilter(logging.Filter):
    """Installed on `uvicorn.access` at app startup (see `install_log_filters`
    below). Uvicorn logs the request line verbatim, including
    `?key=<the shared secret _check_key() checks>` — the same value that
    guards this route. Redact just the value, not the whole record, so the
    access log still shows what happened (method, path, status) without
    printing a live credential to disk/console."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                _KEY_QUERY_RE.sub("key=…", arg) if isinstance(arg, str) and "key=" in arg else arg
                for arg in record.args
            )
        if isinstance(record.msg, str) and "key=" in record.msg:
            record.msg = _KEY_QUERY_RE.sub("key=…", record.msg)
        return True


_filter_installed = False


def install_log_filters() -> None:
    """Idempotent — safe to call from app startup even if hit more than
    once (e.g. reload)."""
    global _filter_installed
    if _filter_installed:
        return
    logging.getLogger("uvicorn.access").addFilter(_RedactSignalKeyFilter())
    _filter_installed = True


def _check_key(source: str, key_query: str | None, key_header: str | None) -> None:
    cfg = _config_store.plugin_config("signals")
    configured = (cfg.signals_protect_key or "").strip()
    if not configured:
        # Fail closed: no configured secret means the route accepts nothing,
        # never everything.
        raise HTTPException(status_code=401, detail="signals inlet not configured")
    presented = (key_query or key_header or "").strip()
    if not presented or presented != configured:
        raise HTTPException(status_code=401, detail="invalid or missing signal key")


@router.post("/{source}")
async def ingest(
    source: str,
    request: Request,
    background_tasks: BackgroundTasks,
    key: str | None = Query(default=None),
    x_signal_key: str | None = Header(default=None, alias="X-Signal-Key"),
) -> dict:
    _check_key(source, key, x_signal_key)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}

    parser = _PARSERS.get(source)
    if parser is not None:
        parsed = parser(body)
        kind, device_key, occurred_at, sender_event_id, sources_device_keys = (
            parsed.kind, parsed.device_key, parsed.occurred_at, parsed.sender_event_id,
            parsed.sources_device_keys,
        )
    else:
        from datetime import datetime, timezone
        kind, device_key, occurred_at, sender_event_id = "unknown", None, datetime.now(timezone.utc), None
        sources_device_keys = []

    cfg = _config_store.plugin_config("signals")
    devices = normalize_devices_config(cfg.signals_devices)
    device_name = resolve_device_name(device_key, devices)

    # Sources fallback (added after a real "Test Alarm": Protect's trigger
    # carries a placeholder device — `FAKE_MAC` — rather than the camera
    # that's actually configured on the alarm). If the trigger device didn't
    # resolve to a known device, and exactly one of the alarm's own
    # `sources[]` devices IS known, trust that instead: it's the only
    # unambiguous fallback (two known sources would be a guess). `device_key`
    # keeps the trigger's own (raw, normalised) value — only `device_name`
    # and the `resolved_via` marker come from sources.
    resolved_via: str | None = None
    if device_name is None:
        known_sources = [k for k in sources_device_keys if k in devices]
        if len(known_sources) == 1:
            device_name = devices[known_sources[0]]
            resolved_via = "sources"

    if device_name is None and (device_key or sources_device_keys):
        logger.info(
            "[signals] unknown device trigger=%r sources=%r on source %r",
            device_key, sources_device_keys, source,
        )

    payload = body
    if resolved_via is not None:
        # Copy rather than mutate `body` in place — `body` is also what a
        # future re-parse of the raw request would see, and this marker is
        # synthesised by us, not sent by Protect.
        payload = {**body, "_signals_resolved_via": resolved_via}

    db = get_db()
    event_id: int
    with db.session() as session:
        event = SignalEvent(
            source=source,
            kind=kind,
            device_key=device_key,
            device_name=device_name,
            occurred_at=occurred_at,
            sender_event_id=sender_event_id,
            payload=payload,
        )
        session.add(event)
        session.flush()
        event_id = event.id
        session.commit()

    background_tasks.add_task(_dispatch, event_id)

    return {"ok": True, "id": event_id, "kind": kind, "device_name": device_name}


async def _dispatch(event_id: int) -> None:
    """Hand the stored event to every registered watcher whose `matches()`
    says yes. Runs as a FastAPI background task (awaited after the response
    is sent) — the settle delay and the vision call never block the webhook
    caller (Protect itself).
    """
    try:
        from app.integrations.signals.watchers.registry import WATCHERS
    except Exception:  # noqa: BLE001
        logger.exception("[signals] failed to import watcher registry")
        return

    db = get_db()
    with db.session() as session:
        event = session.get(SignalEvent, event_id)
        if event is None:
            return
        matched = [w for w in WATCHERS if w.matches(event)]
        occurred_at = event.occurred_at

    for watcher in matched:
        try:
            with db.session() as session:
                window = watcher.current_window(occurred_at)
                if window is None:
                    continue
                run = watcher.get_or_open_run(session, window)
                await watcher.check(session, run, wait_settle=True)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[signals] watcher %s failed handling event %s", watcher.name, event_id
            )
