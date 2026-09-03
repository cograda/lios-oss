"""Home Assistant WebSocket event listener — gap-free state history.

Long-running asyncio task (started from the FastAPI lifespan):
connect → auth → subscribe to state_changed → reconcile → apply events.

The reconcile step runs a full poll sync on every (re)connect, so any
transitions missed while disconnected are caught by change-detection —
that is what makes the stream gap-free. The scheduled 5-min poll stays
as a second reconciler + area-map refresher; both paths are idempotent
against each other (no state diff → no duplicate history row).
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

from app.db import get_db
from app.plugin.config_store import plugin_config
from app.integrations.homeassistant.models import HAEntity, HAStateChange
from app.integrations.homeassistant.sync import (
    _parse_ts,
    should_record_transition,
    sync_home_assistant,
)

logger = logging.getLogger(__name__)

_BACKOFF_MAX = 60.0

# Set by the lifespan; lets dashboard_data report stream health.
current_listener: "HAEventListener | None" = None


async def run_listener_task(stop_event: asyncio.Event) -> None:
    """Startup-task entry point (manifest `background_tasks`, kind="startup").

    Supervised by `app.plugin.supervisor`. `HAEventListener` already owns its
    own reconnect-with-backoff loop internally (see `_run` below) — this
    wrapper just starts it, publishes it as `current_listener` (so
    `ws_last_event_at()` keeps working for the freshness probe), waits for
    `stop_event`, then stops it. No-op if HA isn't configured, matching the
    pre-3.1 `if settings.ha_url and settings.ha_token:` guard in main.py.
    """
    global current_listener

    cfg = plugin_config("homeassistant")
    if not (cfg.ha_url and cfg.ha_token):
        await stop_event.wait()
        return

    listener = HAEventListener()
    current_listener = listener
    listener.start()
    try:
        await stop_event.wait()
    finally:
        await listener.stop()
        current_listener = None


def ws_last_event_at() -> "datetime | None":
    """Last time the WS listener actually received a state_changed event.

    None if the listener hasn't started, or hasn't seen an event yet.
    Used by `services.data_freshness` to detect a listener that's silently
    stopped receiving events (task alive/reconnect loop running, but no
    events flowing) — distinct from `current_listener.connected`, which
    only reflects the socket handshake.
    """
    return current_listener.last_event_at if current_listener else None


def _ws_url() -> str:
    base = plugin_config("homeassistant").ha_url.rstrip("/")
    scheme = "wss" if base.startswith("https") else "ws"
    return f"{scheme}://{base.split('://', 1)[1]}/api/websocket"


def apply_state_event(data: dict) -> None:
    """Apply one state_changed event payload to Postgres (sync, own session)."""
    entity_id = data.get("entity_id")
    if not entity_id:
        return
    new = data.get("new_state")
    old = data.get("old_state")

    db = get_db()
    with db.session() as session:
        row = session.query(HAEntity).filter_by(entity_id=entity_id).first()

        if new is None:  # entity removed from HA
            if row is not None:
                session.delete(row)
            session.commit()
            return

        state = new.get("state")
        attrs = new.get("attributes") or {}
        last_changed = _parse_ts(new.get("last_changed"))
        old_state = old.get("state") if old else (row.state if row else None)

        if old is not None and old_state != state and should_record_transition(
            old_state, state, entity_id
        ):
            session.add(
                HAStateChange(
                    entity_id=entity_id,
                    old_state=old_state,
                    new_state=state,
                    changed_at=last_changed or datetime.now(timezone.utc),
                    attributes=attrs,
                )
            )

        if row is None:
            row = HAEntity(entity_id=entity_id)
            session.add(row)
            # area comes from the poll's area map; leave None until then

        row.domain = entity_id.split(".", 1)[0]
        row.friendly_name = attrs.get("friendly_name")
        row.device_class = attrs.get("device_class")
        row.unit = attrs.get("unit_of_measurement")
        row.state = state
        row.attributes = attrs
        row.last_changed = last_changed
        row.synced_at = datetime.now(timezone.utc)
        session.commit()


class HAEventListener:
    """Reconnecting subscriber for HA state_changed events."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.connected = False
        # Heartbeat: stamped every time a state_changed event is actually
        # received off the socket (not just "TCP connected" — a listener
        # can sit connected-but-silent, e.g. subscribed but HA stops
        # pushing). This is the signal data_freshness uses to detect a
        # dead-but-connected listener, which `connected` alone can't catch.
        self.last_event_at: datetime | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ha-event-listener")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self.connected = False

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._session()
                backoff = 1.0  # clean close → quick reconnect
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "HA event stream error (%s: %s); reconnecting in %.0fs",
                    type(e).__name__, e, backoff,
                )
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _session(self) -> None:
        async with websockets.connect(
            _ws_url(), max_size=16 * 1024 * 1024
        ) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"unexpected HA hello: {msg.get('type')}")
            await ws.send(json.dumps(
                {"type": "auth", "access_token": plugin_config("homeassistant").ha_token}
            ))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"HA websocket auth failed: {msg}")

            await ws.send(json.dumps(
                {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
            ))
            msg = json.loads(await ws.recv())
            if not msg.get("success"):
                raise RuntimeError(f"HA subscribe_events failed: {msg}")

            self.connected = True
            logger.info("HA event stream connected; reconciling")

            # Gap fill: anything that changed while we were disconnected is
            # picked up here by the poll's change-detection. sync_home_assistant
            # is a plain blocking function (sync SQLAlchemy + sync httpx); this
            # listener loop is a genuine asyncio task (websockets), so bridge
            # explicitly via to_thread to avoid stalling the event loop / the
            # ws.recv() calls below while the HTTP fetch + DB upsert run.
            db = get_db()
            with db.session() as session:
                await asyncio.to_thread(sync_home_assistant, session)

            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue
                data = msg.get("event", {}).get("data", {})
                self.last_event_at = datetime.now(timezone.utc)
                await asyncio.to_thread(apply_state_event, data)
