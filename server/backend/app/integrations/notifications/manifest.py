"""Manifest for `notifications` — the push sink for system alerts.

Why this exists: `system_alerts` and the data-freshness probes have been
populated and rendered on the dashboard since 2026-07-15, but nothing ever
*pushed* them anywhere. Finding out an integration died still meant noticing
something looked wrong (the 2026-07-26 shed outage went unannounced for
hours). The transport side was already finished and unused — `infra/` runs a
private `homelab-ntfy` container with a `comar-notes` topic and a phone
subscription over Tailscale — so this package is deliberately only the missing
half: evaluate alerts on a cron, deduplicate, publish.

**2026-08-13: the sink moved from that ntfy topic to Home Assistant's
mobile-app push** (`homeassistant.notify` capability, `notify.<target>`
HA service calls) — same `notify.push` contract, same ledger, same sweep;
only `client.py`'s transport changed. Reason: one less standing service to
run, and HA's companion app already gives per-device routing (critical
alerts, per-user targets) for free instead of a single shared topic.

Type is `capability`, not `source`: nothing is pulled *from* HA push and there
is no external data to cache. What it owns is a *ledger* (`notification_sends`),
which is the whole design problem — see `models.py`.

**A second capability, `notify.email`, added 2026-08-29**: the "Dictator" Tines
automation being retired emailed the user a transcript (HTML body + a
`transcript.txt` attachment) whenever comar finished transcribing a voice
memo — comar can now do the transcription but had no way to send mail at
all (`google_mail` is read-only). Deliberately the same integration as
`notify.push`, not a new one: both are "tell a household member something,
best-effort, over a transport this package owns" — the facade class just
grows a second method. Transport is `email_client.py`, an SMTP sibling of
`client.py`; see that module's docstring for why stdlib `smtplib` and why
recipients are a config dict rather than a `User.email` column. Wiring
`notify.email` into the transcription pipeline is a separate, later change
— this one only adds the capability.

**2026-08-27: push-boundary gating** (`min_active_minutes` /
`refire_cooldown_minutes` / `quiet_hours` below) — a sleeping MacBook (lid
closed) was tripping `macbook:daemon_silent` and
`apple_reminders:data_stale`, each firing and self-resolving in 15-45
minutes, repeating every 30-60 minutes around the clock: ~20 pushes/day to
one phone for expected state, not an incident. See `sweep.py`'s module
docstring for the mechanism; the axes themselves are untouched.

Config is not marked `required` even though every code path here needs it.
`scheduler.py:189` gates an integration's sync job on `is_configured()`, but
`scheduler.py:217-219` gates cron `background_tasks` on `is_integration_enabled()`
*alone* — so `required=True` would hide the tools while the sweep kept firing
unconfigured. The call site has to check either way, so it checks and raises a
`PermanentError` naming the missing keys (recorded on SyncState, visible on the
dashboard), per the rule in the repo root `CLAUDE.md`.
"""

from app.plugin.manifest import ConfigFieldSpec, IntegrationManifest, TaskSpec

MANIFEST = IntegrationManifest(
    name="notifications",
    display_name="Notifications",
    version="1.0.0",
    type="capability",
    description="Pushes system alerts via Home Assistant mobile-app notifications, deduplicated against a send ledger.",
    icon="Bell",
    models=["NotificationSend"],
    embedding_sources=[],
    # HA is written to (a notify.<target> service call), never read from —
    # this integration has no cached data of its own.
    reads_from=[],
    writes_to=["homeassistant"],
    # No sync job: a `capability` has nothing to poll. The sweep below is a
    # cron `background_task` instead, which is also why it keeps running when
    # this integration is unconfigured (see the module docstring).
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    # No staleness probe: `notification_sends` only gets rows when something is
    # actually wrong. A quiet table is the healthy state, so "no recent rows"
    # must never read as stale — that would make the alerting system alert
    # about its own silence.
    staleness_probe=None,
    background_tasks=[
        TaskSpec(
            name="notifications_alert_sweep",
            target="app.integrations.notifications.sweep:run_sweep",
            kind="cron",
            # Every 15 minutes. The staleness thresholds this reads are
            # hour-scale, so a tighter cadence would only re-derive the same
            # fingerprints; a looser one delays first notice of a real outage.
            cron="*/15 * * * *",
        ),
    ],
    routes=[],
    config_schema={
        "targets": ConfigFieldSpec(
            type="dict_str_str",
            required=False,
            default={},
            description=(
                "Maps a user_id (as a string) to the Home Assistant notify "
                "service name for their device, e.g. "
                '{"1": "mobile_app_a_phone"}. Used for per-user routing '
                "(alerts attributable to a specific household member). No "
                "default — a real device name here would fail "
                "tests/test_personalisation_guard.py."
            ),
        ),
        "household_targets": ConfigFieldSpec(
            type="list_str",
            required=False,
            default=[],
            description=(
                "Home Assistant notify service names (without the 'notify.' "
                'prefix) that receive household-wide alerts, e.g. '
                '["mobile_app_a_phone", "mobile_app_b_phone"]. This is what '
                "the alert sweep (household-wide, no single owner) fans out "
                "to. No default for the same reason as `targets`."
            ),
        ),
        "threshold_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=60,
            description="Staleness threshold passed to the alerts capability.",
        ),
        "resend_after_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=1440,
            description=(
                "How long an alert stays quiet after being sent while it is "
                "still unresolved. Defaults to a daily reminder — the point of "
                "the ledger is that a broken integration notifies once, not "
                "every sweep."
            ),
        ),
        "suppress_push_for_user_ids": ConfigFieldSpec(
            type="list_str",
            required=False,
            default=[],
            description=(
                "User ids (as strings) whose personally-attributable alerts "
                "should not be pushed, e.g. [\"2\"]. The alert stays visible "
                "in system_alerts and on the dashboard — this only stops it "
                "ringing a phone. Intended for a household member whose stalled "
                "device nobody on the receiving end can act on."
            ),
        ),
        "sleep_deadline_user_ids": ConfigFieldSpec(
            type="list_str",
            required=False,
            default=[],
            description=(
                "User ids (as strings) to run the sleep-by-deadline check for. "
                "Empty (the default) disables it. See deadlines.py — this is a "
                "wall-clock deadline, not a staleness threshold."
            ),
        ),
        "sleep_deadline_hour": ConfigFieldSpec(
            type="int",
            required=False,
            default=10,
            description=(
                "Local hour by which last night's sleep data is expected. Past "
                "this hour with no session rows for the night, one alert is "
                "raised (once — the ledger dedupes it) and cleared "
                "automatically if the export lands later."
            ),
        ),
        "sleep_deadline_timezone": ConfigFieldSpec(
            type="str",
            required=False,
            default="Europe/Dublin",
            description=(
                "IANA timezone the deadline hour is read in. A deadline in UTC "
                "would drift by an hour across DST, making '10am' mean 09:00 "
                "for half the year."
            ),
        ),
        "notify_on_recovery": ConfigFieldSpec(
            type="bool",
            required=False,
            default=True,
            description=(
                "Also push a message when an alert clears, so a silent phone "
                "means 'healthy' rather than 'possibly still broken'."
            ),
        ),
        # --- Push-boundary gating (flap fix, 2026-08-27) -------------------
        # Detection is unchanged — the axes in `system/tools.py` keep firing
        # exactly as before. These three keys gate whether a *detected* alert
        # is allowed to reach a phone; see `sweep.py`'s module docstring for
        # why the boundary sits here and not in `collect()`. Critical
        # severity bypasses all three — a re-auth link or a genuine outage
        # must never be held back by a gate meant for a sleeping laptop.
        "min_active_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=30,
            description=(
                "A non-critical alert must be continuously active for this "
                "long before it may push at all. An alert that resolves "
                "before the gate elapses never pushes — the fix for a "
                "MacBook going to sleep for 15-45 minutes and 'daemon "
                "silent'/'data stale' flapping every 30-60 minutes all night."
            ),
        ),
        "refire_cooldown_minutes": ConfigFieldSpec(
            type="int",
            required=False,
            default=120,
            description=(
                "After a fingerprint's row resolves, a new firing of the "
                "same fingerprint will not push again until this many "
                "minutes have passed since that resolution — computed from "
                "the ledger's most recent resolved row for the fingerprint. "
                "Stops a flapping condition (resolve, refire, resolve, "
                "refire) from re-arming the persistence gate into a fresh "
                "push every cycle."
            ),
        ),
        "quiet_hours": ConfigFieldSpec(
            type="str",
            required=False,
            default="22:00-07:30",
            description=(
                "Non-critical pushes are held during this window (local "
                "time, 'HH:MM-HH:MM', wrapping midnight) and delivered once "
                "at window end if the alert is still active then — not a "
                "backlog dump of everything that flapped overnight. Empty "
                "string disables quiet hours entirely."
            ),
        ),
        "quiet_hours_timezone": ConfigFieldSpec(
            type="str",
            required=False,
            default="Europe/Dublin",
            description=(
                "IANA timezone `quiet_hours` is interpreted in — matches "
                "`sleep_deadline_timezone`'s reasoning: a fixed UTC window "
                "would drift an hour across DST."
            ),
        ),
        # --- notify.email (SMTP2GO), 2026-08-29 ----------------------------
        "smtp_host": ConfigFieldSpec(
            type="str",
            required=False,
            default="mail-eu.smtp2go.com",
            description="SMTP2GO SMTP hostname.",
        ),
        "smtp_port": ConfigFieldSpec(
            type="int",
            required=False,
            default=465,
            description="SMTP port. 465 is SMTP-over-SSL, what `email_client.py` speaks.",
        ),
        "smtp_username": ConfigFieldSpec(
            type="str",
            required=False,
            secret=True,
            description=(
                "SMTP2GO SMTP username. No default, same reasoning as the "
                "other unconfigured-by-default keys here: a real value would "
                "be a credential checked into a manifest."
            ),
        ),
        "smtp_password": ConfigFieldSpec(
            type="str",
            required=False,
            secret=True,
            description="SMTP2GO SMTP password. Fernet-encrypted at rest; no default.",
        ),
        "smtp_from_address": ConfigFieldSpec(
            type="str",
            required=False,
            default="alex@comar.ie",
            description=(
                "From: address for notify.email sends. comar.ie is already "
                "DKIM-verified with SPF configured for this account, so mail "
                "from it doesn't land in spam."
            ),
        ),
        "email_targets": ConfigFieldSpec(
            type="dict_str_str",
            required=False,
            default={},
            description=(
                "Maps a user_id (as a string) to an email address, e.g. "
                '{"1": "alex@comar.ie"}. There is no `email` column on the '
                "User model — see `email_client.py`'s module docstring for "
                "why this lives here instead. No default for the same "
                "reason `targets` has none: a real address here would fail "
                "tests/test_personalisation_guard.py."
            ),
        ),
    },
    oauth=None,
    provides=["notify.push", "notify.email"],
    # `health.query` is for the sleep deadline watch (deadlines.py), not for
    # anything alert-sweep related. ⚠️ The direction matters: `apple_health`
    # cannot depend on `notify.push` instead, because `notifications` →
    # `system.alerts` → `health.query` would close a cycle that
    # `app/plugin/validate.py` rejects at boot. The package that pushes has to
    # be the one that pulls.
    depends_on=["system.alerts", "homeassistant.notify", "health.query"],
)
