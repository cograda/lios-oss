"""The alert sweep — read `system_alerts`, decide what's genuinely new, publish.

Runs on the cron declared in `manifest.py::background_tasks`. Three steps:

  1. **Flatten** the `system_alerts` payload into one item per distinct
     problem, each with a stable fingerprint (`_fingerprint`).
  2. **Reconcile** that set against the open rows in `notification_sends`:
     new → send; still-open → send only if `resend_after_minutes` has elapsed;
     vanished → resolve, and optionally send a recovery message.
  3. **Record** the outcome on SyncState so a broken notifier is itself visible
     on the dashboard.

Step 2 is the whole value of this module. Step 1 is where the subtlety hides:
`system_alerts` renders issues as human text containing ages ("last sync 3h 12m
ago"), which change on every sweep. Fingerprinting the rendered string would
produce a brand-new fingerprint every 15 minutes and dedupe nothing, so
`_issue_kind` maps each issue to a stable *kind* first.

`system` is reached through the `system.alerts` capability, never by importing
its internals — see `app/plugin/capabilities.py` and this package's
`manifest.py::depends_on`.

**Push-boundary gating (2026-08-27).** `vault/Projects/lios/Backlog.md`:
"Push notifications flap all night" — `macbook:daemon_silent` and
`apple_reminders:data_stale` each fired and self-resolved in 15-45 minutes,
repeating every 30-60 minutes around the clock, because a MacBook with the
lid closed is expected state that a flat elapsed-time check cannot tell apart
from an incident. The fix sits entirely at the boundary between "this row is
open" and "call `client.publish`" — never in `collect()` or the axes in
`system/tools.py`. Detection keeps detecting; only delivery is gated. Three
independent mechanisms, all bypassed by `severity == "critical"` (a re-auth
link or a real outage must never wait on a gate built for a sleeping laptop):

  - **Persistence gate** (`min_active_minutes`) — a fingerprint may push only
    once it has been continuously active (row open) for at least this long.
    A new row is written, unpublished, on first sighting; if the condition
    resolves before the gate elapses the row closes normally and nothing was
    ever sent. This alone kills most lid-close flaps, since they resolve in
    15-45 minutes against a 30-minute default gate.
  - **Re-fire cooldown** (`refire_cooldown_minutes`) — after a fingerprint's
    row resolves, a *new* firing of the same fingerprint will not push until
    this long has elapsed since that resolution (looked up from the most
    recent resolved row for the fingerprint). Stops a condition that keeps
    flapping past the persistence gate from re-arming a fresh push every
    cycle.
  - **Quiet hours** (`quiet_hours`) — non-critical pushes are held during a
    configured local-time window and delivered once at window end if the
    alert is *still* active then. An alert that clears overnight during the
    window simply resolves with nothing ever sent — never a backlog dump of
    everything that flapped while everyone was asleep.

A row a gate holds is marked with `suppressed_reason` (see `models.py`) so
`notify_recent` can still explain it — a silently suppressed push must never
look, from outside, identical to a sweep that broke.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.errors import ComarError
from app.integrations.notifications import client, deadlines
from app.integrations.notifications.client import NotifyConfigError
from app.integrations.notifications.models import NotificationSend
from app.plugin.capabilities import get_capability
from app.plugin.config_store import plugin_config


def _strip_prefix(title: str) -> str:
    """Drop the platform prefix from a stored alert title.

    Rows written before the 2026-09-03 rename carry ``comar: ``; new rows carry
    ``lios: ``. A recovery message for an old row must still read cleanly.
    """
    for prefix in ("lios: ", "comar: "):
        if title.startswith(prefix):
            return title[len(prefix):]
    return title


logger = logging.getLogger(__name__)

# Fallback when `quiet_hours_timezone` is unset or unparseable — same
# reasoning and same default as `deadlines._DEFAULT_TZ`: a window expressed
# in UTC drifts an hour across DST and stops meaning "22:00" for half the
# year.
_DEFAULT_TZ = "Europe/Dublin"

# Maps the leading text of an issue string from `system/tools.py::handle_alerts`
# to a stable kind. Order matters — "failing repeatedly" must be tested before
# "failing", or a slow-tool alert would be filed as a sync failure.
#
# Coupling note: these prefixes mirror strings built in another package. That is
# a real (if shallow) coupling, and `_issue_kind` degrades to a slug rather than
# raising if one changes — a wrongly-grouped alert is a far better failure than
# a sweep that crashes and stops notifying at all. `tests/test_notifications.py`
# asserts every prefix still matches something the real handler emits.
_ISSUE_KINDS: tuple[tuple[str, str], ...] = (
    ("never synced", "never_synced"),
    ("failing repeatedly", "tool_failing"),
    ("failing", "failing"),
    ("sync stale", "sync_stale"),
    ("data stale", "data_stale"),
    ("p95 duration", "tool_slow"),
)

# Consecutive failures past which deriverstion is treated as critical rather
# than a warning — enough to rule out a single transient blip.
_CRITICAL_FAILURE_COUNT = 5

# F7: the original way an issue naming a specific user was detected — by
# regexing the rendered prose of `system/tools.py`'s health-coverage axis
# ("data gap for user 2: 3 of last 7 days missing (...)").
#
# Superseded 2026-08-19 by the structural `issue_users` map that
# `system/tools.py::_attribute` now puts on each alert entry, which covers all
# three attributable shapes (health-coverage gaps, per-owner staleness rows,
# and daemon liveness) instead of just this one. Kept as the fallback: an older
# payload — or a shape nobody has attributed yet — still resolves, and a
# wording change now only costs the fallback, not the attribution.
_USER_ATTRIBUTED_ISSUE = re.compile(r"^data gap for user (\d+):")


def _attributed_user(alert: dict, issue: str) -> int | None:
    """Which household member, if any, this issue belongs to.

    Three sources in decreasing order of trust:

      1. `alert["issue_users"][issue]` — set structurally by
         `system/tools.py::_attribute`, keyed on the exact issue string.
      2. `alert["user_id"]` — an entry whose *every* issue has one owner
         (a daemon alert is keyed on a `client_tokens` label, which belongs
         to exactly one person).
      3. The legacy prose regex above.

    Returns None for a genuinely household-wide problem, which is most of
    them — a failing Gmail sync belongs to the household, not to a person.
    """
    structural = (alert.get("issue_users") or {}).get(issue)
    if structural is not None:
        return int(structural)
    if alert.get("user_id") is not None:
        return int(alert["user_id"])
    match = _USER_ATTRIBUTED_ISSUE.match(issue.strip())
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class AlertItem:
    """One distinct problem, ready to publish."""

    fingerprint: str
    title: str
    body: str
    severity: str
    # F7: set only when the issue names a specific user (see
    # `_USER_ATTRIBUTED_ISSUE` above). None means household-shared, the
    # normal case — `notification_sends.user_id` mirrors this 1:1.
    target_user_id: int | None = None


def _issue_kind(issue: str) -> str:
    """Reduce a rendered issue string to a stable kind.

    The input carries volatile detail ("sync stale (last sync 3h 12m ago)");
    the output must be identical across sweeps for as long as the underlying
    problem persists, or deduplication cannot work.
    """
    lowered = issue.strip().lower()
    for prefix, kind in _ISSUE_KINDS:
        if lowered.startswith(prefix):
            return kind
    # Unknown shape — derive something stable from the words before any
    # parenthesised detail, which is where the varying numbers live.
    head = lowered.split("(")[0].strip()
    return "_".join(head.split()[:3]) or "unknown"


def collect(session: Session) -> list[AlertItem]:
    """Flatten the current `system_alerts` payload into fingerprinted items."""
    cfg = plugin_config("notifications")
    alerts_capability = get_capability("system.alerts")
    # `alerts_household`, not `alerts` — this sweep runs on a cron with no
    # user ever bound, and must see every household-infrastructure problem
    # regardless of who (if anyone) happens to be asking. Calling the plain
    # `alerts()` method would happen to produce the same result today (no
    # context is bound on this path), but that's an accident of the caller,
    # not a guarantee — see `system/tools.py::handle_alerts_household`.
    raw = alerts_capability.alerts_household(
        session, {"threshold_minutes": cfg.threshold_minutes or 60}
    )
    payload = json.loads(raw)

    items: list[AlertItem] = []

    # Per-integration alerts: one item per (integration, issue kind), not one
    # per integration — "stale AND failing" are two problems that can resolve
    # independently, and collapsing them would let one mask the other.
    for alert in payload.get("alerts", []):
        name = alert.get("integration", "unknown")
        failures = alert.get("consecutive_failures") or 0
        severity = "critical" if failures >= _CRITICAL_FAILURE_COUNT else "warning"
        for issue in alert.get("issues", []):
            body = f"{name}: {issue}"
            if alert.get("last_error"):
                body += f"\nLast error: {alert['last_error']}"
            target_user_id = _attributed_user(alert, issue)
            fingerprint = f"integration:{name}:{_issue_kind(issue)}"
            if target_user_id is not None:
                # Without this, two users' gaps both fall back to the same
                # word-slug kind ("data_gap_for") and collide onto one open
                # ledger row — the partial unique index would then let the
                # second user's gap silently overwrite the first's.
                fingerprint += f":user{target_user_id}"
            items.append(
                AlertItem(
                    fingerprint=fingerprint,
                    title=f"lios: {name} degraded",
                    body=body,
                    severity=severity,
                    target_user_id=target_user_id,
                )
            )

    # Re-auth is always critical: it cannot clear on its own, and the fix is a
    # specific link a human has to open.
    for entry in payload.get("reauth_needed", []):
        account = entry.get("account_email", "unknown")
        provider = entry.get("provider", "google")
        items.append(
            AlertItem(
                fingerprint=f"reauth:{provider}:{account}",
                title="lios: re-authentication needed",
                body=(
                    f"{provider} token for {account} needs re-auth"
                    + (f" ({entry['reason']})" if entry.get("reason") else "")
                    + f"\n{entry.get('reauth_url', '')}"
                ),
                severity="critical",
            )
        )

    for entry in payload.get("tool_alerts", []):
        tool = entry.get("tool", "unknown")
        issue = entry.get("issue", "")
        items.append(
            AlertItem(
                fingerprint=f"tool:{tool}:{_issue_kind(issue)}",
                title=f"lios: tool {tool} unhealthy",
                body=f"{tool}: {issue}",
                severity="warning",
            )
        )

    # Deadline watches — a different question from everything above. The axes
    # in `system_alerts` all ask "has this gone quiet for longer than N"; a
    # deadline asks "is it past 10am and last night's data still isn't here",
    # which no elapsed-time threshold can express. See `deadlines.py`.
    try:
        items.extend(deadlines.collect(session))
    except Exception:  # noqa: BLE001
        # Same rule as every axis in `system/tools.py`: one broken check must
        # never stop the sweep from publishing the others.
        logger.exception("deadline checks failed")

    return _apply_suppression(items)


def _suppressed_user_ids() -> set[int]:
    """User ids whose attributable alerts should not be pushed.

    Why this exists: `household_targets` is a list of devices, and in this
    household it holds exactly one phone — so every "household-wide" alert has
    always landed on one person. That makes another household member's stalled
    laptop or frozen step count a notification *they* cannot act on and the
    recipient cannot act on either, arriving daily. Set 2026-08-19.

    Suppression is deliberately at the *push* boundary, not the detection
    boundary: the alert stays in `system_alerts` and on the dashboard, so the
    stall is still visible when someone looks — it just stops ringing a phone.
    Filtering it out of `check_all` instead would recreate the exact blindness
    the per-owner probes were added to fix.
    """
    cfg = plugin_config("notifications")
    out: set[int] = set()
    for raw in cfg.suppress_push_for_user_ids or []:
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            # A junk config entry must not silence everything or crash the
            # sweep — skip it loudly and carry on.
            logger.warning("ignoring non-numeric suppress_push_for_user_ids entry %r", raw)
    return out


def _apply_suppression(items: list[AlertItem]) -> list[AlertItem]:
    """Drop items attributed to a suppressed user before they reach the ledger.

    Before the ledger rather than at publish time: `notification_sends` is a
    record of what was *sent*, and an open row that never sends would sit there
    looking like a delivery that failed.
    """
    suppressed = _suppressed_user_ids()
    if not suppressed:
        return items
    kept = [i for i in items if i.target_user_id not in suppressed]
    dropped = len(items) - len(kept)
    if dropped:
        logger.info("suppressed %d alert(s) for user_ids %s", dropped, sorted(suppressed))
    return kept


def _minutes(value, *, config_key: str) -> int:
    """Coerce a config minutes value, degrading a junk value to "disabled"
    (0) rather than crashing the sweep or reviving a hardcoded default that
    could silently diverge from what the admin actually set.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        logger.warning("ignoring bad notifications.%s %r; gate disabled", config_key, value)
        return 0
    return value


def _resolve_quiet_hours_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or _DEFAULT_TZ)
    except Exception:  # noqa: BLE001 — zoneinfo raises several unrelated types
        logger.warning(
            "bad notifications.quiet_hours_timezone %r; falling back to %s", name, _DEFAULT_TZ
        )
        return ZoneInfo(_DEFAULT_TZ)


def _parse_hhmm(raw: str) -> time:
    hour_s, minute_s = raw.strip().split(":", 1)
    hour, minute = int(hour_s), int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"hour/minute out of range: {raw!r}")
    return time(hour, minute)


def _parse_quiet_hours(raw: str | None) -> tuple[time, time] | None:
    """Parse `"HH:MM-HH:MM"` into (start, end), or None if disabled/unset.

    A window is allowed to wrap midnight (the default, "22:00-07:30") —
    `_in_quiet_hours` below is what interprets that, this just parses the
    two endpoints.
    """
    if not raw or not raw.strip():
        return None
    try:
        start_s, end_s = raw.split("-", 1)
        return _parse_hhmm(start_s), _parse_hhmm(end_s)
    except (ValueError, TypeError):
        logger.warning("bad notifications.quiet_hours %r; quiet hours disabled", raw)
        return None


def _in_quiet_hours(cfg, now: datetime) -> bool:
    window = _parse_quiet_hours(getattr(cfg, "quiet_hours", None))
    if window is None:
        return False
    tz = _resolve_quiet_hours_tz(getattr(cfg, "quiet_hours_timezone", None))
    current = now.astimezone(tz).time()
    start, end = window
    if start <= end:
        return start <= current < end
    # Wraps midnight (e.g. 22:00-07:30): "in the window" means at or after
    # start, OR before end — the gap the plain range comparison misses.
    return current >= start or current < end


def _push_gate(
    item: AlertItem,
    row: NotificationSend,
    now: datetime,
    cfg,
    last_resolved: dict[str, datetime],
) -> tuple[bool, str | None]:
    """Whether `row` may push right now, and — if not — why.

    Order doesn't matter for correctness (all three are independent holds),
    but persistence-then-cooldown-then-quiet-hours is the order a human
    would explain it in, and `suppressed_reason` only ever reflects the
    first reason found, so this is also the priority a query sees.
    """
    if item.severity == "critical":
        return True, None

    min_active = _minutes(getattr(cfg, "min_active_minutes", 30), config_key="min_active_minutes")
    if min_active and (now - row.first_seen_at) < timedelta(minutes=min_active):
        return False, "min_active_gate"

    cooldown = _minutes(
        getattr(cfg, "refire_cooldown_minutes", 120), config_key="refire_cooldown_minutes"
    )
    resolved_at = last_resolved.get(item.fingerprint)
    if cooldown and resolved_at is not None and (now - resolved_at) < timedelta(minutes=cooldown):
        return False, "refire_cooldown"

    if _in_quiet_hours(cfg, now):
        return False, "quiet_hours"

    return True, None


def _publish(row: NotificationSend, item: AlertItem, now: datetime) -> None:
    """Publish one item and stamp the ledger row, tolerating a failed send.

    A publish failure must not abort the sweep: the remaining alerts still
    deserve a try, and the row stays open (`send_count` unchanged) so the next
    sweep retries it rather than treating it as delivered.

    **Routing (changed 2026-08-19).** An item with a `target_user_id` goes to
    that one person's device (`targets[str(user_id)]`); everything else fans out
    household-wide as before. This was previously always household-wide, and the
    old docstring here explained why: baking the owner into the fingerprint was a
    prerequisite, or two users' otherwise identical issues would collide onto one
    open ledger row and dedupe one against the other. `collect()` now does bake
    it in (`:user{id}`), so the prerequisite is met and routing is safe.

    It also *matters* now. "Last night's sleep is missing" is addressed to one
    person; sending it household-wide would tell the wrong person about a gap in
    somebody else's data — the same mistake as showing Sam Alex's health hole,
    which is why the scoped alerts view filters by owner in the first place.

    Falls back to the household targets when that person has no device mapped,
    since a misrouted alert beats a silently dropped one — but says so in the
    log, because the fallback means someone is reading about another person's
    problem.
    """
    try:
        client.publish(
            item.title, item.body, item.severity, user_id=item.target_user_id,
            source="sweep", fingerprint=item.fingerprint, ledger=False,
        )
    except NotifyConfigError as exc:
        if item.target_user_id is None:
            logger.warning("HA notify publish failed for %s: %s", item.fingerprint, exc)
            return
        logger.warning(
            "no per-user notify target for user %s (%s); falling back to household",
            item.target_user_id,
            exc,
        )
        try:
            client.publish(
                item.title, item.body, item.severity,
                source="sweep", fingerprint=item.fingerprint, ledger=False,
            )
        except ComarError as fallback_exc:
            logger.warning(
                "HA notify household fallback failed for %s: %s",
                item.fingerprint,
                fallback_exc,
            )
            return
    except ComarError as exc:
        logger.warning("HA notify publish failed for %s: %s", item.fingerprint, exc)
        return
    row.last_sent_at = now
    row.send_count = (row.send_count or 0) + 1


def reconcile(session: Session, items: list[AlertItem]) -> dict[str, int]:
    """Diff current alerts against open ledger rows and publish the delta.

    Two independent questions get asked of every currently-active alert:
    "is it due a (re)send" (the pre-existing `resend_after_minutes` logic —
    unchanged) and, only if so, "is it *allowed* to push right now" (the
    persistence gate / re-fire cooldown / quiet hours added 2026-08-27, via
    `_push_gate`). A row that is due but not allowed stays open, unsent,
    marked with why — the next sweep re-asks both questions from scratch, so
    a hold is never permanent, only until the gate condition itself clears.
    """
    cfg = plugin_config("notifications")
    resend_after = timedelta(minutes=cfg.resend_after_minutes or 1440)
    now = datetime.now(timezone.utc)
    topic = client.current_topic()

    by_fingerprint = {item.fingerprint: item for item in items}
    open_rows = {
        row.fingerprint: row
        for row in session.query(NotificationSend)
        .filter(NotificationSend.resolved_at.is_(None))
        .all()
    }
    # For the re-fire cooldown: the most recent resolution of each
    # fingerprint, across all history (not just currently-open rows) — a
    # fingerprint that resolved and hasn't reappeared yet still needs this
    # the instant it does.
    last_resolved: dict[str, datetime] = dict(
        session.query(NotificationSend.fingerprint, func.max(NotificationSend.resolved_at))
        .filter(NotificationSend.resolved_at.isnot(None))
        .group_by(NotificationSend.fingerprint)
        .all()
    )

    counts = {"new": 0, "resent": 0, "resolved": 0, "suppressed": 0, "held": 0}

    for fingerprint, item in by_fingerprint.items():
        row = open_rows.get(fingerprint)
        is_new = row is None
        if is_new:
            row = NotificationSend(
                fingerprint=fingerprint,
                title=item.title,
                body=item.body,
                severity=item.severity,
                topic=topic,
                send_count=0,
                user_id=item.target_user_id,
                # Set explicitly (not left to the column's server_default) so
                # the persistence gate can compute this row's age against
                # `now` within the same sweep that created it — a freshly
                # inserted, unflushed row has no default from Postgres yet.
                first_seen_at=now,
            )
            session.add(row)
        else:
            # Keep the body fresh even when suppressing — the ledger should
            # show the current age, not the age at first detection.
            row.body = item.body
            row.severity = item.severity
            row.user_id = item.target_user_id

        never_delivered = row.last_sent_at is None
        resend_due = is_new or never_delivered or (now - row.last_sent_at) >= resend_after
        if not resend_due:
            counts["suppressed"] += 1
            continue

        allowed, reason = _push_gate(item, row, now, cfg, last_resolved)
        if not allowed:
            row.suppressed_reason = reason
            counts["held"] += 1
            continue

        row.suppressed_reason = None
        _publish(row, item, now)
        counts["new" if is_new else "resent"] += 1

    # Anything open that the current sweep no longer sees has cleared.
    for fingerprint, row in open_rows.items():
        if fingerprint in by_fingerprint:
            continue
        row.resolved_at = now
        counts["resolved"] += 1
        # Only announce recovery for something that actually reached the phone;
        # an alert that never sent has nothing to un-say.
        if cfg.notify_on_recovery and (row.send_count or 0) > 0:
            try:
                client.publish(
                    f"lios: recovered — {_strip_prefix(row.title)}",
                    f"Resolved after {_format_duration(now - row.first_seen_at)}.",
                    "recovery",
                    source="sweep", fingerprint=fingerprint, ledger=False,
                )
            except ComarError as exc:
                # The row is resolved regardless; a missed recovery ping is
                # cosmetic, and re-opening it would re-alert on the next sweep.
                logger.warning("HA notify recovery publish failed for %s: %s", fingerprint, exc)

    session.commit()
    return counts


def _format_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def sweep_blocking() -> dict[str, int]:
    """Synchronous body of the sweep — separated so tests can call it directly."""
    from app.db import get_db

    db = get_db()
    with db.session() as session:
        return reconcile(session, collect(session))


async def run_sweep() -> None:
    """Cron entry point (see `manifest.py::background_tasks`).

    Writes SyncState under this integration's own name so a notifier that is
    itself broken shows up on the dashboard — and, once configured, in the very
    alert payload it reads. That is deliberate: the failure mode this whole
    package exists to fix is a problem nobody is told about.
    """
    import asyncio

    from app.scheduler import _update_sync_state

    try:
        counts = await asyncio.to_thread(sweep_blocking)
    except Exception as exc:
        logger.exception("Notification sweep failed")
        _update_sync_state(
            "notifications", status="error", error=str(exc)[:200], trigger="sweep",
        )
        return

    logger.info("Notification sweep: %s", counts)
    _update_sync_state("notifications", status="ok", trigger="sweep")
