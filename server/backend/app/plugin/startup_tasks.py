"""Startup-task lifecycle — V4 chunk 3.1.

Discovers every manifest's `kind="startup"` background task (the obsidian
vault watcher, the Home Assistant WebSocket listener), launches each under
`app.plugin.supervisor.supervise()` as an `asyncio.Task`, and tears them
down cleanly on shutdown. This is what `app/main.py`'s lifespan calls
instead of importing `app.integrations.obsidian.watcher` /
`app.integrations.homeassistant.events` directly.
"""

from __future__ import annotations

import asyncio
import logging

from app.plugin.config_store import is_integration_enabled
from app.plugin.refs import resolve_ref
from app.plugin.supervisor import supervise
from app.plugin.validate import discover_manifests

logger = logging.getLogger(__name__)


def start_all() -> tuple[list[asyncio.Task], asyncio.Event]:
    """Start every manifest-declared startup task. Returns (tasks, stop_event)."""
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []
    for name, manifest in sorted(discover_manifests().items()):
        if not is_integration_enabled(name):
            logger.info(f"Skipping startup tasks for {name} — disabled")
            continue
        for task_spec in manifest.background_tasks:
            if task_spec.kind != "startup":
                continue
            target = resolve_ref(task_spec.target)
            task = asyncio.create_task(
                supervise(task_spec.name, target, stop_event),
                name=f"startup-task:{task_spec.name}",
            )
            tasks.append(task)
            logger.info(f"Started background task {task_spec.name} ({name})")
    return tasks, stop_event


async def stop_all(tasks: list[asyncio.Task], stop_event: asyncio.Event) -> None:
    """Signal every startup task to stop and wait for them to exit."""
    stop_event.set()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
