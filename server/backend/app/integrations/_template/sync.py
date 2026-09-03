"""Pull/store split for the `_template` scaffold integration — V4 chunk 4.3e.

`app.plugin.bases.SourceIntegration.sync()` (inherited, unmodified, by
`TemplateIntegration` in `__init__.py`) calls these two functions per
account, in this order, for every scheduled or manually-triggered sync:

  1. `pull_items()` — fetch, no persistence.
  2. `store_items()` — persist, no outbound I/O.

Keeping them as two separate top-level functions (rather than one combined
"sync_template()") is what let `google_calendar`/`lastfm`/`weather` etc. drop
their hand-rolled fan-out loops entirely in V4 chunks 4.1/4.3 — the kernel's
`SourceIntegration.sync()` does the account fan-out, cursor bookkeeping, and
error classification/aggregation (`app.plugin.sync_runtime.fan_out`) around
whatever `pull`/`store` you write. Your job is only ever "fetch" and
"persist"; never re-implement the loop around them.

Relative imports below (`.client`, `.models`) — see `__init__.py`'s module
docstring for why this scaffold uses them instead of the codebase's usual
absolute `app.integrations.<name>.module` convention: they need to keep
resolving correctly when this file is copied to a differently-named
directory, before the rename is done.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .client import fetch_items
from .models import TemplateItem
from app.plugin.bases import PullResult
from app.plugin.config_store import plugin_config

logger = logging.getLogger(__name__)


def pull_items(user_id: int, session: Session, cursor: str | None) -> PullResult:
    """Fetch one page of items for `user_id`. No DB writes.

    `cursor`, when set, is the value `SourceIntegration.sync()` read back
    from the last successful `store_items()` call for this (integration,
    user, cursor_key) — see `TemplateIntegration.cursor_key` in `__init__.py`.
    This scaffold opts into cursor bookkeeping to demonstrate the pattern;
    an integration that always re-fetches a bounded window (like
    google_calendar's rolling 30-day pull) would instead leave `cursor_key`
    unset and ignore this parameter.
    """
    cfg = plugin_config("__TEMPLATE_INTEGRATION_NAME__")
    records, next_cursor = fetch_items(cfg.api_key, page_size=cfg.page_size, cursor=cursor)
    return PullResult(records=records, cursor=next_cursor)


def store_items(session: Session, records: list[dict]) -> int:
    """Upsert `records` (one account's page of items) and return the count
    persisted. No outbound I/O — that's `pull_items()`'s job.

    Upserts by `(user_id, external_id)` — the same natural key the model's
    `UniqueConstraint` declares (see models.py). `records` already carry
    `user_id` because `TemplateIntegration.pull()` (in `__init__.py`) stamps
    it onto each dict before handing them to `store()` — the same "thread
    the owning user_id with the record" pattern `google_mail`'s
    `pull_mail()` uses, since this base class's `store(session, records)`
    signature receives only the records, not the account.
    """
    if not records:
        return 0

    count = 0
    for record in records:
        existing = (
            session.query(TemplateItem)
            .filter_by(user_id=record["user_id"], external_id=record["external_id"])
            .first()
        )
        if existing:
            existing.title = record["title"]
            existing.fetched_at = record["fetched_at"]
        else:
            session.add(TemplateItem(
                user_id=record["user_id"],
                external_id=record["external_id"],
                title=record["title"],
                fetched_at=record["fetched_at"],
            ))
        count += 1

    session.commit()
    logger.info(f"template: synced {count} items")
    return count
