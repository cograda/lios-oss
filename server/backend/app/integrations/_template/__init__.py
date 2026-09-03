"""Template scaffold integration — V4 chunk 4.3e.

Copy this whole `_template/` directory to `app/integrations/<your_name>/`,
search-and-replace the literal placeholder string
`__TEMPLATE_INTEGRATION_NAME__` with `<your_name>` across every file in the
copy (it appears in `manifest.py::MANIFEST.name`, this file's `name`
property, `sync.py`'s `plugin_config()` call, and `tools.py`'s ping
payload), then edit `manifest.py`/`models.py`/`client.py`/`sync.py`/
`tools.py` to describe your real integration. Full walkthrough:
`server/docs/writing-an-integration.md`.

This class is a `SourceIntegration` (see `app/plugin/bases.py`) — the
template-method base for "poll an external system on a schedule, persist
what changed". It writes exactly three things beyond the ABC's identity
properties:

  - `accounts(session)` — which per-account keys to fan out over. This
    scaffold treats every active `User` as one "account" (a plausible shape
    for a per-user external service with no multi-account concept per
    user — contrast with google_calendar, where one *user* can have several
    Google *accounts*, each needing its own `pull`/`store` call).
  - `pull(account, session, cursor)` / `store(session, records)` — thin
    one-line adapters onto `sync.py`'s `pull_items`/`store_items`, exactly
    like `GoogleCalendarIntegration` (`app/integrations/google_calendar/__init__.py`)
    delegates to `sync.py::pull_calendar_events`/`store_calendar_events`.

`sync()` itself is NOT written here — it's fully inherited from
`SourceIntegration`, which resolves `accounts()`, fans out over them
(`app.plugin.sync_runtime.fan_out`), and handles the optional `SyncCursor`
bookkeeping around each account's `pull`/`store` pair. You never re-implement
that loop.

RELATIVE IMPORTS, deliberately, for the two lines below (and the equivalent
ones in `sync.py`/`tools.py`) — every OTHER integration in this codebase
uses absolute `from app.integrations.<name>.module import ...` imports for
its own intra-package wiring (see `google_calendar/__init__.py`), because a
real integration is named once, permanently, and never moves. `_template`
is the one package designed to be copied wholesale to a new directory name
— a hardcoded `from app.integrations._template.sync import ...` would keep
resolving to THIS original, uncopied package even after the copy is renamed
(Python resolves absolute dotted paths against `sys.modules`/the real
package tree, not against "whichever copy you meant"), silently importing
the wrong module's `TemplateItem` and registering ITS table under the
literal, never-renamed placeholder name — exactly the bug that broke CI the
first time this scaffold shipped (see `tests/test_drop_in_integration.py`'s
module docstring, "FIXUP" section). Relative imports (`from .sync import
...`) resolve against `__package__` at import time, so they correctly
follow the copy to wherever it was dropped. Once you've done the rename
(and, if you like, switched back to the codebase's usual absolute-import
convention — nothing requires keeping these relative after that point),
this stops mattering.
"""

from typing import Any

from sqlalchemy.orm import Session

from .sync import pull_items, store_items
from .tools import get_mcp_tools
from app.models.users import User
from app.plugin.bases import PullResult, SourceIntegration


class TemplateIntegration(SourceIntegration):
    """Scaffold `SourceIntegration` — copy this shape for a new poll-based
    integration. See the module docstring above for the rename steps."""

    # Opts into SyncCursor bookkeeping (get before pull(), set after a
    # successful store()) — demonstrates the pattern `sync.py::pull_items`
    # relies on. Leave this `None` (the base class default) if your real
    # integration always re-fetches a bounded window every sync instead of
    # resuming from a cursor (google_calendar's shape).
    cursor_key = "page"

    @property
    def name(self) -> str:
        # MUST equal this package's directory name and manifest.py's
        # MANIFEST.name — see manifest.py's module docstring.
        return "__TEMPLATE_INTEGRATION_NAME__"

    @property
    def display_name(self) -> str:
        return "Template Integration"

    def accounts(self, session: Session) -> list[User]:
        """One "account" per active user. Real integrations with their own
        multi-account concept (google_calendar, google_mail) instead query
        their own OAuthToken rows here — see those packages' `accounts()`.
        """
        return session.query(User).filter_by(is_active=True).all()

    def account_user_id(self, account: User) -> int | None:
        return account.id

    def account_label(self, account: User) -> str:
        return account.name

    def pull(self, account: User, session: Session, cursor: str | None) -> PullResult:
        result = pull_items(account.id, session, cursor)
        # Stamp the owning user_id onto each record now — store()'s
        # SourceIntegration signature only receives `records`, not the
        # account, so the owning user has to travel with the row (same
        # reasoning google_mail's pull_mail() follows for account_email).
        for record in result.records:
            record["user_id"] = account.id
        return result

    def store(self, session: Session, records: list[dict]) -> int:
        return store_items(session, records)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return get_mcp_tools()

    async def dashboard_data(self) -> dict[str, Any]:
        """Return summary data for the web dashboard. Override with
        something real once this integration has data worth summarising —
        an empty dict is a valid, honest default for a scaffold."""
        return {}

    # is_configured() is NOT overridden — BaseIntegration's default
    # (`app.plugin.config_store.is_configured_from_schema`) already checks
    # that every `required` config_schema key (here: `api_key`) resolves to
    # a truthy value. Only override this if you need a real connectivity
    # probe beyond "is the key present" (see obsidian: checks the vault
    # mount exists on disk; google_mail: checks a token has the right scope).
