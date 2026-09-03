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


def publish(title: str, body: str, severity: str = "warning", user_id: int | None = None) -> None:
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
    """
    targets = _resolve_targets(user_id)
    data = _SEVERITY_DATA.get(severity, {})
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
        return

    transient = next((e for e in errors if isinstance(e, TransientError)), None)
    raise transient or errors[0]
