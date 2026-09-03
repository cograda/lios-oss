"""inbox's facade.

Exists for the same single reason `system`'s does: a kernel route needs to call
into this package, and `tests/test_kernel_import_guard.py` allows kernel code to
cross into an integration only via `<pkg>.facade`. Here the caller is
`app/routes/inbox.py`'s `POST /api/inbox/ingest`, which enriches a file inline so
its JSON response can carry a meaningful summary for the caller's confirmation
message (2026-07-31).

No `provides` entry in the manifest: capability declarations are for
*integration-to-integration* wiring resolved through
`app.plugin.capabilities.get_capability()`. This is a kernel route with a fixed
1:1 dependency, so it imports this module directly — the same shape `system`
used before it gained a second, genuinely cross-integration consumer.

`system`'s daily brief looked like it would be that second consumer and is
deliberately not: `inbox` depends on `notify.push`, and `notifications`
depends on `system.alerts`, so `system` consuming `inbox` closes a dependency
cycle that boot validation rejects. See `system/manifest.py` for the full
reasoning — the daily-note command calls `inbox_pending` directly instead.

Methods are all things the ingest route genuinely has to do: resolve which user
is calling to a filesystem location (F6, 2026-08-08 — the inbox went per-user),
identify content, recognise a duplicate, record ownership, and describe what
landed. The route still has no business reaching further into `scan`'s
sniffers, sidecar I/O or bucket-transition helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class InboxFacade:
    def default_user_id(self) -> int:
        """User id attributed to callers with no per-caller identity —
        the shared `inbox_token` webhook secret, or the UI token — both of
        which authenticate a *request*, not a *user* (see
        `app/routes/inbox.py::_resolve_caller_user_id`). See
        `scan.LEGACY_OWNER_USER_ID` for why that's user 1."""
        from app.integrations.inbox import scan

        return scan.LEGACY_OWNER_USER_ID

    def bucket_dir_for(self, user_id: int, bucket: str) -> Path:
        """Where the ingest route should write a new file for `user_id`."""
        from app.integrations.inbox import scan

        return scan.user_root(user_id) / bucket

    def record_ingest(self, user_id: int, bucket: str, filename: str, *, sha256: str | None = None) -> None:
        """Record ownership of a just-written file in the `InboxItem` ledger."""
        from app.integrations.inbox import scan

        scan.record_item(user_id, f"{bucket}/{filename}", sha256=sha256)

    def content_hash(self, data: bytes) -> str:
        """Stable identity for uploaded bytes, for the dedup check below."""
        from app.integrations.inbox import scan

        return scan.content_hash(data)

    def find_by_hash(self, digest: str, user_id: int) -> Path | None:
        """An already-ingested file owned by `user_id` with this content,
        or None.

        The server is the only place dedup can be authoritative: the Tines/phone
        producer and the Mac watcher can't see each other's uploads, and a
        duplicate costs a second transcription. Scoped per user (F6) — a
        duplicate check must never match against another user's upload.
        """
        from app.integrations.inbox import scan

        return scan.find_by_hash(digest, user_id)

    def summary_for(self, path: Path, *, size_bytes: int | None = None) -> str:
        """Render the one-line summary for a file already on disk.

        Used on the duplicate path, where there's nothing to enrich — the sidecar
        already holds everything, possibly including a transcript from the first
        time round.
        """
        from app.integrations.inbox import scan

        return scan.summarise(scan.read_sidecar(path), size_bytes=size_bytes)

    def email_ingested_document(self, meta: dict[str, Any], path: Path) -> bool:
        """Email a just-ingested document to its owner, if it is due one.

        The durable half of the capture confirmation: `confirm_ingest` puts a
        line on a lock screen, this puts the extracted text somewhere it can be
        found again. Audio and images are excluded here and emailed by their own
        sweeps — see `scan.email_ingested_document`.

        Never raises, for the same reason as `confirm_ingest`: the file is on
        disk before this runs.
        """
        from app.integrations.inbox import scan

        try:
            return scan.email_ingested_document(meta, path)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).debug(
                "inbox: document email failed", exc_info=True
            )
            return False

    def confirm_ingest(self, meta: dict[str, Any], path: Path) -> bool:
        """Push an arrival confirmation for a just-ingested file, if enabled.

        Exists so `POST /api/inbox/ingest` can announce a capture itself instead
        of relying on the caller to do it from the response body. That is the last
        job holding the Tines relay in the capture path — see
        `scan.notify_ingested` for why it is config-gated and not simply on.

        Returns whether a push was attempted. Never raises: the file is already on
        disk, and a notification failure must not turn a successful capture into an
        error the producer retries.
        """
        from app.integrations.inbox import scan

        try:
            return scan.notify_ingested(meta, path)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).debug(
                "inbox: ingest confirmation failed", exc_info=True
            )
            return False

    def enrich_for_response(
        self, path: Path, *, size_bytes: int | None = None
    ) -> tuple[dict[str, Any], str]:
        """Enrich a just-ingested file and render its one-line summary.

        Returns `(sidecar_meta, summary)`. Idempotent — the hourly enrichment
        cron re-running over the same file is a no-op. Raises whatever `scan`
        raises; the route treats enrichment as best-effort and catches, because
        the file is already safely on disk by the time this is called.
        """
        from app.integrations.inbox import scan

        meta = scan.enrich_one(path)
        return meta, scan.summarise(meta, size_bytes=size_bytes)

    def transcribe_in_background(self, background_tasks: Any, path: Path) -> None:
        """Schedule an immediate transcription attempt for `path` (Task C of
        the Tines retirement) instead of leaving it to the next `*/5 * * * *`
        cron tick — a straight migration off Tines (which returned a
        transcript in about a minute) would otherwise make capture feel much
        slower than it used to.

        `background_tasks` is a FastAPI `BackgroundTasks` instance; scheduling
        it here (rather than the route calling `background_tasks.add_task`
        directly on a `scan` function) keeps the route's only reach into this
        package going through the facade, same as every other method here.

        Fires-and-forgets `scan.transcribe_file_task`, which races the cron
        sweep on this same file by design — see
        `scan._acquire_transcription_lock` for why that's safe.
        """
        from app.integrations.inbox import scan

        background_tasks.add_task(scan.transcribe_file_task, path)


FACADE = InboxFacade()
