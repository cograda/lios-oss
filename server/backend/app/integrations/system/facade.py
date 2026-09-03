"""system's facade — V4 chunk 4.3e.

Originally added for one caller: `app/routes/system.py`'s `GET /system/alerts`
route needs to call the `handle_alerts` tool handler directly (a read-only
wrapper so the dashboard can render the same payload the `system_alerts` MCP
tool returns, without going through the MCP transport) — that's a kernel route
reaching into an integration package's internals, which the CI import-boundary
guard (`tests/test_kernel_import_guard.py`) does not allow except via
`<pkg>.facade`.

Since 2026-07-31 the same method is also a declared capability,
`provides=["system.alerts"]` (see manifest.py), consumed by `notifications` —
which sweeps this payload on a cron, deduplicates it against its own ledger and
pushes the delta to ntfy. So `system` is no longer a pure *consumer* of
capabilities, though it remains a pure composer of other integrations' data:
this facade exposes only what a kernel caller genuinely can't do otherwise, and
adding a method needs the same justification the first one had.

`clear_brief_cache` is the second, on that basis: `app/routes/preferences.py`
must invalidate a user's cached daily brief when their preferences change,
and a route may not import `app.integrations.system.brief` directly.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.integrations.system import tools as _tools


class SystemFacade:
    def alerts(self, session: Session, arguments: dict[str, Any] | None = None) -> str:
        """Per-caller view: scoped to whichever user is bound via `use_user`,
        or the household view if nothing is bound (see `handle_alerts`)."""
        return _tools.handle_alerts(session, arguments or {})

    def alerts_household(self, session: Session, arguments: dict[str, Any] | None = None) -> str:
        """The whole-fleet view, independent of any ambient user context.

        Added alongside the per-user scoping fix (F-alerts-scoping): the
        notifications sweep (cron, no request context at all) and the
        dashboard route (cookie auth, no per-user binding) both need the
        household view *unconditionally* — not "whichever view the ambient
        context happens to produce today". Call this instead of `alerts()`
        whenever the caller genuinely cannot be scoped down.
        """
        return _tools.handle_alerts_household(session, arguments or {})

    def clear_brief_cache(self, user_id: int | None = None) -> None:
        """Drop cached daily-brief sources for a user (or everyone).

        Preferences shape both *what* the brief fetches and *how*, so a stale
        cached payload would keep the old behaviour until the TTL expired —
        confusing precisely when someone has just changed a setting and
        re-run the command to see the effect.
        """
        from app.integrations.system import brief

        brief.clear_cache(user_id)


FACADE = SystemFacade()
