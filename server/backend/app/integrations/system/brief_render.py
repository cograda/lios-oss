"""Deterministic markdown renderers for `system_daily_brief`'s formatting
tier (`render=true`).

## Why this exists

`/daily-note` asks a Haiku subagent to turn the brief's JSON into prose for
every section, including the purely mechanical ones — "here is the current
coffee bag", "here are today's rail departures" — where there is nothing to
opine about and nothing an LLM pass can get right that a template can't get
right for free (and can't get *wrong*: a hallucinated coffee name is not a
risk this module can have). This module renders those sections deterministically,
straight from the brief payload, so `/kickoff` can paste the result instead of
composing it. Sections that genuinely need judgement (the Morning assessment,
email/WhatsApp triage, coaching opinions) stay the model's job and are not
rendered here.

## The one hard rule

**Every number comes from the payload handed to the function, or it doesn't
appear.** No renderer here reads a previous note, no renderer caches a value
across calls (the brief's own cache is a different, source-level cache — see
`brief.py` — and is keyed by raw source, never by rendered markdown value).
This is the same rule the daily-note template enforces on the model
(`daily-note.md.j2`'s "Claims carry their source"), applied mechanically: a
pure function that only ever reads its one argument cannot copy a stale
number forward even by accident.

## Absent vs empty vs error

Three states, and they must never look alike:

- **Absent** (the key isn't in the payload at all — the source was never
  fetched, e.g. the section was filtered out, or the user has no data and the
  source is gated) is rendered as an explicit "not measured" line for
  sections where the reader would otherwise expect one every day (Pulse's
  Sleep/Vitals lines, freshness rows, alerts). For sections that are
  legitimately absent for whole classes of user (Coffee, Listening — gated on
  `has_data()`) or absent by calendar rule (Transport on a weekend), the whole
  fragment renders as `""` and is omitted, matching the template's own
  omission rule for those sections.
- **Empty** (the source ran and came back with nothing to report — no
  nearly-empty sensors, no snags captured, no coffee bag on the go) also
  renders as `""` for the sections the spec calls out this way (consumables,
  snags, coffee, transport-on-weekend). This is a real, positive "zero", not
  an unknown, and looks nothing like "not measured" in the underlying data —
  only the two happen to render the same (empty) fragment for those sections,
  which the daily-note template already tolerates (a section with no data is
  "absent, not present-and-empty").
- **Error** (the source key holds `{"error": ...}`, per `brief.py`'s
  `_fetch_one` contract) always renders a visible `⚠️ ... unavailable` line —
  never silently omitted, matching the same rule the daily-note template's
  step 4 states for the model's own reading of the payload.

## Headings

Each renderer's `## Heading` is intended to match
`app/prompts/templates/daily-note.md.j2` step 6's skeleton for the same
content, so the two can be spliced into one note without a reader noticing
the seam. Two of the nine don't have a pre-existing heading to match, because
the template renders that content inline rather than under its own `##`:

- **alerts** — the template puts system-alert warnings as blockquote lines
  directly under the day header, with no heading at all. This module gives
  them one (`## System Alerts`) so the fragment is self-contained per this
  file's contract; a template splicing pass can still lift the alerts prose
  out from under the heading if it wants the old inline placement.
- **listening** — the template nests a "🎵 Listening" *sub*-block inside
  Pulse, not a top-level `##` section. Rendered as its own `## Listening`
  fragment here because the render contract calls it out as an independent
  section; a template splicing pass can fold it back under Pulse.

The remaining seven (`Pulse`, `Coffee`, `Transport`, `House`, `Snags`,
`Today`, `Data freshness`) are copied verbatim from the template's own
headings (with the template's parenthetical *rendering instructions*, e.g.
"(weekdays only — omit on Sat/Sun)", stripped — those describe when the
model should include the section, not text meant to appear in a finished
note).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

HEADING_ALERTS = "System Alerts"
HEADING_PULSE = "Pulse"
HEADING_COFFEE = "Coffee"
HEADING_TRANSPORT = "Transport"
HEADING_HOUSE = "House"
HEADING_SNAGS = "Snags"
HEADING_TODAY = "Today"
HEADING_LISTENING = "Listening"
HEADING_FRESHNESS = "Data freshness"

# The section keys `render_all` produces, and the order they're written in —
# also what `brief.build(render=True)`'s cache-write step iterates.
SECTIONS = (
    "alerts", "pulse", "coffee", "transport", "consumables",
    "snags", "calendar", "listening", "freshness",
)

NOT_MEASURED = "_Not measured today._"


def _section(heading: str, body: str) -> str:
    """Wrap `body` under `## heading`; `""` in, `""` out.

    A renderer that has nothing to say returns an empty body and the whole
    fragment vanishes — no heading is ever written over nothing, which is
    what lets a caller safely `"\n\n".join(f for f in fragments if f)`.
    """
    if not body:
        return ""
    return f"## {heading}\n\n{body.strip()}\n"


def _is_error(value: Any) -> bool:
    return isinstance(value, dict) and "error" in value


def _hhmm(iso_ts: str | None) -> str:
    if not iso_ts:
        return "?"
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).strftime("%H:%M")
    except (ValueError, TypeError):
        return "?"


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------


def _alert_log_line(entry: dict, *, cleared: bool) -> str:
    """One line for a `system_alert_log` entry (lios#230):
    `HH:MM alertname — summary [still firing|cleared HH:MM]`, 📱-marked
    when `page == "phone"` (the ones that were meant to buzz a handset —
    see `alerts/models.py`'s docstring). `received_at` is when the event
    entered the log — the same timestamp used for both `fired` and
    `cleared` entries, so the two read consistently side by side."""
    marker = "📱 " if entry.get("page") == "phone" else ""
    hhmm = _hhmm(entry.get("received_at"))
    alertname = entry.get("alertname") or "?"
    summary = entry.get("summary") or entry.get("description") or "(no summary)"
    if cleared:
        state = f"cleared {_hhmm(entry.get('ends_at') or entry.get('received_at'))}"
    else:
        state = "still firing" if entry.get("still_firing", True) else "cleared"
    return f"- {marker}{hhmm} {alertname} — {summary} [{state}]"


def _monitoring_since_last_note(alert_log: Any) -> list[str]:
    """The "Monitoring since last note" sub-block's lines, or `[]` when
    there's nothing to add (absent/error source, or a genuinely empty
    window) — `render_alerts` decides whether `[]` means "omit the
    sub-heading" or "not measured"."""
    if not isinstance(alert_log, dict) or _is_error(alert_log):
        return None  # sentinel: caller distinguishes "no source" from "empty"
    fired = alert_log.get("fired") or []
    cleared = alert_log.get("cleared") or []
    if not fired and not cleared:
        return []
    lines = [_alert_log_line(e, cleared=False) for e in fired]
    lines += [_alert_log_line(e, cleared=True) for e in cleared]
    return lines


def render_alerts(payload: dict) -> str:
    """System alerts: status, the issues list, a `recent_runs` summary, and
    (lios#230) a "Monitoring since last note" sub-block listing what fired/
    cleared in `payload["alert_log"]` since the previous note — the
    reviewable log that lets FYI-severity monitoring noise (a transient low-
    memory blip, most Pushover chatter) surface here instead of paging a
    phone. Phone-tagged events (`page == "phone"`) are marked 📱 so the two
    classes are still visually distinct even though both land in this list.

    `payload["alerts"]` is always fetched (it has no `section`, see
    `brief.build_sources`), so its total absence here is an anomaly worth
    surfacing rather than silently skipping. The existing alerts content
    above the monitoring sub-block is unchanged by this addition.
    """
    alerts = payload.get("alerts")
    if not isinstance(alerts, dict):
        return _section(HEADING_ALERTS, NOT_MEASURED)
    if _is_error(alerts):
        return _section(HEADING_ALERTS, f"⚠️ system alerts unavailable ({alerts['error']})")

    lines = [f"Status: **{alerts.get('status', 'unknown')}**"]
    issue_entries = alerts.get("alerts") or []
    if not issue_entries:
        lines.append("All systems OK.")
    else:
        for entry in issue_entries:
            integration = entry.get("integration", "?")
            for issue in entry.get("issues") or []:
                lines.append(f"- ⚠️ **{integration}**: {issue}")

    recent_runs = alerts.get("recent_runs")
    if isinstance(recent_runs, dict) and recent_runs.get("counts"):
        counts = recent_runs["counts"]
        parts = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        lines.append(f"Recent runs: {parts}")
    else:
        lines.append("Recent runs: _not measured_")

    alert_log = payload.get("alert_log")
    log_lines = _monitoring_since_last_note(alert_log)
    if log_lines is None:
        lines.append("")
        lines.append("**Monitoring since last note:** _not measured_")
    elif log_lines:
        lines.append("")
        lines.append("**Monitoring since last note:**")
        lines.extend(log_lines)
    # else: empty window — nothing to add, matching this module's "empty
    # renders as omitted content, not a heading over nothing" rule for the
    # sub-block specifically (the parent ## System Alerts section still
    # renders regardless, per the always-fetched contract above).

    return _section(HEADING_ALERTS, "\n".join(lines))


# ---------------------------------------------------------------------------
# pulse — numbers only, no opinion lines
# ---------------------------------------------------------------------------


_WORKOUT_OVERLAP_MINUTES = 10


def _parse_workout_dt(iso_ts: str | None):
    """Best-effort `datetime` for a workout/activity `start` field, or None."""
    if not iso_ts:
        return None
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _durations_similar(a: float, b: float) -> bool:
    """Within 25% of the larger, or 5 minutes, whichever is more forgiving.

    Loose on purpose: two independent trackers (a watch feeding Apple Health,
    Strava's own GPS/manual stop) rarely agree on a session's exact end.
    """
    if not a or not b:
        return False
    tolerance = max(5.0, 0.25 * max(a, b))
    return abs(a - b) <= tolerance


def _merge_workouts(
    health_items: list[dict], strava_items: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Pair Strava activities with Apple Health workouts covering the same
    session (issue #195b), so the same workout — captured by both a watch's
    HealthKit sync and Strava — isn't listed twice.

    A pair is "the same session" when both start within
    `_WORKOUT_OVERLAP_MINUTES` of each other AND have a similar duration —
    both conditions, so a warm-up walk that happens to start close to a
    later lift doesn't get glued to it just because the clocks are close.

    Health wins the merged line when there's a match (it usually carries
    heart rate; Strava usually carries pace/distance) — the matched entry is
    *noted*, not discarded, via `_strava_matched` on the returned dict.

    Returns `(merged, strava_only)`: `merged` is `health_items` (copied, so
    the caller's payload is never mutated) with `_strava_matched` set on
    paired rows; `strava_only` is every Strava activity that found no health
    match, in its original order.
    """
    merged = [dict(w) for w in health_items]
    matched_health_idx: set[int] = set()
    strava_only: list[dict] = []

    for s in strava_items:
        s_start = _parse_workout_dt(s.get("start"))
        match_idx = None
        if s_start is not None:
            for i, w in enumerate(merged):
                if i in matched_health_idx:
                    continue
                w_start = _parse_workout_dt(w.get("start"))
                if w_start is None:
                    continue
                if (
                    abs((s_start - w_start).total_seconds())
                    <= _WORKOUT_OVERLAP_MINUTES * 60
                    and _durations_similar(
                        w.get("duration_min") or 0, s.get("duration_min") or 0
                    )
                ):
                    match_idx = i
                    break
        if match_idx is not None:
            merged[match_idx]["_strava_matched"] = True
            matched_health_idx.add(match_idx)
        else:
            strava_only.append(s)

    return merged, strava_only


def render_pulse(payload: dict) -> str:
    """Sleep, HRV, steps, workouts — numbers only, straight from the payload.

    Sourced from `health_sleep` (stage/session detail) and `health_trends`
    (the daily numeric metrics, keyed by date) rather than `health_summary`,
    which is a pre-formatted prose string built server-side — not something
    this module can pull individual numbers back out of without re-parsing
    text, which is exactly the fragility this renderer exists to avoid.
    Workouts merge `health_workouts` with `strava_activities` — see
    `_merge_workouts` — because relying on Apple Health alone produced a
    confidently wrong "none in the last week" when Strava independently had
    an activity that never reached HealthKit (issue #195).

    Absent entirely (none of the four keys present) means the user has no
    health *or* Strava data at all — the whole section is omitted, same as
    the template's own auto-omission rule.
    """
    keys = ("health_sleep", "health_trends", "health_workouts", "strava_activities")
    if not any(k in payload for k in keys):
        return ""

    lines: list[str] = []

    sleep = payload.get("health_sleep")
    if sleep is None:
        lines.append(f"😴 Sleep: {NOT_MEASURED}")
    elif _is_error(sleep):
        lines.append("😴 Sleep: ⚠️ unavailable")
    else:
        total = sleep.get("total_hours") or 0
        if not total:
            lines.append("😴 Sleep: no session recorded.")
        else:
            h = int(total)
            m = int(round((total - h) * 60))
            stages = sleep.get("stage_breakdown") or {}
            stage_bits = [
                f"{label} {stages[key]:.1f}h"
                for label, key in (
                    ("deep", "asleepDeep"), ("REM", "asleepREM"),
                    ("core", "asleepCore"), ("awake", "awake"),
                )
                if stages.get(key)
            ]
            line = f"😴 Sleep: {h}h{m:02d}m"
            if stage_bits:
                line += " — " + ", ".join(stage_bits)
            sessions = sleep.get("sessions") or []
            if sessions:
                line += f". {_hhmm(sessions[0].get('start'))} → {_hhmm(sessions[-1].get('end'))}"
            lines.append(line)

    trends = payload.get("health_trends")
    if trends is None:
        lines.append(f"🏃 Vitals/Movement: {NOT_MEASURED}")
    elif _is_error(trends):
        lines.append("🏃 Vitals/Movement: ⚠️ unavailable")
    else:
        daily = trends.get("daily") or {}
        latest_date = max(daily) if daily else None
        today_metrics = daily.get(latest_date, {}) if latest_date else {}
        averages = trends.get("period_averages") or {}
        bits: list[str] = []
        if "resting_hr_bpm" in today_metrics:
            bits.append(f"resting HR {today_metrics['resting_hr_bpm']:.0f}")
        if "hrv_ms" in today_metrics:
            bits.append(f"HRV {today_metrics['hrv_ms']:.0f}ms")
        if "steps" in today_metrics:
            step_line = f"{today_metrics['steps']:.0f} steps"
            if "steps" in averages:
                step_line += f" (7-day avg {averages['steps']:.0f})"
            bits.append(step_line)
        if "distance_km" in today_metrics:
            bits.append(f"{today_metrics['distance_km']:.1f} km")
        if "active_energy_kcal" in today_metrics:
            bits.append(f"{today_metrics['active_energy_kcal']:.0f} kcal active")
        if bits:
            lines.append("🏃 " + " · ".join(bits))
        else:
            lines.append(f"🏃 Vitals/Movement: no data for {latest_date or 'today'}.")

    workouts = payload.get("health_workouts")
    strava = payload.get("strava_activities")
    health_absent = workouts is None
    health_err = _is_error(workouts)
    strava_absent = strava is None
    strava_err = _is_error(strava)

    if health_absent and strava_absent:
        lines.append(f"💪 Workouts: {NOT_MEASURED}")
    else:
        health_items = (
            workouts.get("workouts") or []
            if not health_absent and not health_err
            else []
        )
        # `connected` is only meaningful when strava actually returned a
        # payload; None means "don't know" (absent/errored), not "no".
        strava_connected = (
            strava.get("connected", False)
            if not strava_absent and not strava_err
            else None
        )
        strava_items = strava.get("activities") or [] if strava_connected else []

        merged, strava_only = _merge_workouts(health_items, strava_items)
        display = merged[:3] + strava_only[:3]

        if not display:
            # Nothing to show from either source. A blanket "none in the
            # last week" is exactly the confidently-wrong claim #195 was
            # filed against — say what each source actually reported,
            # including when one of them isn't connected/available at all,
            # rather than letting "empty" and "unknown" render identically.
            if health_err:
                health_part = "⚠️ Apple Health unavailable"
            elif health_absent:
                health_part = "Apple Health not measured"
            else:
                health_part = "none in Apple Health"

            if strava_err:
                strava_part = "Strava unavailable"
            elif strava_absent:
                strava_part = "Strava not measured"
            elif strava_connected is False:
                strava_part = "Strava not connected"
            else:
                strava_part = "Strava: none"

            lines.append(f"💪 Workouts: {health_part} ({strava_part}).")
        else:
            for w in merged[:3]:
                bit = f"💪 {w.get('type', 'workout')} — {w.get('duration_min', 0):.0f} min"
                if w.get("avg_hr_bpm"):
                    bit += f", avg HR {w['avg_hr_bpm']:.0f}"
                if w.get("_strava_matched"):
                    bit += " (also on Strava)"
                lines.append(bit)
            for a in strava_only[:3]:
                label = a.get("name") or a.get("type", "activity")
                bit = f"💪 {label} — {a.get('duration_min', 0):.0f} min (Strava)"
                lines.append(bit)

    return _section(HEADING_PULSE, "\n".join(lines))


# ---------------------------------------------------------------------------
# coffee — current bags only
# ---------------------------------------------------------------------------


def render_coffee(payload: dict) -> str:
    current = payload.get("coffee_current")
    if current is None:
        return ""  # gated off (no coffee data) or the section wasn't fetched
    if _is_error(current):
        return _section(HEADING_COFFEE, "⚠️ coffee data unavailable")
    coffees = current.get("coffees") or []
    if not coffees:
        return ""
    bits = []
    for c in coffees:
        label = f"**{c.get('name') or 'Unknown'}**"
        meta = ", ".join(x for x in (c.get("roaster"), c.get("process")) if x)
        if meta:
            label += f" ({meta})"
        bits.append(label)
    return _section(HEADING_COFFEE, "☕ Drinking: " + " · ".join(bits))


# ---------------------------------------------------------------------------
# transport — rail departures, weekdays only
# ---------------------------------------------------------------------------


def render_transport(payload: dict) -> str:
    meta = payload.get("_meta") or {}
    if meta.get("is_weekend"):
        return ""
    rail = payload.get("rail")
    if rail is None:
        return ""  # no station configured, or section not fetched
    if _is_error(rail):
        return _section(HEADING_TRANSPORT, "⚠️ Rail data unavailable")
    departures = rail.get("departures") or []
    if not departures:
        message = rail.get("message")
        return _section(HEADING_TRANSPORT, message) if message else ""
    parts = []
    for d in departures[:5]:
        sched = d.get("scheduled_departure") or "?"
        expected = d.get("expected_departure")
        dest = d.get("destination") or "?"
        if expected and expected != sched:
            parts.append(f"{sched} → {dest} (exp {expected})")
        else:
            parts.append(f"{sched} → {dest}")
    return _section(HEADING_TRANSPORT, "🚂 Next trains: " + ", ".join(parts))


# ---------------------------------------------------------------------------
# consumables — House: *_nearly_empty binary sensors that are on
# ---------------------------------------------------------------------------


def render_consumables(payload: dict) -> str:
    home = payload.get("home")
    if home is None:
        return ""
    if _is_error(home):
        return _section(HEADING_HOUSE, "⚠️ home status unavailable")
    appliances = ((home.get("sections") or {}).get("appliances")) or []
    nearly_empty = [
        e for e in appliances
        if (e.get("entity_id") or "").endswith("_nearly_empty")
        and (e.get("state") or "").lower() == "on"
    ]
    if not nearly_empty:
        return ""
    lines = [
        f"🧴 {e.get('friendly_name') or e.get('entity_id')} nearly empty — consider a #quick task."
        for e in nearly_empty
    ]
    return _section(HEADING_HOUSE, "\n".join(lines))


# ---------------------------------------------------------------------------
# snags — FYI list of snags created
# ---------------------------------------------------------------------------


def render_snags(payload: dict) -> str:
    """FYI list of newly captured snags.

    `snag_capture` **writes** (it registers snags), so `system_daily_brief`
    — read-only by design — never calls it and this key is never populated
    by the tool itself. This renderer stays generic over `snags_created` /
    `snags` / `sheet_url` regardless, so a caller who merges `snag_capture`'s
    own result into a payload dict before rendering (or a future read-only
    "what did snag_capture last report" source) gets a correct render for
    free. Through `system_daily_brief` alone, this section is always "".
    """
    created = payload.get("snags_created")
    if not created:
        return ""
    items = payload.get("snags") or []
    lines = []
    for s in items:
        line = f"{s.get('uid', '?')} · {s.get('room', '?')} · {s.get('description', '')}"
        if s.get("trade"):
            line += f" ({s['trade']})"
        lines.append(line)
    if not lines:
        lines.append(f"{created} new snag(s) captured.")
    sheet_url = payload.get("sheet_url")
    if sheet_url:
        lines.append(f"📋 Sheet: {sheet_url}")
    return _section(HEADING_SNAGS, "\n".join(lines))


# ---------------------------------------------------------------------------
# calendar — Today: events, all-day first
# ---------------------------------------------------------------------------


def render_calendar(payload: dict) -> str:
    events = payload.get("calendar")
    if events is None:
        return _section(HEADING_TODAY, NOT_MEASURED)
    if _is_error(events):
        return _section(HEADING_TODAY, "⚠️ calendar unavailable")
    if not isinstance(events, list):
        # Not the bare list `calendar.query`'s `today` always returns on a
        # real payload — an unrecognised shape, treated the same as "we
        # can't tell" rather than crashing on `.get()` below.
        return _section(HEADING_TODAY, NOT_MEASURED)
    if not events:
        return _section(HEADING_TODAY, "_Nothing on the calendar today._")
    all_day = [e for e in events if e.get("all_day")]
    timed = [e for e in events if not e.get("all_day")]
    lines = [f"- ALL DAY  {e.get('summary') or '(untitled)'}" for e in all_day]
    for e in timed:
        span = f"{_hhmm(e.get('start'))}–{_hhmm(e.get('end'))}"
        summary = e.get("summary") or "(untitled)"
        if summary == "(busy)":
            # Free/busy-only work events (`google_calendar/tools.py`'s
            # visibility filter) carry no title at all, only the calendar
            # they're on — a bare "(busy)" line said nothing useful. Render
            # the calendar label instead, e.g. "10:00–10:30  busy
            # (work@example.com)". Titled events keep their own summary.
            calendar_label = e.get("calendar") or "?"
            lines.append(f"- {span}  busy ({calendar_label})")
        else:
            lines.append(f"- {span}  {summary}")
    return _section(HEADING_TODAY, "\n".join(lines))


# ---------------------------------------------------------------------------
# listening — Last.fm counts, no mood/vibe opinion
# ---------------------------------------------------------------------------


def render_listening(payload: dict) -> str:
    recent = payload.get("lastfm_recent")
    stats = payload.get("lastfm_stats")
    if recent is None and stats is None:
        return ""  # gated off — no scrobbles for this user

    lines: list[str] = []
    if _is_error(recent):
        lines.append("⚠️ recent listening unavailable")
    elif isinstance(recent, list) and recent:
        lines.append(f"{len(recent)} recent scrobbles.")

    if _is_error(stats):
        lines.append("⚠️ listening stats unavailable")
    elif isinstance(stats, dict):
        total = stats.get("total_scrobbles")
        if total is not None:
            lines.append(f"{total} scrobbles this period.")
        top_artists = stats.get("top_artists") or []
        if top_artists:
            bits = ", ".join(f"{a['artist']} ({a['play_count']})" for a in top_artists[:3])
            lines.append(f"Heavy rotation: {bits}")
        top_genres = stats.get("top_genres") or []
        if top_genres:
            bits = ", ".join(g["genre"] for g in top_genres[:3])
            lines.append(f"Top genres: {bits}")

    if not lines:
        return ""
    return _section(HEADING_LISTENING, "\n".join(lines))


# ---------------------------------------------------------------------------
# freshness — table from alerts.data_freshness
# ---------------------------------------------------------------------------


def render_freshness(payload: dict) -> str:
    alerts = payload.get("alerts")
    if not isinstance(alerts, dict):
        return _section(HEADING_FRESHNESS, NOT_MEASURED)
    if _is_error(alerts):
        return _section(HEADING_FRESHNESS, "⚠️ freshness data unavailable")

    rows = alerts.get("data_freshness")
    if not rows:
        body = "_No freshness probes reported._"
    else:
        # `age`/`threshold` are pre-formatted display strings ("47m", "6h") —
        # the server already chose the unit (see brief docstring's reading
        # notes) — so this renderer can't re-derive staleness arithmetically
        # without a duration parser. It doesn't need to: `alerts.alerts`
        # already carries a "data stale" issue for exactly the rows that are
        # stale (see `system/tools.py::_render_alerts`, axis 2), so a row is
        # flagged here iff its integration appears there with such an issue,
        # or it has never had a single record (`latest` is null).
        stale_integrations = {
            entry.get("integration")
            for entry in (alerts.get("alerts") or [])
            if any("stale" in (issue or "") for issue in entry.get("issues") or [])
        }

        def _is_stale(row: dict) -> bool:
            return row.get("latest") is None or row.get("integration") in stale_integrations

        ordered = sorted(rows, key=lambda r: not _is_stale(r))
        table_lines = ["| Source | Latest | Age | Threshold | |", "|---|---|---|---|---|"]
        for row in ordered:
            latest = row.get("latest")
            latest_disp = _hhmm(latest) if latest else "—"
            age_disp = row.get("age") or "never"
            threshold_disp = row.get("threshold") or "?"
            mark = "⚠️" if _is_stale(row) else "✅"
            table_lines.append(
                f"| {row.get('integration', '?')} | {latest_disp} | {age_disp} | "
                f"{threshold_disp} | {mark} |"
            )
        body = "\n".join(table_lines)

    unmeasured = alerts.get("unmeasured")
    if unmeasured:
        body += "\n\n_Unmeasured (no probe): " + ", ".join(sorted(unmeasured)) + "._"

    return _section(HEADING_FRESHNESS, body)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

_RENDERERS = {
    "alerts": render_alerts,
    "pulse": render_pulse,
    "coffee": render_coffee,
    "transport": render_transport,
    "consumables": render_consumables,
    "snags": render_snags,
    "calendar": render_calendar,
    "listening": render_listening,
    "freshness": render_freshness,
}

# Sections excluded from the prewarm's rendered-markdown cache — every
# section whose payload keys come from a `volatile=True` Source in
# `brief.py::build_sources` (alerts, calendar, reminders/tasks, home/house,
# rail/transport, and all four health_* sources feeding Pulse). This is the
# same "never cache volatile" rule the module docstring states for raw
# sources, applied one level up: caching a *rendered* fragment built from a
# prewarm payload with a volatile key missing (`include_volatile=False`)
# would freeze that fragment at "not measured"/omitted for the whole cache
# TTL even once the live source is available — the exact stale-plausible-zero
# failure `brief.py`'s docstring warns about, just moved from a number to a
# markdown fragment. Only Coffee, Snags and Listening have no volatile
# dependency at all, so only those three are ever written to the rendered
# cache; the rest are always recomputed fresh from the live payload.
NEVER_CACHE_RENDERED = frozenset({"alerts", "transport", "pulse", "consumables", "calendar", "freshness"})


def render_all(payload: dict, sections: tuple[str, ...] = SECTIONS) -> dict[str, str]:
    """Render every requested section over `payload`. Pure — no I/O, no cache."""
    return {name: _RENDERERS[name](payload) for name in sections if name in _RENDERERS}
