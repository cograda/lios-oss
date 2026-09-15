"""Sync-on-read freshness — ensure cached data is fresh before tool execution.

When a tool is called, check when its integration last synced. If the data
is older than the integration's staleness threshold, run a sync inline
before returning results. This keeps the "scheduled sync" as the baseline
but ensures interactive queries always get reasonably fresh data.

V4 chunk 1.3: thresholds come from each integration's own manifest
(`freshness_threshold_minutes`) instead of a hand-maintained kernel dict.
`None` (either an explicit manifest value, or an integration with no
manifest entry at all — e.g. `embedding`, which isn't an integration)
means "never auto-refresh".
"""

import concurrent.futures
import logging
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _threshold_seconds_by_integration() -> dict[str, int | None]:
    """{integration name: freshness threshold in seconds, or None}.

    Sourced from every integration's manifest via
    `app.plugin.validate.discover_manifests()` — the same registry chunk
    1.2 uses to drive model discovery. Cached for the process lifetime;
    manifests are static, imported once at startup.
    """
    from app.plugin.validate import discover_manifests

    return {
        name: (
            manifest.freshness_threshold_minutes * 60
            if manifest.freshness_threshold_minutes is not None
            else None
        )
        for name, manifest in discover_manifests().items()
    }


def threshold_seconds(integration_name: str) -> int | None:
    """Freshness threshold for `integration_name`, in seconds.

    None means "never auto-refresh" — either the manifest says so
    explicitly, or the name isn't a registered integration at all (matches
    the old dict's `.get()` fallback behavior for absent keys).
    """
    return _threshold_seconds_by_integration().get(integration_name)


def ensure_fresh(integration_name: str, session: Session) -> None:
    """Check if integration data is stale and sync if needed.

    Called from the tool dispatch layer (MCP call_tool) before executing
    the tool handler. Runs the integration's sync() method inline if the
    cached data exceeds the staleness threshold.

    This runs in a thread (asyncio.to_thread).
    integration.sync() is itself a plain blocking function; we hand it to a
    dedicated worker thread so we can still enforce the 30s timeout without
    blocking this thread indefinitely on a hung sync.
    """
    threshold = threshold_seconds(integration_name)
    if threshold is None:
        return  # No freshness check for this integration

    from app.models.tokens import SyncState

    state = session.query(SyncState).filter_by(integration=integration_name).first()

    if state and state.last_sync_at:
        age = (datetime.now(timezone.utc) - state.last_sync_at).total_seconds()
        if age < threshold:
            return  # Data is fresh enough

    # Data is stale — run sync
    from app.integrations import get as get_integration

    integration = get_integration(integration_name)
    if integration is None:
        logger.warning(f"Freshness: integration {integration_name} not found")
        return

    if not integration.is_configured():
        return

    logger.info(
        f"Freshness: {integration_name} data is stale "
        f"(threshold={threshold}s), syncing inline"
    )

    try:
        # Don't use the executor as a context manager: __exit__ calls
        # shutdown(wait=True), which would block here until a hung sync
        # actually finishes — exactly what the timeout is meant to avoid.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            executor.submit(integration.sync).result(timeout=30)
        finally:
            executor.shutdown(wait=False)

        # Update sync state
        _update_sync_state(integration_name, session, "ok")
        logger.info(f"Freshness: {integration_name} sync completed")

    except concurrent.futures.TimeoutError:
        logger.warning(f"Freshness: {integration_name} sync timed out (30s)")
        _update_sync_state(integration_name, session, "error", "Freshness sync timed out")

    except Exception as e:
        # Don't fail the tool call — serve stale data rather than nothing
        logger.warning(f"Freshness: {integration_name} sync failed: {e}")
        _update_sync_state(integration_name, session, "error", str(e)[:500])


def _update_sync_state(
    integration_name: str, session: Session, status: str, error: str | None = None
) -> None:
    """Update the SyncState row for this integration."""
    from app.models.tokens import SyncState

    state = session.query(SyncState).filter_by(integration=integration_name).first()
    if not state:
        state = SyncState(integration=integration_name)
        session.add(state)
    state.last_sync_at = datetime.now(timezone.utc)
    state.last_sync_status = status
    state.last_error = error
    session.commit()
