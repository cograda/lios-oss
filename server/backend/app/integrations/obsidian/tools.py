"""MCP tool definitions and handlers for Obsidian vault integration.

vault_search stays CustomTool because the SemanticSearchTool builder doesn't
expose the folder/source-filter the EmbeddingService supports. vault_recent
and vault_stats fit ListTool / StatsTool cleanly.
vault_transfer is a CustomTool — it moves a file across user vaults; no DSL
shape fits.
"""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.context import current_user_id
from app.config import settings
from app.integrations.obsidian.models import VaultChunk
from app.models.users import User
from app.services import vault_paths
from app.services.embedding import EmbeddingService, MODEL_NAME
from app.tools import CustomTool, ExtraFilter, ListTool, StatsTool
from app.tools.helpers import iso_or_none, serialize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hand-written: vault_search (needs source_filter passthrough)
# ---------------------------------------------------------------------------

def handle_search(session: Session, arguments: dict[str, Any]) -> str:
    """Semantic search across the vault, optionally scoped to a folder."""
    query = arguments.get("query", "").strip()
    if not query:
        return json.dumps({"error": "query is required"})

    top_k = min(int(arguments.get("limit", 10)), 50)
    folder = arguments.get("folder")

    results = EmbeddingService.search(
        session, query=query, sources=["vault"], limit=top_k, source_filter=folder,
    )

    paths = [r["source_id"] for r in results]
    mtimes: dict[str, Any] = {}
    if paths:
        rows = (
            session.query(VaultChunk.path, VaultChunk.modified_at)
            .filter(VaultChunk.path.in_(paths))
            .all()
        )
        mtimes = {r.path: r.modified_at for r in rows}

    formatted = [
        {
            "path": r["source_id"],
            "score": r["score"],
            "preview": r["preview"][:200],
            "modified": mtimes.get(r["source_id"], "").isoformat()
            if mtimes.get(r["source_id"]) else None,
        }
        for r in results
    ]
    return json.dumps(formatted, indent=2)


# ---------------------------------------------------------------------------
# DSL: ListTool folder filter (prefix ILIKE) + StatsTool compute
# ---------------------------------------------------------------------------

def _folder_prefix(session: Session, query, value: str):
    """Filter VaultChunk.path with a startswith ILIKE."""
    if not value:
        return query
    return query.filter(VaultChunk.path.ilike(f"{value}%"))


def _vault_to_dict(c: VaultChunk) -> dict:
    return serialize(
        c, ["path", "modified_at"],
        renames={"modified_at": "modified"},
        transforms={"modified_at": iso_or_none},
    )


def _vault_stats_compute(session: Session, _arguments: dict[str, Any]) -> dict:
    from app.services.embedding import Embedding, EmbeddingQueue

    total_tracked = session.query(func.count(VaultChunk.id)).scalar() or 0
    total_embedded = (
        session.query(func.count(Embedding.id)).filter_by(source="vault").scalar() or 0
    )
    latest = session.query(func.max(VaultChunk.indexed_at)).scalar()

    folders = (
        session.query(
            func.split_part(VaultChunk.path, "/", 1).label("folder"),
            func.count(VaultChunk.id),
        )
        .group_by("folder")
        .order_by(func.count(VaultChunk.id).desc())
        .all()
    )

    queue_pending = (
        session.query(func.count(EmbeddingQueue.id))
        .filter_by(source="vault", status="pending").scalar() or 0
    )

    return {
        "total_files": total_tracked,
        "total_embedded": total_embedded,
        "queue_pending": queue_pending,
        "model": MODEL_NAME,
        "last_indexed": latest.isoformat() if latest else None,
        "by_folder": {name: count for name, count in folders},
    }


# ---------------------------------------------------------------------------
# Hand-written: vault_transfer (move a file from caller's vault to recipient's Inbox/)
# ---------------------------------------------------------------------------

def handle_transfer(session: Session, arguments: dict[str, Any]) -> str:
    """Move a file from the calling user's vault into a recipient's Inbox.

    True handoff semantics — the file leaves the sender's vault and lands in
    the recipient's `Inbox/<filename>`. Syncthing propagates both the delete
    (from sender) and the create (in recipient) to all paired Macs within
    seconds; no further wiring needed.

    The caller-facing `path` is logical (e.g. `Notes/recipe.md`); it resolves
    against the calling user's vault via `vault_paths.resolve()`.
    """
    path_arg = (arguments.get("path") or "").strip()
    recipient_name = (arguments.get("recipient") or "").strip().lower()
    if not path_arg or not recipient_name:
        return json.dumps({"error": "both `path` and `recipient` are required"})

    uid = current_user_id()
    if uid is None:
        return json.dumps({"error": "no authenticated user"})

    sender = session.get(User, uid)
    if sender is None:
        return json.dumps({"error": "calling user not found"})
    if sender.name == recipient_name:
        return json.dumps({"error": "cannot transfer to yourself"})

    recipient = session.execute(
        select(User).where(User.name == recipient_name)
    ).scalar_one_or_none()
    if recipient is None or not recipient.is_active:
        return json.dumps({"error": f"unknown or inactive recipient: {recipient_name}"})

    src = vault_paths.resolve(path_arg)
    if not src.is_file():
        return json.dumps({"error": f"source file not found: {path_arg}"})

    inbox = vault_paths.user_vault_path(recipient.name) / "Inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    # Avoid overwriting an existing file in the recipient's inbox: prefix a timestamp.
    dst = inbox / src.name
    if dst.exists():
        stem, suffix = src.stem, src.suffix
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dst = inbox / f"{stem}.{ts}{suffix}"

    shutil.move(str(src), str(dst))
    logger.info(
        "vault_transfer: %s/%s → %s/Inbox/%s",
        sender.name, path_arg, recipient.name, dst.name,
    )

    # CRITICAL: poke Syncthing to rescan both affected folders immediately.
    # Without this, Syncthing waits up to 60s for its next periodic rescan, and
    # the calling user's Mac (which still has the source file locally) will
    # announce its version first → server resurrects the file from Mac's
    # "newer" view. The rescan flips the order so Syncthing notices the
    # server-side delete + create before Mac has time to re-announce.
    try:
        _poke_rescan(f"vault-{sender.name}", str(src.relative_to(vault_paths.user_vault_path(sender.name))))
        _poke_rescan(f"vault-{recipient.name}", str(dst.relative_to(vault_paths.user_vault_path(recipient.name))))
    except Exception:
        logger.exception("vault_transfer: rescan poke failed (non-fatal; sync will catch up at next periodic rescan)")

    return json.dumps({
        "ok": True,
        "from": f"{sender.name}/{path_arg}",
        "to": f"{recipient.name}/Inbox/{dst.name}",
    })


def _poke_rescan(folder_id: str, sub_path: str) -> None:
    """Tell the local Syncthing to rescan a specific path under a folder.

    `sub_path` is relative to the folder root; rescanning a specific subpath is
    faster than rescanning the whole folder. Blocking but fast (<200ms).
    """
    import httpx

    if not settings.syncthing_api_key:
        return  # No-op if Syncthing isn't wired (e.g. dev environment)

    base = settings.syncthing_url.rstrip("/")
    params = {"folder": folder_id}
    if sub_path:
        params["sub"] = sub_path
    r = httpx.post(
        f"{base}/rest/db/scan",
        params=params,
        headers={"X-API-Key": settings.syncthing_api_key},
        timeout=5.0,
    )
    if r.status_code not in (200, 204):
        logger.warning("Syncthing rescan poke for %s/%s returned %s", folder_id, sub_path, r.status_code)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def get_mcp_tools() -> list[dict]:
    return [
        CustomTool(
            name="vault_search",
            description=(
                "Semantic search across the Obsidian vault — finds notes by meaning, "
                "not just keywords. Returns file paths, similarity scores, and content "
                "previews. Optionally filter to a specific folder. "
                "For keyword search, use the client's native grep against the vault path."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, max 50).",
                        "default": 10,
                    },
                    "folder": {
                        "type": "string",
                        "description": "Filter to a vault folder (e.g. 'Household', 'Daily Notes/Alex').",
                    },
                },
                "required": ["query"],
            },
            handler=handle_search,
            category="search",
            examples=[
                "Find notes about the renovation",
                "Search for Finn's medical notes",
            ],
        ).build(),

        ListTool(
            name="vault_recent",
            description=(
                "List recently modified files in the Obsidian vault, sorted newest first. "
                "Shows file path and modification time. Use this to see what's been "
                "worked on lately or find recently created notes."
            ),
            model=VaultChunk,
            timestamp_col="modified_at",
            to_dict=_vault_to_dict,
            default_limit=20,
            max_limit=100,
            extra_filters=[
                ExtraFilter(
                    param_name="folder",
                    column="path",
                    description="Filter to a vault folder (prefix match).",
                    match_mode=_folder_prefix,
                ),
            ],
            category="home",
            examples=[
                "What notes were changed recently?",
                "Show recent daily notes",
            ],
        ).build(),

        CustomTool(
            name="vault_transfer",
            description=(
                "Move a file from your personal vault into another user's Inbox. "
                "True handoff — the file leaves your vault and appears in theirs. "
                "Useful for passing tasks, notes, or recipes to a household member. "
                "Recipient's Mac receives the file within seconds via Syncthing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Logical path inside your vault (e.g. 'Notes/recipe.md').",
                    },
                    "recipient": {
                        "type": "string",
                        "description": "Short username of the recipient (e.g. 'sam', 'alex').",
                    },
                },
                "required": ["path", "recipient"],
            },
            handler=handle_transfer,
            category="home",
            examples=[
                "Send 'Notes/Carrot Soup.md' to Sam",
                "Hand off the renovation snag list to Alex",
            ],
        ).build(),

        StatsTool(
            name="vault_stats",
            description=(
                "Vault index statistics: total files indexed, embedding count, "
                "queue status, embedding model info, and file counts by folder. "
                "Admin tool for checking index health."
            ),
            model=VaultChunk,
            compute=_vault_stats_compute,
            input_schema={"type": "object", "properties": {}},
            category="system",
        ).build(),
    ]
