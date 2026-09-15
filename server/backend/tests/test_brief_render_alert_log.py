"""`render_alerts`'s "Monitoring since last note" sub-block (lios#230, unit
tier) — pure function, no DB. Covers: FYI + phone lines, still-firing vs
cleared wording, absent/error source, and an empty window."""

from __future__ import annotations

from app.integrations.system import brief_render

BASE_ALERTS_PAYLOAD = {"status": "all_ok", "alerts": []}


def _payload(alert_log):
    return {"alerts": BASE_ALERTS_PAYLOAD, "alert_log": alert_log}


def test_fyi_and_phone_lines_with_markers():
    alert_log = {
        "fired": [
            {
                "fingerprint": "fp1", "alertname": "LowMemory", "page": None,
                "summary": "transient blip", "received_at": "2026-09-14T09:15:00+00:00",
                "still_firing": True,
            },
            {
                "fingerprint": "fp2", "alertname": "HostDiskFull", "page": "phone",
                "summary": "disk full", "received_at": "2026-09-14T10:00:00+00:00",
                "still_firing": True,
            },
        ],
        "cleared": [],
    }
    fragment = brief_render.render_alerts(_payload(alert_log))

    assert "Monitoring since last note" in fragment
    assert "09:15 LowMemory — transient blip [still firing]" in fragment
    assert "📱 10:00 HostDiskFull — disk full [still firing]" in fragment
    # FYI line must NOT carry the phone marker.
    assert "📱 09:15" not in fragment


def test_cleared_line_shows_clear_time():
    alert_log = {
        "fired": [],
        "cleared": [
            {
                "fingerprint": "fp3", "alertname": "LowMemory", "page": None,
                "summary": "self-cleared", "received_at": "2026-09-14T07:00:00+00:00",
                "ends_at": "2026-09-14T07:05:00+00:00",
            },
        ],
    }
    fragment = brief_render.render_alerts(_payload(alert_log))
    assert "07:00 LowMemory — self-cleared [cleared 07:05]" in fragment


def test_no_longer_firing_entry_in_fired_list_reads_cleared_without_time():
    """`still_firing: False` is possible when the resolution happened
    outside the window (see service.py) — the fired-list line then reads
    "[cleared]" with no time, since the row itself carries no ends_at."""
    alert_log = {
        "fired": [
            {
                "fingerprint": "fp4", "alertname": "Flaky", "page": None,
                "summary": "resolved earlier", "received_at": "2026-09-14T09:00:00+00:00",
                "still_firing": False,
            },
        ],
        "cleared": [],
    }
    fragment = brief_render.render_alerts(_payload(alert_log))
    assert "09:00 Flaky — resolved earlier [cleared]" in fragment


def test_empty_window_omits_sub_block_but_keeps_alerts_section():
    fragment = brief_render.render_alerts(_payload({"fired": [], "cleared": []}))
    assert "System Alerts" in fragment
    assert "Monitoring since last note" not in fragment


def test_absent_source_renders_not_measured():
    payload = {"alerts": BASE_ALERTS_PAYLOAD}  # no "alert_log" key at all
    fragment = brief_render.render_alerts(payload)
    assert "Monitoring since last note:** _not measured_" in fragment


def test_error_source_renders_not_measured():
    fragment = brief_render.render_alerts(_payload({"error": "capability unavailable"}))
    assert "Monitoring since last note:** _not measured_" in fragment


def test_existing_alerts_content_is_unchanged_above_the_new_block():
    payload = {
        "alerts": {
            "status": "degraded",
            "alerts": [{"integration": "lastfm", "issues": ["data stale"]}],
            "recent_runs": {"counts": {"ok": 10, "error": 1}},
        },
        "alert_log": {"fired": [], "cleared": []},
    }
    fragment = brief_render.render_alerts(payload)
    assert "Status: **degraded**" in fragment
    assert "⚠️ **lastfm**: data stale" in fragment
    assert "Recent runs: 1 error, 10 ok" in fragment
