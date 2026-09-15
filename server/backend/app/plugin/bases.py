"""Typed integration base classes — V4 chunk 4.1.

`app/integrations/base.py`'s `BaseIntegration` is deliberately minimal (one
ABC for all 19+ integrations), which meant wildly different integration
shapes — polling sources, push-fed sources, bidirectional read/write, pure
action wrappers, and internal capability services with no external system
at all — each re-implemented the same plumbing the kernel now provides:
manifests (chunk 1.x), lifecycle/scheduling (chunk 3.1), and the shared sync
runtime — HTTP error classification, multi-account fan-out, cursor
bookkeeping (chunk 3.2).

This module introduces the type hierarchy the manifests already name in
their `type` field (`"source" | "push_source" | "bidirectional" | "action" |
"capability" | "system"`) so a new integration's package only has to write
the handful of methods that are actually specific to it. Every class here
subclasses the existing `BaseIntegration`, so integrations that haven't been
converted yet keep working completely unchanged — this is a pure addition,
not a breaking change to the ABC.

`google_calendar` is the first (and, as of this chunk, only) conversion —
see `app/integrations/google_calendar/__init__.py` for the reference shape
future integrations should copy.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.base import BaseIntegration
from app.plugin.sync_runtime import SyncCursor, fan_out

logger = logging.getLogger(__name__)


@dataclass
class PullResult:
    """One account's `pull()` output.

    `records` are handed to `store()` as-is — `SourceIntegration` never
    inspects their shape, so a subclass is free to use dicts, dataclasses,
    or ORM-ready kwargs, whatever its own `store()` expects.

    `cursor`, if set, is persisted via `SyncCursor.set()` once `store()`
    returns successfully — opt-in, only meaningful when the integration also
    sets `cursor_key`. Leave `None` for integrations (like google_calendar)
    that re-fetch a bounded window every sync rather than resuming from a
    cursor.
    """

    records: list[Any] = field(default_factory=list)
    cursor: str | None = None


@dataclass
class ActionResult:
    """Outcome of a `BidirectionalIntegration`/`ActionIntegration` outbound
    action (`execute_action()`). Deliberately generic — the action payload
    shape is defined by each integration's own command/action type, not by
    this base."""

    ok: bool
    detail: str | None = None
    data: dict[str, Any] | None = None


class SourceIntegration(BaseIntegration):
    """Template-method base for read-only pull sources.

    Subclasses write exactly three things:

      - `accounts(session)` — the list of opaque per-account keys to fan out
        over (an `OAuthToken` row, an account email string, a config row —
        whatever the integration's own `pull()`/`store()` expect). Default:
        a single `[None]` "account", for integrations with no multi-account
        concept (override is only needed for multi-account integrations like
        google_calendar/google_mail).
      - `pull(account, session, cursor) -> PullResult` — fetch from the
        external system. No persistence here.
      - `store(session, records) -> int` — persist `records` (upsert +
        prune-stale, if relevant) and return the count persisted. No
        outbound I/O here.

    `sync()` itself is fully implemented here: resolve `accounts()`, fan out
    over them via `app.plugin.sync_runtime.fan_out` (identical
    success/failure aggregation semantics to the old hand-rolled
    google_calendar/google_mail loops — see that module's docstring),
    calling `pull()` then `store()` per account, and — when `cursor_key` is
    set — reading/writing the account's `SyncCursor` around the call. A
    subclass never re-implements fan-out, error classification, or cursor
    bookkeeping; it only ever touches `pull`/`store`.
    """

    #: Set to a non-None key to opt into automatic `SyncCursor` bookkeeping
    #: (get before `pull()`, set after a successful `store()`). Integrations
    #: that re-fetch a fixed window every sync (google_calendar) leave this
    #: `None` — nothing reads/writes a cursor row for them.
    cursor_key: str | None = None

    def accounts(self, session: Session) -> list[Any]:
        """Return the accounts to sync. Default: a single `None` sentinel —
        override for multi-account integrations."""
        return [None]

    def account_user_id(self, account: Any) -> int | None:
        """The owning user_id for this account, if any — used to scope the
        `SyncCursor` row when `cursor_key` is set. Default: no owning user
        (single-tenant / household-shared integrations)."""
        return None

    def account_label(self, account: Any) -> str:
        """Human-readable label for logs. Default: `str(account)`."""
        return str(account)

    @abstractmethod
    def pull(self, account: Any, session: Session, cursor: str | None) -> PullResult:
        """Fetch new/changed records for one account from the external
        system. Must not write to the DB — that's `store()`'s job."""

    @abstractmethod
    def store(self, session: Session, records: list[Any]) -> int:
        """Persist `records` for one account (upsert + prune-stale, if
        relevant) and return the count persisted. Must not perform outbound
        I/O — that's `pull()`'s job."""

    def sync(self) -> None:
        """Template method — plain `def`, not a coroutine (the scheduler
        runs every integration's `sync()` via `asyncio.to_thread`; see
        `tests/test_sync_contract.py`). Internally bridges to the async
        `fan_out` helper via `asyncio.run`, exactly like the pre-4.1
        hand-rolled google_calendar/google_mail `sync()` methods did.
        """
        from app.db import get_db

        db = get_db()
        with db.session() as session:
            accounts = self.accounts(session)
            if not accounts:
                logger.info(f"{self.name}: no accounts configured — skipping sync")
                return

            def _sync_one(account: Any) -> int:
                cursor = None
                if self.cursor_key:
                    cursor = SyncCursor.get(
                        session,
                        self.name,
                        self.cursor_key,
                        user_id=self.account_user_id(account),
                    )

                result = self.pull(account, session, cursor)
                count = self.store(session, result.records)

                if self.cursor_key and result.cursor is not None:
                    SyncCursor.set(
                        session,
                        self.name,
                        self.cursor_key,
                        result.cursor,
                        user_id=self.account_user_id(account),
                    )
                return count

            fan_result = asyncio.run(
                fan_out(accounts, _sync_one, label=f"{self.name} accounts")
            )
            total = sum(fan_result.succeeded)
            logger.info(
                f"{self.name} sync complete: {total} records, "
                f"{len(fan_result.succeeded)}/{len(accounts)} accounts ok"
            )


class PushSourceIntegration(BaseIntegration):
    """Base for integrations whose data arrives via an inbound push route
    (e.g. a bridge container POSTing to the kernel, or a phone app hitting
    an ingest endpoint) rather than an outbound poll.

    No `sync()` — the kernel never schedules a pull for these. Instead the
    integration declares its ingest route(s) via its manifest's `routes`
    field, and (optionally) a `probe()` liveness check that the kernel can
    schedule and record to `SyncState`, formalizing what the whatsapp bridge
    heartbeat does today (`app.integrations.whatsapp` is NOT converted onto
    this base in this chunk — this just defines the interface it will adopt
    later).
    """

    def sync(self) -> None:
        raise NotImplementedError(
            f"{self.name} is push-based — data arrives via its ingest "
            "route(s), not a scheduled sync(). The kernel should call "
            "probe() for liveness instead."
        )

    async def probe(self) -> bool:
        """Liveness check for the upstream push source (e.g. "is the bridge
        container's health endpoint responding"). Only needed if the
        integration's manifest declares a `background_tasks` liveness job
        that calls it. Default: unimplemented."""
        raise NotImplementedError


class BidirectionalIntegration(SourceIntegration):
    """`SourceIntegration` plus outbound command handling.

    Everything about the read side (`accounts`/`pull`/`store`/`sync`) is
    inherited unchanged from `SourceIntegration`. The write side is a single
    additional method: `execute_action()`. This chunk only defines the
    interface — formalizing it into a queue/dispatch pattern (the way
    `apple_reminders` already does with its command table + SSE callback) is
    chunk 4.3's job. `google_calendar`'s `create_event` tool still calls
    `client.create_event()` directly from its tool handler for now, same as
    before this chunk.
    """

    async def execute_action(self, action: Any) -> ActionResult:
        """Perform one outbound write against the external system. Not yet
        wired into any dispatch path — see class docstring."""
        raise NotImplementedError


class ActionIntegration(BaseIntegration):
    """No data pull, no cached table — tools with `.write` capabilities that
    hit an external system directly from the tool handler (nothing to sync
    on a schedule). `sync()`/`dashboard_data()` default to no-ops; override
    only if the integration turns out to have something worth showing."""

    def sync(self) -> None:
        return None

    async def dashboard_data(self) -> dict[str, Any]:
        return {}


class CapabilityService(BaseIntegration):
    """No external system at all — serves tools and intra-plugin calls from
    in-process state or by composing other integrations (e.g. `system`,
    `sheets`). `sync()` is a no-op; `is_configured()` already defaults (via
    `BaseIntegration`) to a manifest `config_schema` check, which is
    vacuously true for the common case of an empty schema."""

    def sync(self) -> None:
        return None

    async def dashboard_data(self) -> dict[str, Any]:
        return {}
