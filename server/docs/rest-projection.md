# The REST projection: tools, batch, and the envelope

Step 1 of the "one data layer" plan's migration order
(`vault/Projects/lios/Plans/2026-09-11 One data layer — REST, MCP and GraphQL
for web apps.md`, §5). Covers what shipped in that step: `POST
/api/v1/batch`, the envelope, and per-tool HTTP caching. Everything here sits
on top of the existing `POST /api/v1/tools/{name}` / `GET /api/v1/tools`
projection (`app/api/v1.py`), which is unchanged in shape by default — see
"The envelope" below for why.

## The envelope

Every response in this family is meant to converge on one shape:

```jsonc
// success
{"ok": true, "result": <tool's own JSON>, "warnings": [{"code", "message"}]?}

// failure
{"ok": false, "error": {"code": "...", "message": "...", "retryable": true|false}}
```

`warnings` carries **non-primary** side-effect problems — the model is: `ok`
means the primary effect happened, even if something secondary (a Sheets
mirror, say) did not. Its first occupant was the tasks tools'
`render_skipped`/`render_error` pattern (#204) — those fields stayed on
`result` and were additionally lifted into `warnings` so a caller that only
read the envelope still saw them. **That pattern is gone (2026-09-14,
2026-09-14):** `Task Backlog.md`'s render became unconditional — the
edit-detection guard that produced a skipped render was itself the cause of
an outage and was removed outright — so no `tasks_*` tool emits
`render_skipped`/`render_error` any more, and `_warnings_from_result` (the
folder that watched for them) was deleted with it. `warnings` stays part of
the envelope shape for whatever the next producer of a partial-success
condition turns out to be; there is simply none today.

Error `code` is one of a small closed set (`app/api/error_codes.py`):
`not_found`, `invalid_args`, `forbidden`, `unknown_tool`, `timeout`,
`upstream`, `internal`. `retryable` defaults per code (`timeout`/`upstream`
are retryable; everything else is not) and can be overridden per error.

### `POST /api/v1/batch` speaks the envelope unconditionally

New route, no deployed consumer yet (apps/loops migrates to it in a later
step of the same plan), so there's no existing contract to protect.

```jsonc
POST /api/v1/batch
{"items": [{"id": "1", "tool": "tasks_query", "args": {}}, ...]}
// a bare JSON list is also accepted, as shorthand for {"items": [...]}
```

```jsonc
{"ok": true, "results": [
  {"id": "1", "ok": true, "result": {...}, "warnings": [...]?},
  {"id": "2", "ok": false, "error": {"code": "unknown_tool", "message": "...", "retryable": false}}
]}
```

Properties worth relying on:

- **Reuses `dispatch_tool()` directly** — the same chokepoint the single-tool
  route calls, under the same authenticated `user` for every item. A batch
  item is scoped exactly as the same call would be on its own; it cannot
  widen what the caller's bearer already permits (no per-item auth
  override exists to widen it with).
- **Results are always in input order**, regardless of which item's
  dispatch finishes first.
- **A per-item failure never fails the batch.** An unknown tool name, a
  capability/scope refusal (e.g. a `readonly` bearer calling a non-read-only
  tool), a handler error, or a per-item timeout are all reported as that
  item's own `{"ok": false, "error": {...}}` entry. Only a malformed
  request — bad JSON, no `items`, too many items — fails the whole request
  with a top-level `{"ok": false, "error": ...}` and a 4xx status.
- **Caps and budget are config**, not hardcoded: `settings.batch_max_items`
  (`HOME_BATCH_MAX_ITEMS`, default 25) and
  `settings.batch_timeout_seconds` (`HOME_BATCH_TIMEOUT_SECONDS`, default
  30) bound items-per-request and the whole batch's wall-clock budget. An
  item still running when the budget expires is reported as its own
  per-item `timeout` — the budget bounds the *request*, not any one item
  (each item still has its own `dispatch_tool()` per-call ceiling, 60s).

### `POST /api/v1/tools/{name}` — envelope is opt-in

This route is not new, and it's not just used by this repo: the lios-sync
daemon (`core/client/src/lios_sync/server_client.py::call_tool`) and
`apps/loops` (`apps/loops/backend/app/core_api.py::call_tool`) are both
deployed today, reading its shape as it's always been —
`{"ok": false, "error": "<plain string>"}` on failure, no `warnings` key.
Changing that shape under them, in this change, would break a deployed
daemon and a deployed app for the sake of a contract neither has asked for
yet.

So the default response is **byte-for-byte unchanged**. Send either header to
opt into the richer envelope instead:

```
X-Lios-Envelope: 1
```

or

```
Accept: application/vnd.lios.envelope+json
```

With the header, `error` becomes the `{"code", "message", "retryable"}`
object and a successful response may carry `warnings`, following the exact
same rules as batch. Without it, nothing changes.

## Caching: ETag + Cache-Control on read-only tools

Any tool call whose registered annotations carry `readOnlyHint: true` (the
same annotation `app/plugin/dispatch.py` reads to enforce `readonly`-scoped
bearers) gets, on `POST /api/v1/tools/{name}` success:

- `ETag`: a hash of the exact response body that would be sent. Send it back
  as `If-None-Match` on a later identical call to get a `304 Not Modified`
  instead of the body.
- `Cache-Control: private, max-age=<N>`, where `N` comes from
  `app/api/freshness_hints.py`'s per-tool table — or `no-store` if the tool
  isn't listed (0 is the default: caching is opt-in per tool, not assumed
  safe). First three: `weather_current`/`weather_forecast` at 600s,
  `calendar_today`/`calendar_list_events`/`calendar_next_events` at 60s,
  `tasks_query` explicitly at 0 (read straight back after being written —
  must never look stale).

A write tool (no `readOnlyHint`, or `readOnlyHint: false`) never gets these
headers, on either shape of the response. `POST /api/v1/batch` does not set
per-call caching headers on the aggregate response — a batch mixes tools
with different freshness, so there's no one `Cache-Control` that would be
honest for the whole thing.

## Error codes

`app/api/error_codes.py::error_obj(code, message, retryable=None)` builds the
error object and coerces an unrecognised code to `internal`. Classification
from a `dispatch_tool()` outcome (`app/api/v1.py::_classify_dispatch_error`)
is necessarily best-effort today: `dispatch_tool` itself collapses every
failure mode into one string (see `app/plugin/dispatch.py::ToolResult`), so
this layer pattern-matches the few messages that already carry real
structure (an unknown-tool 404, a timeout, the read-only-scope refusal) and
falls back to `internal` for everything else. Threading a typed error
through `dispatch_tool` itself — so a handler's own `PermanentError`/
`not_found`/`invalid_args` distinctions survive to this layer — is future
work, not part of this step.
