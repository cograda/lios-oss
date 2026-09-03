"""homeassistant's declared facade — capabilities `homeassistant.entities`
and `homeassistant.notify` (V4 chunk 4.2).

The only surface another integration is allowed to import from
`app.integrations.homeassistant`. Consumers: `system` (home status
snapshot for the morning briefing), `commute` (publishing
`sensor.commute_*` state back into HA after each morning solve — a write,
despite the capability name being about "entities" broadly rather than
read-only), and `notifications` (`homeassistant.notify` — pushes to a
mobile-app `notify.<target>` service in place of the old ntfy sink). Derivers
built on `app.algo` read `numeric_history()` for features and ground truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.homeassistant import client as _client
from app.integrations.homeassistant.tools import handle_history, handle_home_status


class HomeAssistantFacade:
    def home_status(self, session: Session, arguments: dict[str, Any]) -> str:
        return handle_home_status(session, arguments)

    def history(self, session: Session, arguments: dict[str, Any]) -> str:
        """State-change history for one entity (appliance cycles etc.)."""
        return handle_history(session, arguments)

    def set_state(self, entity_id: str, state: Any, attributes: dict | None = None) -> bool:
        # Module-attribute lookup (not a bound-at-import-time name) so
        # tests that monkeypatch/patch `app.integrations.homeassistant.
        # client.set_state` still take effect through this facade.
        return _client.set_state(entity_id, state, attributes)

    def notify(
        self, target: str, title: str, message: str, data: dict | None = None
    ) -> bool:
        """Push via a HA mobile-app notify service.

        `target` is a notify service name WITHOUT the `notify.` prefix (e.g.
        `mobile_app_a_phone`) — HA's service-call API takes the service name
        alone, with `notify` as the fixed domain. Raises `TransientError`/
        `PermanentError` exactly as `call_service` does; this facade adds no
        error handling of its own so the caller (`notifications/client.py`)
        keeps its existing classification contract intact.

        ⚠️ `data` is NESTED under the `data` key, never spread into the
        payload root. HA's `notify.mobile_app_*` schema accepts exactly
        `message`, `title`, `target` and `data`; any other root key is
        `extra keys not allowed` and the whole call comes back HTTP 400,
        which `call_service` (correctly) classifies as permanent — so the
        push is dropped, not retried. This spread `data` until 2026-08-22,
        which silently killed every `critical` and `recovery` push for
        weeks. `warning` alone survived, because its severity payload is
        `{}` and spreading an empty dict is a no-op — so the one severity
        anybody sends by hand looked healthy while real alerts vanished.
        An empty `data` is omitted rather than sent as `{}`, matching the
        pre-2026-08-22 wire shape for `warning` exactly.
        """
        payload: dict = {"title": title, "message": message}
        if data:
            payload["data"] = dict(data)
        return _client.call_service("notify", target, payload)


    def numeric_history(
        self,
        session: Session,
        entity_id: str,
        start: "datetime",
        end: "datetime",
    ) -> list[tuple["datetime", float]]:
        """Recorded numeric readings for one entity, oldest first.

        Added for the algo harness: a deriver's `features()` is called with a
        *historical* `made_at` during training and must return what would have
        been known then, so it needs a table that keeps history. `ha_entities`
        holds only the latest state per entity — reading it from a feature
        builder would make every training row see today's value while claiming
        to be last March, and the model would score beautifully and predict
        nothing.

        Two caveats a caller has to know:

        ⚠️ `ha_state_changes` records numeric→numeric transitions **only when
        `ha_record_numeric_history` is true**, and it defaults to false. On a
        deployment where it has never been set there is no numeric history at
        all, and this correctly returns an empty list — which a trainer reads
        as `insufficient_rows`, not as a bug.

        Non-numeric states (`unavailable`, `unknown`, which HA writes
        routinely) are skipped rather than coerced: `float("unavailable")`
        raises, and treating them as zero would drop a hard zero into the
        middle of a temperature series.
        """
        from app.integrations.homeassistant.models import HAStateChange

        rows = (
            session.query(HAStateChange.changed_at, HAStateChange.new_state)
            .filter(
                HAStateChange.entity_id == entity_id,
                HAStateChange.changed_at >= start,
                HAStateChange.changed_at <= end,
            )
            .order_by(HAStateChange.changed_at.asc())
            .all()
        )
        out: list[tuple[datetime, float]] = []
        for changed_at, state in rows:
            try:
                out.append((changed_at, float(state)))
            except (TypeError, ValueError):
                continue
        return out


FACADE = HomeAssistantFacade()
