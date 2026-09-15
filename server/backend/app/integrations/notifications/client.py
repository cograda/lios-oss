"""Home Assistant mobile-app push client.

Was an ntfy JSON-publish client until 2026-08-13; now a thin wrapper over the
`homeassistant.notify` capability (`app.integrations.homeassistant.facade`),
which calls HA's `notify.<target>` service. The one real design decision left
here is routing: `user_id=None` means household-wide (fan out to every
configured `household_targets` entry), a real `user_id` means "this one
person's phone" (`targets[str(user_id)]`) — see `_resolve_targets`.

Historical note on why title/message are never trusted to HTTP headers: the
old ntfy path put the title in a `Title` header, and httpx encodes headers as
latin-1/ASCII — an em-dash in an alert title once crashed the publish with a
`UnicodeEncodeError` *before the request was sent*, causing 107 consecutive
scheduler failures. That bug was specific to ntfy's header-based publish
variant; it is moot now that HA service calls carry everything as a JSON
body (`call_service` in the homeassistant client posts `json=data` throughout)
— but the lesson (never put caller-supplied unicode text in a header) still
applies to any future sink, hence it stays written down.

The only other real work here is failure classification — a transient HA
outage must not look the same as a misconfiguration (missing target mapping),
because the scheduler retries one and not the other.
"""

from __future__ import annotations

import logging

from app.errors import PermanentError, TransientError
from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)

# HA mobile-app push payload per severity. `critical` asks iOS for a
# Critical Alert (bypasses Do Not Disturb / silent mode) plus ntfy-style high
# priority framing on Android via `priority`; `ttl: 0` means "deliver now or
# not at all" rather than queuing. `warning` sends HA's defaults — ordinary
# deriverstion should not interrupt anyone. `recovery` is reassurance, not
# news, so it asks for a passive/low-importance presentation.
#
# Critical Alerts require the household member to have granted the
# companion app "Critical Alerts" permission for this specific
# notify-service target (Settings -> Notifications -> Home Assistant on
# iOS). Without that grant, the notification still arrives — it just
# degrades to a normal alert that respects Do Not Disturb like any other.
# It does not fail; it quietly becomes a `warning`-shaped delivery.
_SEVERITY_DATA: dict[str, dict] = {
    "critical": {"push": {"interruption-level": "critical"}, "ttl": 0, "priority": "high"},
    "warning": {},
    "recovery": {"push": {"interruption-level": "passive"}, "importance": "low"},
}


class NotifyConfigError(PermanentError):
    """notifications isn't configured for the requested target(s).

    Permanent: retrying cannot fix a missing config key.
    """


def _resolve_targets(user_id: int | None) -> list[str]:
    """Return the list of HA notify-service targets for this send.

    This is the single enforcement point for config that the manifest
    deliberately does not mark `required` — see that module's docstring for
    why. The error names the exact keys so the SyncState row the scheduler
    writes is actionable rather than just "failed".
    """
    cfg = plugin_config("notifications")

    if user_id is None:
        targets = list(cfg.household_targets or [])
        if not targets:
            raise NotifyConfigError(
                "notifications is not configured: set household_targets via "
                "PUT /api/integrations/notifications/config"
            )
        return targets

    mapping = cfg.targets or {}
    target = mapping.get(str(user_id))
    if not target:
        raise NotifyConfigError(
            f"notifications is not configured for user {user_id}: add an "
            f"entry for \"{user_id}\" to targets via "
            "PUT /api/integrations/notifications/config"
        )
    return [target]


def current_topic() -> str:
    """A short label for the ledger's `topic` column.

    `notification_sends.topic` predates this sink swap and there's no
    migration in this pass (see the manifest docstring), so household sweeps
    still need *something* to stamp there. No longer an ntfy topic — this is
    just the configured household targets, comma-joined. Raises the same
    `NotifyConfigError` as `publish()` if `household_targets` is unset, which
    matches the strictness the old ntfy-backed `current_topic()` had (it also
    raised via `_resolve()` when unconfigured).
    """
    return ",".join(_resolve_targets(user_id=None))


def publish(
    title: str,
    body: str,
    severity: str = "warning",
    user_id: int | None = None,
    *,
    source: str = "adhoc",
    fingerprint: str | None = None,
    ledger: bool = True,
    extra_data: dict | None = None,
) -> None:
    """Push one message via Home Assistant's mobile-app notify service(s).

    `user_id=None` (the default) fans out to every configured
    `household_targets` entry — this is the sweep's only caller shape today
    (household-wide infrastructure alerts, no per-user attribution wired up
    on the send side — see `sweep.py`'s comment on why). A real `user_id`
    routes to that one person's `targets[str(user_id)]` device.

    Raises `TransientError` for anything worth retrying (network blip, 5xx,
    429) and `PermanentError` for anything that won't fix itself (missing
    config, or HA rejecting the call outright — bad token, unknown service).
    When fanning out to more than one target, a per-target failure is logged
    and skipped rather than aborting the whole send — one broken target
    should not silence the rest of the household; only if *every* target
    fails does this function raise, using the most useful error of the batch
    (a `TransientError` takes priority over a `PermanentError` when both
    occurred, since that's the one worth a scheduler retry).

    **Ledger every publish (added 2026-09-04)** — closes
    `vault/Projects/lios/Backlog.md`'s "Ad-hoc pushes are never ledgered"
    item. `ledger=True` (the default) writes one `notification_sends` row via
    `_record_send`, whatever the outcome, tagged with `source` (who called
    this — "tool", "household", "tasks", "inbox", the "adhoc" default, or
    "sweep") and `error_text` set on failure. `sweep.py` passes
    `ledger=False` because it already owns the fingerprint-keyed open-row
    lifecycle in `notification_sends` (`reconcile()`) and writes its own row
    around this call — a second write here would double-record every sweep
    send. `fingerprint` is accepted but only meaningful when `ledger=True`
    (an ad-hoc caller has none to give, hence the default `None`).

    A missing-config `NotifyConfigError` from `_resolve_targets` is ledgered
    too (targets=[] — there is nothing to fan out to yet) rather than only
    HA-level send failures: a misconfigured `targets`/`household_targets` is
    exactly the kind of failure the backlog item's "bare except around every
    producer" complaint was about, and it used to leave zero trace either way.
    """
    try:
        targets = _resolve_targets(user_id)
    except NotifyConfigError as exc:
        if ledger:
            _record_send(
                title, body, severity, user_id, source, fingerprint, targets=[],
                status="failed", error_text=str(exc)[:500],
            )
        raise
    data = dict(_SEVERITY_DATA.get(severity, {}))
    # `extra_data` (added for signals watchers, 2026-09-11) merges in caller-
    # supplied HA notify data — e.g. `{"entity_id": "camera.front_door..."}`
    # so the phone's notification shows the live camera view. Merged rather
    # than replacing the severity payload: a `critical` push that also wants
    # to attach a camera should keep its interruption-level.
    if extra_data:
        data.update(extra_data)
    notify = get_capability("homeassistant.notify")

    errors: list[Exception] = []
    delivered = False
    for target in targets:
        try:
            notify.notify(target, title, body, data)
            delivered = True
        except (TransientError, PermanentError) as exc:
            logger.warning("HA notify failed for target %s: %s", target, exc)
            errors.append(exc)

    if delivered or not errors:
        if ledger:
            _record_send(
                title, body, severity, user_id, source, fingerprint, targets,
                status="sent", error_text=None,
            )
        return

    transient = next((e for e in errors if isinstance(e, TransientError)), None)
    final_exc = transient or errors[0]
    if ledger:
        _record_send(
            title, body, severity, user_id, source, fingerprint, targets,
            status="failed", error_text=str(final_exc)[:500],
        )
    raise final_exc


def _record_send(
    title: str,
    body: str,
    severity: str,
    user_id: int | None,
    source: str,
    fingerprint: str | None,
    targets: list[str],
    *,
    status: str,
    error_text: str | None,
) -> None:
    """Write one ad-hoc `notification_sends` row — see `publish()`'s
    "Ledger every publish" note.

    A one-shot record, not the sweep's fingerprint-keyed lifecycle:
    `resolved_at` is set immediately, so this row never appears in
    `sweep.reconcile()`'s `WHERE resolved_at IS NULL` open-row query and
    cannot interact with its dedup/gating logic. Best-effort — a broken
    ledger write must never turn a delivered push into a raised exception,
    so failures here are logged and swallowed, never re-raised.
    """
    try:
        from datetime import datetime, timezone

        from app.db import get_db
        from app.integrations.notifications.models import NotificationSend

        now = datetime.now(timezone.utc)
        db = get_db()
        with db.session() as session:
            session.add(
                NotificationSend(
                    user_id=user_id,
                    fingerprint=fingerprint,
                    title=title,
                    body=body,
                    severity=severity,
                    topic=",".join(targets),
                    source=source,
                    status=status,
                    error_text=error_text,
                    first_seen_at=now,
                    last_sent_at=now if status == "sent" else None,
                    send_count=1 if status == "sent" else 0,
                    resolved_at=now,
                )
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to write notification_sends ledger row: %s", exc)

    _record_push_channel_health(status, error_text)


def _record_push_channel_health(status: str, error_text: str | None) -> None:
    """Feed the push channel's own delivery health into `sync_state` under
    the synthetic integration name `notifications_push` — distinct from the
    `notifications` row the sweep's own run health already writes
    (`sweep.run_sweep`). `system/tools.py::_build_alerts_payload`'s Axis 1
    already renders `consecutive_failures` for *every* row in `sync_state`
    generically, so this needs no change there: a channel that starts
    failing shows up as `notifications_push: failing (Nx consecutive)`
    without a new axis being written for it.

    Best-effort, same as the ledger write above.
    """
    try:
        from app.scheduler import _update_sync_state

        _update_sync_state(
            "notifications_push",
            status="ok" if status == "sent" else "error",
            error=error_text,
            trigger="push",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to record push channel health: %s", exc)
