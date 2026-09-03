"""Pinned job-schedule snapshot (unit tier) — V4 chunk 3.1.

Captures the exact job set `setup_scheduler()` registers — ids, cron
expressions, timezones, misfire-grace-times — as it stood on HEAD *before*
this chunk's refactor (five hardcoded jobs in scheduler.py + per-integration
`sync_schedule()`), and asserts the manifest-driven version produces the
identical set. This is the "no behavior change" proof the chunk brief asks
for.

`is_configured()` is monkeypatched to True for every registered integration
so the snapshot doesn't depend on which API keys/secrets happen to be set
in the test environment (some sync jobs are gated on `integration.is_configured()`
— unrelated to and unaffected by this chunk).
"""

from __future__ import annotations

import asyncio

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.integrations import INTEGRATIONS, register_all
from app import scheduler as scheduler_module

pytestmark = pytest.mark.unit


@pytest.fixture
def anyio_backend():
    return "asyncio"


# (cron, timezone, misfire_grace_time) — pinned from HEAD's scheduler.py
# hardcoded jobs + every integration's (then) sync_schedule()/sync_timezone().
PINNED_SCHEDULE: dict[str, tuple[str, str | None, int]] = {
    # Per-integration sync jobs (only integrations with a schedule).
    "sync_attachments": ("*/30 * * * *", None, 120),
    # Repinned 2026-08-13 from `1-5` to `mon-fri`. Not cosmetic: APScheduler's
    # from_crontab numbers day-of-week 0=Monday..6=Sunday, so `1-5` fired
    # Tue-Sat — the job skipped every Monday morning and ran every Saturday.
    # Named days say what they mean and don't depend on the quirk. This guard
    # is what caught the change, which is exactly its job.
    "sync_commute": ("0-57 7-8 * * mon-fri", "Europe/Dublin", 120),
    "sync_google_calendar": ("*/15 * * * *", None, 120),
    "sync_google_mail": ("*/15 * * * *", None, 120),
    "sync_homeassistant": ("*/5 * * * *", None, 120),
    "sync_inbox": ("7 * * * *", None, 120),
    "sync_lastfm": ("*/15 * * * *", None, 120),
    # Strava activities surface minutes-to-hours after the ride finishes
    # (watch sync, then Strava's own processing), so a tighter cadence buys
    # nothing but API quota against a 200-request/15-min ceiling. One request
    # per run when there is nothing new (2026-08-29).
    "sync_strava": ("*/30 * * * *", None, 120),
    "sync_media": ("5,35 * * * *", None, 120),
    "sync_obsidian": ("*/30 * * * *", None, 120),
    "sync_weather": ("*/30 * * * *", None, 120),
    "sync_whatsapp": ("*/30 * * * *", None, 120),
    # First `type="deriver"` integration (2026-08-22). Two jobs from one
    # manifest, and the split is the point: `schedule` drives the prediction
    # cycle, `background_tasks` drives training. Refitting per prediction
    # cycle would make every prediction unreproducible and would let one
    # overcast week replace a working model within the hour.
    "sync_solar_forecast": ("20 * * * *", None, 120),
    # Kernel jobs (was inline in setup_scheduler(), now app.plugin.kernel_jobs).
    "embedding_processor": ("*/5 * * * *", None, 120),
    "prune_client_logs": ("0 3 * * *", None, 300),
    "prune_tool_calls": ("0 3 * * *", None, 300),
    "prune_auth_events": ("0 3 * * *", None, 300),
    # Grades every deriver's due predictions against observed reality. Kernel-
    # owned so a new deriver is scored from its first prediction without
    # declaring anything — a per-deriver scoring cron would fail silently, and
    # a silent scoring outage looks exactly like a working forecaster. Offset
    # to :12 because every other cron here fires on :00 and scoring reads the
    # tables those jobs write.
    "score_algo_predictions": ("12 * * * *", None, 300),
    # Cron-kind background tasks (was inline in setup_scheduler(), now
    # manifest background_tasks entries).
    "backlog_sync": ("*/30 * * * *", None, 120),
    "whatsapp_bridge_heartbeat": ("* * * * *", None, 30),
    "solar_forecast_train": ("40 4 * * sun", None, 600),
    # `notifications` has no sync job at all — it's a `capability`, so this
    # cron task is its only scheduled work (2026-07-31).
    "notifications_alert_sweep": ("*/15 * * * *", None, 120),
    # Transcription runs on its own cron rather than inside sync_inbox: it
    # costs money per call and a long memo takes minutes (2026-07-31).
    "inbox_transcribe_pending": ("*/5 * * * *", None, 120),
    # Images get the same treatment on a slower cadence — a photographed letter
    # isn't time-critical the way a voice note is, and images arrive in bursts
    # (a camera-roll drop) that shouldn't race the transcriber (2026-08-05).
    "inbox_describe_pending": ("*/15 * * * *", None, 120),
    # Pulls WhatsApp message-to-self notes into the triage queue (2026-08-17).
    # Faster than the hourly `sync_inbox` because a note typed on a phone should
    # reach triage while the thought is current; nothing here is billable, so the
    # cadence is bounded by usefulness rather than spend.
    "inbox_route_whatsapp_notes": ("*/10 * * * *", None, 120),
    # Pre-builds the cacheable half of `system_daily_brief` so the first
    # /daily-note of the day reads a warm cache instead of fanning out across
    # ~20 sources. Windowed to the morning and repeated every 15 min rather
    # than fired once: a single early warm goes stale for anyone who opens
    # their note later, and household members don't start their days together.
    # Volatile sources (rail, home status) are deliberately not warmed.
    "daily_brief_prewarm": ("*/15 5-11 * * *", None, 300),
}


@pytest.mark.anyio
async def test_job_schedule_is_byte_identical_to_pinned_snapshot(monkeypatch):
    INTEGRATIONS.clear()
    register_all()
    assert INTEGRATIONS, "expected register_all() to populate the registry"

    for integration in INTEGRATIONS.values():
        monkeypatch.setattr(integration, "is_configured", lambda: True, raising=False)

    # V4 chunk 5.1: setup_scheduler() also gates on the enable/disable
    # switch, which reads the `integration_config` table — mock it enabled
    # for everyone so this pinned-schedule snapshot doesn't need real
    # Postgres (unrelated to what this test pins).
    monkeypatch.setattr(scheduler_module, "is_integration_enabled", lambda name: True)

    scheduler_module.scheduler.remove_all_jobs()
    try:
        scheduler_module.setup_scheduler()

        jobs = {job.id: job for job in scheduler_module.scheduler.get_jobs()}

        assert set(jobs) == set(PINNED_SCHEDULE), (
            f"job id set changed: missing={set(PINNED_SCHEDULE) - set(jobs)}, "
            f"extra={set(jobs) - set(PINNED_SCHEDULE)}"
        )

        for job_id, (cron, tz, misfire) in PINNED_SCHEDULE.items():
            job = jobs[job_id]
            expected_trigger = CronTrigger.from_crontab(cron, timezone=tz)
            assert str(job.trigger) == str(expected_trigger), (
                f"{job_id}: trigger changed — {job.trigger!r} != {expected_trigger!r}"
            )
            assert job.misfire_grace_time == misfire, (
                f"{job_id}: misfire_grace_time changed — "
                f"{job.misfire_grace_time!r} != {misfire!r}"
            )
    finally:
        scheduler_module.scheduler.remove_all_jobs()
        if scheduler_module.scheduler.running:
            scheduler_module.scheduler.shutdown(wait=False)
