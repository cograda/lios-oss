"""Manifest-driven route mounting — V4 chunk 3.1.

Replaces the hand-maintained import+include_router pair in
`app/routes/__init__.py` (previously: `apple_reminders.routes` and
`apple_health.routes`, hardcoded by name). Each integration manifest
declares its routers as dotted refs (`routes: list[str]`, e.g.
`"app.integrations.apple_reminders.routes:router"`); `mount_integration_routes()`
walks every manifest in deterministic (sorted-by-name) order, resolves each
ref via `app.plugin.refs.resolve_ref()`, and mounts it on the kernel's
`/api`-prefixed router.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.plugin.refs import resolve_ref
from app.plugin.validate import discover_manifests

logger = logging.getLogger(__name__)


def mount_integration_routes(router: APIRouter) -> None:
    """Mount every integration-declared router onto `router`."""
    for name, manifest in sorted(discover_manifests().items()):
        for ref in manifest.routes:
            integration_router = resolve_ref(ref)
            router.include_router(integration_router)
            logger.info(f"Mounted routes for {name}: {ref}")
