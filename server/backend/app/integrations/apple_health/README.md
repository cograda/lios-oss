# Apple Health

Push-fed integration (`type="push_source"`) — see `manifest.py` and
`routes.py`'s module docstring for the push pipeline itself
(Health Auto Export iOS app → `POST /api/health/push` → `parse_export.py` →
`sync.py::sync_from_push` → Postgres). This file covers one specific,
recurring failure: a Strong-app workout that never appears anywhere comar
reads from, investigated for issue #195(a) as a follow-up to #184.

## Where a Strong workout can go missing (issue #195a / #184)

**#184 (2026-09-08) and #195 (2026-09-09) are the same gap recurring three
days apart with two different workouts** (a Thu 3 Sept squat session, a Tue
8 Sept bench/pull-up session) — not two unrelated one-offs. Two occurrences
with different workout content rules out a single corrupt record; a
structural cause has to sit somewhere every Strong session passes through.

### The chain, and where comar's code stops being able to see it

```
Strong app  →  writes to HealthKit  →  Health Auto Export reads HealthKit
   (device)         (device)              on its own schedule, exports
                                           whatever data types are enabled
                                                    │
                                                    ▼
                                    POST /api/health/push  (comar)
                                                    │
                                    parse_export.py: parse_health_auto_export()
                                                    │
                                    sync.py: sync_from_push() → HealthWorkout row
```

Everything left of the `POST` is on the phone and outside comar's
visibility entirely — comar has no signal for "Strong tried to write a
workout to HealthKit and didn't" or "Health Auto Export's export set
doesn't include Workouts today". A gap there is indistinguishable, from
comar's side, from "nothing happened" (no push arrives at all, or a push
arrives with no workout in it — both look identical downstream).

### What was actually checked in the code (not assumed)

- **`parse_export.py::parse_health_auto_export` applies no source/app
  filter.** It iterates `data.get("workouts", [])` unconditionally
  (line ~172 onward) — there is no check anywhere for which app or device
  wrote a workout into HealthKit. If a Strong workout reaches Health Auto
  Export's export payload at all, nothing in this function would exclude
  it.
- **The one thing that *does* drop a workout record: missing `start`/`end`.**
  `w.get("id", _synthetic_uid(...))` always produces a usable `uid` even
  with no native id, but a workout with no parseable `start`/`end` is
  skipped and recorded in the `skipped` list returned to the caller
  (`routes.py` writes that into `SyncState.last_error` via `_record_push`,
  status still `"ok"` — see that module's docstring for why). This is a
  live, checkable question (below), not a guess.
- **`sync_from_push` dedupes on `(user_id, uid)`, upserting** — a re-sent
  workout (Health Auto Export's push window is a trailing 7 days, per
  `routes.py`) converges on one row, never silently discarded for being a
  repeat.
- **No date-window or timezone bug found in the read path.**
  `handle_health_workouts` filters `start_time >= now - timedelta(days=7)`
  against `HealthWorkout.start_time`, which is stored timezone-aware
  (`_parse_dt` in `sync.py` defaults a *naive* incoming timestamp to UTC —
  correct for Health Auto Export, which always sends an offset, and a
  choice `health_workout_add`, issue #185, deliberately does NOT copy for
  human-typed input — see that tool's docstring). Nothing here would make
  a workout that arrived get excluded from the query.

**Conclusion: no code-level bug was found that would explain a workout
silently failing to store once it reaches `sync_from_push`.** Two
recurrences of "the workout never shows up in `health_workouts`, over a
week each time" argue against a transient push failure too — the trailing
7-day resend window means a genuinely transient drop should self-heal on
the very next successful export, and both cases stayed missing for the
whole week they were checked. That points at something upstream of the
`POST` — most likely Health Auto Export's on-device export configuration
not including workouts from Strong specifically (a per-data-type toggle in
that app, not something comar's push route ever sees), or Strong's own
HealthKit-write step not completing for a given session. Neither is
observable from server-side code or logs.

### The concrete live check that would settle it (not run — no production
access from this environment)

1. `health_workouts` with `days=14` — confirm whether *any* row exists near
   2026-09-03 or 2026-09-08, under *any* `type` (a mislabeled workout_type
   would still count as "the payload arrived").
2. If none: check whether **other** data from the same push arrived for
   those dates — `health_trends`/`health_today` for `steps` /
   `active_energy_kcal` on 2026-09-03 and 2026-09-08. If daily metrics
   *are* present for those dates but the workout is not, the push itself
   happened and reached comar successfully, but never *contained* a
   workout record — which locates the gap upstream, at Health Auto
   Export's export configuration or Strong's HealthKit write, not in
   comar's ingestion.
3. Inspect `SyncState.last_error` for `integration="apple_health"` (surfaced
   by `system_alerts`) around those two dates — a `"workout ...: ..."`
   entry in `skipped` would mean the record *did* arrive but failed
   comar's own `start`/`end` parse (the one code-level drop point found
   above); its absence rules that out.
4. If (1) finds nothing and (2) shows daily metrics present: the fix is on
   the phone (re-check Health Auto Export's "Workouts" export toggle and
   Strong's Apple Health write permission), not in this repo. `#185`'s
   `health_workout_add` tool is the mitigation regardless of root cause —
   see below.

## `health_workout_add` (issue #185)

A manual write path into the same `health_workouts` table every reader
(`health_workouts`, `health_exercise_status`, `health_weekly_summary`,
`health_summary`) already queries — so a workout recovered by hand (e.g.
from a Strong capture-confirmation email, as in #184) can be recorded as
structured data instead of only as prose in a daily note. See its tool
description in `tools.py` for the input contract; `uid` is generated as
`manual:<uuid4>` (HealthWorkout's uniqueness is `(user_id, uid)`, and every
synced row's `uid` is an HKObject UUID, so this prefix can never collide),
and a naive `started_at`/`ended_at` is refused rather than guessed — the
opposite of `sync.py::_parse_dt`'s default-to-UTC behaviour for a push
payload, deliberately, because a human typing a time carries no such
guarantee.

This tool does not fix the Strong→HealthKit gap above — it exists because
that gap may never be fixable from here at all.
